"""The call itself — ported from agentic-workspace's
``src/meta_display/call_agent.py`` (``CallAgentMixin``), which was itself the
standalone ``aw-call-agent`` service folded into the monolith's meta_display
process in 2026-07.

What came across unchanged, because it is the part that actually works:

* the Edge-TTS voice map and ``_pick_edge_voice`` / ``_strip_markdown``
  (monolith lines 54-78) — including the iOS ``en_US`` underscore quirk;
* raw MP3 out of ``tts()``, deliberately *not* transcoded (monolith's comment
  at line 48: every other TTS path in AW re-encodes to OGG/Opus for Telegram,
  which the call client cannot decode);
* ``latest_target_session_id`` — the over-HTTP reconstruction of the last
  Claude Code session for this Target, so a new call resumes the same
  conversation instead of starting blank (monolith line 128);
* ``stream_run`` — the poll-the-event-log loop with heartbeats on every tick,
  and ``run_agent_with_heartbeat`` for the window *before* a run_id exists.
  Both heartbeat paths are load-bearing regression fixes (monolith lines 173
  and 208 explain each at length); dropping them re-opens a bug where a
  perfectly healthy but slow-to-first-token turn gets killed by the client's
  stall watchdog.

What had to change, and why:

* **No meta_display.** The monolith mixed this into ``MetaDisplayRoutes`` and
  broadcast every turn through ``self._ws_broadcast`` so the glasses webapp,
  the Watch and the durable history log all saw the call live. None of that
  machinery exists in aw-workspace — there is no meta device session, no
  ``self.agent.db``, no ``_session_for_run``. So this port is the call and
  only the call: the WS client is the sole surface. Everything a turn
  produces still lands in Agents Platform's own run/event log, which is the
  durable record here.
* **The Target is created, not assumed.** The monolith leaned on
  ``run_sync``'s auto-provisioning of ``<agent>-<external_id>`` for the
  Watch. This port dispatches through plain ``POST /api/agents/<slug>/run``
  with ``target_slug``, which 404s on a Target that does not exist yet — so
  ``ensure_target()`` creates it on first call (409 = someone else won the
  race, which is success).
* **Auth.** agents-platform-multitenant's ``require_identity()`` rejects
  unauthenticated calls, so every request carries the configured bearer
  token. The monolith talked to a local, unauthenticated instance.
* **Hardcoded constants became config.** ``AGENT_SLUG``/``EXTERNAL_ID`` and
  the ``/aw-apple-watch`` prompt header were fixed strings for one iOS app;
  here they are ``agent_slug`` / ``external_id`` / ``prompt_template``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("aw_apps.call_agent.service")

#: Edge TTS voice map — carried over verbatim from the monolith (which in turn
#: mirrored aw.json's ``workspace_agent.edge_voices``).
EDGE_VOICES: dict[str, str] = {
    "_default": "pt-BR-AntonioNeural",
    "pt": "pt-BR-AntonioNeural",
    "en": "en-US-AndrewMultilingualNeural",
    "de": "de-DE-ConradNeural",
    "es": "es-MX-JorgeNeural",
    "fr": "fr-FR-HenriNeural",
    "it": "it-IT-DiegoNeural",
}


def pick_edge_voice(lang: str) -> str:
    # iOS's Locale identifiers use an underscore ("en_US"), not a hyphen
    # ("en-US") — splitting on "-" alone silently no-ops on those. Browsers
    # send the hyphen form; both have to work.
    iso = lang.replace("_", "-").split("-")[0].lower() if lang else ""
    return EDGE_VOICES.get(iso) or EDGE_VOICES["_default"]


def strip_markdown(text: str) -> str:
    """Markdown is unspeakable — an agent that writes ``**done**`` must not
    have "asterisk asterisk done" read out. Ported as-is."""
    text = re.sub(r'\*+([^*]+)\*+', r'\1', text)
    text = re.sub(r'_+([^_]+)_+', r'\1', text)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    text = re.sub(r'#+\s*', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    return text.strip()


class CallAgentError(RuntimeError):
    """Raised for a misconfiguration the caller can actually fix (no base URL,
    no token) — surfaced to the WS client as an ``error`` frame rather than a
    stack trace in a log nobody reads."""


@dataclass
class CallSettings:
    """Effective settings for one call. Built by ``settings.resolve()``."""

    agents_platform_base: str = ""
    agents_platform_token: str = ""
    agent_slug: str = "call-agent-openai"
    external_id: str = "aw-call-shared"
    prompt_template: str = (
        "CONTEXT:\n- source: streaming_call\n- surface: voice\n"
        "Answer in short spoken prose — no markdown, no lists, no step "
        "narration. Reply in the language the user spoke.\nUSER_MESSAGE:\n${text}"
    )
    default_voice_lang: str = "pt-BR"
    stt_provider: str = "faster-whisper"
    stt_openai_model: str = "gpt-4o-mini-transcribe"
    stt_realtime_model: str = "gpt-live-transcribe"
    stt_realtime_delay: str = "low"
    tts_provider: str = "edge"
    tts_openai_model: str = "gpt-4o-mini-tts"
    tts_openai_voice: str = "alloy"
    openai_api_key: str = ""
    voice_runtime: str = "openai-realtime"
    realtime_model: str = "gpt-realtime-2.1-mini"
    realtime_voice: str = "alloy"
    #: Silence, in ms, that ends an utterance. The browser's own end-of-speech
    #: detection is far too eager for someone who thinks mid-sentence, so the
    #: client does its own with this value.
    speech_pause_ms: float = 650.0
    poll_interval_seconds: float = 0.1
    max_poll_seconds: float = 300.0
    #: Where the base/token actually came from — surfaced by GET /settings so
    #: "why is this app not calling anything" is answerable without a log.
    source: str = "unset"

    @property
    def target_slug(self) -> str:
        return f"{self.agent_slug}-{self.external_id}"

    @property
    def max_polls(self) -> int:
        interval = self.poll_interval_seconds or 0.1
        return max(1, int(self.max_poll_seconds / interval))

    def build_prompt(self, text: str) -> str:
        template = self.prompt_template or "${text}"
        if "${text}" not in template:
            # A template that forgot the placeholder would silently send the
            # agent the same fixed prompt for every turn — the caller's words
            # would vanish. Append instead of dropping them.
            return f"{template}\n{text}"
        return template.replace("${text}", text)


class CallAgentService:
    """Everything a call needs from Agents Platform. One instance per request
    is fine — it holds no connection state, only settings."""

    def __init__(self, settings: CallSettings):
        self.settings = settings

    # ---- helpers -----------------------------------------------------------

    def _require_backend(self) -> tuple[str, dict[str, str]]:
        s = self.settings
        if not s.agents_platform_base:
            raise CallAgentError(
                "no agents-platform base URL configured — set "
                "`agents_platform_base` in this app's settings, or install the "
                "Agents Platform Runners app to inherit its own.")
        headers = {}
        if s.agents_platform_token:
            headers["Authorization"] = f"Bearer {s.agents_platform_token}"
        return s.agents_platform_base.rstrip("/"), headers

    def client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        base, headers = self._require_backend()
        return httpx.AsyncClient(base_url=base, headers=headers, timeout=timeout)

    # ---- REST surface ------------------------------------------------------

    async def agents_list(self) -> dict:
        """Proxy the agents list — used by the call UI's agent picker. Direct
        port of the monolith's ``call_agents_list`` (which was itself
        aw-call-agent's ``GET /api/agents-list``), including the fallback to a
        single synthetic row so the picker is never empty."""
        fallback = {"agents": [{"slug": self.settings.agent_slug,
                                "name": self.settings.agent_slug}]}
        try:
            async with self.client(timeout=10.0) as client:
                resp = await client.get("/api/agents")
                resp.raise_for_status()
                data = resp.json()
                agents = (data if isinstance(data, list)
                          else data.get("items", data.get("agents", [])))
                rows = [{"slug": a.get("slug"), "name": a.get("name") or a.get("slug")}
                        for a in agents if a.get("slug")]
                return {"agents": rows or fallback["agents"]}
        except Exception as exc:
            log.warning("Could not fetch agents list: %s", exc)
            return fallback

    async def tts(self, text: str, lang: str = "") -> bytes:
        """Speech via the selected provider, returned as raw MP3.

        Deliberately a direct ``edge_tts`` call and deliberately not
        transcoded — see this module's docstring.
        """
        clean = strip_markdown(text)
        if not clean:
            raise CallAgentError("empty text")

        if self.settings.tts_provider == "openai":
            if not self.settings.openai_api_key:
                raise CallAgentError(
                    "OpenAI TTS is selected but `openai_api_key` is empty")
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={
                        "Authorization": f"Bearer {self.settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.settings.tts_openai_model,
                        "voice": self.settings.tts_openai_voice,
                        "input": clean,
                        "response_format": "mp3",
                    },
                )
            if response.status_code >= 400:
                raise CallAgentError(
                    f"OpenAI TTS failed ({response.status_code}): "
                    f"{response.text[:200]}")
            return response.content

        if self.settings.tts_provider != "edge":
            raise CallAgentError(
                f"unsupported TTS provider: {self.settings.tts_provider}")

        import edge_tts

        voice = pick_edge_voice(lang or self.settings.default_voice_lang)

        buf = io.BytesIO()
        communicate = edge_tts.Communicate(clean, voice)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    # ---- run lifecycle -----------------------------------------------------

    async def ensure_target(self, client: httpx.AsyncClient) -> None:
        """Create ``<agent>-<external_id>`` if it isn't there yet.

        New in this port: the monolith got the Target for free from
        ``run_sync``'s auto-provisioning. ``POST /api/agents/<slug>/run``
        404s on an unknown ``target_slug`` instead, which would make the very
        first call of a fresh install fail with a message about a slug the
        user never typed. A 409 means someone else created it between our GET
        and our POST — that is success, not an error (same rule the runners
        app's agent provisioner uses for seeding).
        """
        slug = self.settings.target_slug
        try:
            resp = await client.get(f"/api/targets/{slug}")
            if resp.status_code == 200:
                return
        except Exception as exc:
            log.debug("target probe failed for %s: %s", slug, exc)
        try:
            resp = await client.post("/api/targets", json={
                "slug": slug,
                "name": f"Call — {self.settings.agent_slug}",
                "description": ("Auto-created by the Call Agent app. Every voice "
                                "call to this agent resumes the conversation here."),
                "source_kind": "manual",
                "created_by": "call-agent",
            })
            if resp.status_code in (200, 201, 409):
                return
            log.warning("could not create target %s: %s %s", slug,
                        resp.status_code, resp.text[:200])
        except Exception as exc:
            log.warning("could not create target %s: %s", slug, exc)

    async def latest_target_session_id(self, client: httpx.AsyncClient) -> str | None:
        """Most recent Claude Code session_id used for this Target, so a new
        call resumes where the last one left off.

        Ported from the monolith's ``_call_latest_target_session_id``: list the
        Target's runs, walk them newest-first, and read each run's events until
        one carries a persisted ``system.init.session_id``. Still done over
        HTTP (not a shared DB) for the same reason as there — this process only
        ever talks to Agents Platform through its REST API.
        """
        slug = self.settings.target_slug
        try:
            resp = await client.get(f"/api/targets/{slug}/runs", params={"limit": 2000})
            resp.raise_for_status()
            runs = resp.json().get("runs", [])
        except Exception as exc:
            log.warning("Could not list runs for target %s: %s", slug, exc)
            return None
        # Bound the walk: a long-lived Target accumulates thousands of runs and
        # the monolith would issue one events GET per run before giving up. The
        # session id lives in the newest handful or nowhere useful.
        for run in list(reversed(runs))[:25]:
            try:
                ev_resp = await client.get(f"/api/runs/{run['id']}/events",
                                           params={"kinds": "system.init"})
                ev_resp.raise_for_status()
                for event in ev_resp.json():
                    if event.get("kind") == "system.init":
                        sid = (event.get("payload") or {}).get("session_id")
                        if sid:
                            return sid
            except Exception:
                continue
        return None

    async def run_agent(self, prompt: str, claude_session_id: str | None) -> str:
        """Start an agent run and return the run_id."""
        body: dict[str, Any] = {
            "input": {"input": prompt},
            "target_slug": self.settings.target_slug,
        }
        if claude_session_id:
            body["session_id"] = claude_session_id

        async with self.client(timeout=60.0) as client:
            await self.ensure_target(client)
            resp = await client.post(
                f"/api/agents/{self.settings.agent_slug}/run", json=body)
            if resp.status_code == 404:
                raise CallAgentError(
                    f"agent '{self.settings.agent_slug}' not found on "
                    f"{self.settings.agents_platform_base} — pick a different "
                    f"agent in this app's settings.")
            resp.raise_for_status()
            return resp.json()["run_id"]

    async def run_agent_with_heartbeat(
        self, prompt: str, claude_session_id: str | None,
        heartbeat: Callable[[], Awaitable[None]],
    ) -> str:
        """``run_agent`` but pumping heartbeats while the POST is in flight.

        Ported wholesale, including the reason (monolith line 173): the POST
        blocks on the executor's per-Target session lock, so a call can queue
        silently behind a prior in-flight turn on the same Target. With no
        heartbeats during that wait the client's stall watchdog can expire
        before the run even starts, killing a healthy-but-queued call.
        """
        task = asyncio.ensure_future(self.run_agent(prompt, claude_session_id))
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task),
                                       timeout=self.settings.poll_interval_seconds)
            except asyncio.TimeoutError:
                await heartbeat()
        return task.result()

    async def stream_run(
        self, run_id: str,
        on_delta: Callable[[str], Awaitable[None]],
        heartbeat: Callable[[], Awaitable[None]],
    ) -> tuple[str, str | None]:
        """Poll the run's event log, streaming ``llm_token`` deltas out.
        Returns ``(full_text, claude_session_id)``.

        The heartbeat on *every* tick — not only ticks that produced events —
        is the monolith's 2026-07-12 regression fix (its line 208 comment): a
        container cold start, a session resume, or any tool-call phase produces
        no ``llm_token`` events at all, and a client that only re-arms its
        watchdog on real text would kill the turn mid-flight.
        """
        s = self.settings
        full_text = ""
        claude_session_id: str | None = None
        after_ts: str | None = None

        async with self.client(timeout=60.0) as client:
            for _ in range(s.max_polls):
                params: dict[str, str] = {}
                if after_ts is not None:
                    params["after_ts"] = after_ts

                try:
                    resp = await client.get(f"/api/runs/{run_id}/events", params=params)
                    resp.raise_for_status()
                    events: list[dict] = resp.json()
                except Exception as exc:
                    log.warning("Event poll error: %s", exc)
                    await heartbeat()
                    await asyncio.sleep(s.poll_interval_seconds)
                    continue

                done = False
                for event in events:
                    kind = event.get("kind", "")
                    payload = event.get("payload", {}) or {}
                    ts = event.get("ts")
                    if ts is not None:
                        after_ts = ts

                    if kind == "system.init" and not claude_session_id:
                        claude_session_id = payload.get("session_id")

                    elif kind == "llm_token":
                        delta = payload.get("delta", "")
                        if delta:
                            full_text += delta
                            await on_delta(delta)

                    elif kind == "node_end":
                        text = payload.get("text", "")
                        if text and not full_text:
                            full_text = text
                        done = True

                    elif kind in ("done", "error"):
                        if kind == "error" and not full_text:
                            full_text = str(payload.get("message") or payload.get("text") or "")
                        done = True

                if done:
                    break
                await heartbeat()
                await asyncio.sleep(s.poll_interval_seconds)

        return full_text, claude_session_id
