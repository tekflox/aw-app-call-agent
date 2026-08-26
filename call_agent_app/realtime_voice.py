"""Full-duplex OpenAI Realtime voice session for an AudioSocket call."""
from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from .call_history import CallStore
from .service import CallAgentError, CallSettings

log = logging.getLogger("aw_apps.call_agent.realtime_voice")
# Uvicorn's default application logger threshold is WARNING. Latency tracing
# is intentionally INFO and must remain visible without turning every library
# in the container verbose.
log.setLevel(logging.INFO)
latency_log = logging.getLogger("uvicorn.error")

REALTIME_CRISPAL_TOOLS = [
    "agent_crispal_haiku",
    "agent_crispal_sonnet",
    "agent_crispal_social_sonnet",
]
REALTIME_CRISPAL_TARGET = "call-agent-crispal"


class PCMResampler:
    """Stateful C-backed telephone/Realtime resampler."""

    def __init__(self, input_rate: int, output_rate: int):
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.state = None

    def convert(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        # A 1:2 IIR provides a cheap anti-alias low-pass filter for
        # 24 kHz -> 8 kHz while ratecv keeps phase state across WS deltas.
        weight_a, weight_b = ((1, 2) if self.output_rate < self.input_rate
                              else (1, 0))
        output, self.state = audioop.ratecv(
            pcm, 2, 1, self.input_rate, self.output_rate,
            self.state, weight_a, weight_b)
        return output


def pcm8k_to_pcm24k(pcm: bytes) -> bytes:
    return PCMResampler(8000, 24000).convert(pcm)


def pcm24k_to_pcm8k(pcm: bytes) -> bytes:
    return PCMResampler(24000, 8000).convert(pcm)


class OpenAIRealtimeVoiceSession:
    """One persistent audio-in/audio-out session per phone call."""

    def __init__(self, call_id: str, settings: CallSettings, store: CallStore,
                 emit: Callable[[bytes], Awaitable[None]]):
        self.call_id = call_id
        self.settings = settings
        self.store = store
        self.emit = emit
        self.ws = None
        self.reader_task: asyncio.Task | None = None
        self.sender_task: asyncio.Task | None = None
        self._input_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=100)
        self._input_dropped = 0
        self._input_resampler = PCMResampler(8000, 24000)
        self._output_resampler = PCMResampler(24000, 8000)
        self._transcripts: asyncio.Queue[str] = asyncio.Queue()
        self._last_transcript = ""
        self._reply_parts: dict[str, list[str]] = {}
        self._persist_tasks: set[asyncio.Task] = set()
        self._closed = False
        self.response_active = False
        self.response_id = ""
        self.output_response_id = ""
        self._connected_at = 0.0
        self._speech_started_at = 0.0
        self._speech_stopped_at = 0.0
        self._response_created_at = 0.0
        self._first_audio_seen = False

    def _latency(self, event: str, **values) -> None:
        now = time.monotonic()
        fields = {"call": self.call_id, "event": event, **values}
        if self._connected_at:
            fields["session_ms"] = round((now - self._connected_at) * 1000, 1)
        if self._speech_stopped_at:
            fields["after_speech_stop_ms"] = round(
                (now - self._speech_stopped_at) * 1000, 1)
        latency_log.info("call_latency %s", " ".join(
            f"{key}={value}" for key, value in fields.items()))

    async def _control_plane(self) -> tuple[str, list[dict]]:
        base = self.settings.prompt_template.replace("${text}", "").strip()
        if not self.settings.agents_platform_base:
            return base, []
        headers = ({"Authorization": f"Bearer {self.settings.agents_platform_token}"}
                   if self.settings.agents_platform_token else {})
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    f"{self.settings.agents_platform_base.rstrip('/')}/api/agents/"
                    f"{self.settings.agent_slug}", headers=headers)
                response.raise_for_status()
                agent = response.json()
        except Exception as exc:
            log.warning("could not load realtime control plane for %s: %s",
                        self.settings.agent_slug, exc)
            return base, []
        system = str(agent.get("system_prompt") or "").strip()
        capabilities = str(agent.get("capabilities") or "").strip()
        instructions = "\n\n".join(part for part in (
            system, capabilities, base,
            "You are in a live full-duplex telephone call. Respond briefly in "
            "the caller's language. If interrupted, stop immediately and answer "
            "the newest utterance. Never promise to use a tool later; use it now "
            "or clearly say it is unavailable. For Crispal store questions or "
            "actions, use the most appropriate Crispal MCP agent. Always pass "
            f"target_slug='{REALTIME_CRISPAL_TARGET}'. Briefly acknowledge that "
            "you are checking, then report the tool result in natural spoken prose.",
        ) if part)

        config_slug = str(agent.get("agent_config_slug") or "").strip()
        if not config_slug:
            return instructions, []
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    f"{self.settings.agents_platform_base.rstrip('/')}/api/agent-configs/"
                    f"{config_slug}", headers=headers)
                response.raise_for_status()
                config = response.json()
        except Exception as exc:
            log.warning("could not load realtime MCP config %s: %s", config_slug, exc)
            return instructions, []

        servers = ((config.get("mcp_config") or {}).get("servers") or {})
        server = servers.get("aw-gateway") or {}
        server_url = str(server.get("url") or "").strip()
        if not server_url or not server_url.rstrip("/").endswith("/mcp/aw-crispal"):
            log.warning("realtime MCP disabled: agent config is not scoped to aw-crispal")
            return instructions, []
        tool = {
            "type": "mcp",
            "server_label": "aw_crispal",
            "server_description": "Scoped Crispal production agents for store operations.",
            "server_url": server_url,
            "headers": dict(server.get("headers") or {}),
            "allowed_tools": REALTIME_CRISPAL_TOOLS,
            # The scoped gateway owns its own approval policy. Asking OpenAI
            # for a second approval would stall a live phone call because
            # there is no visual approval surface on the audio channel.
            "require_approval": "never",
        }
        return instructions, [tool]

    async def connect(self) -> None:
        if not self.settings.openai_api_key:
            raise CallAgentError("OpenAI Realtime voice requires `openai_api_key`")
        import websockets
        started = time.monotonic()
        self.ws = await websockets.connect(
            f"wss://api.openai.com/v1/realtime?model={self.settings.realtime_model}",
            additional_headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            open_timeout=15, close_timeout=5, max_size=8 * 1024 * 1024)
        websocket_ms = round((time.monotonic() - started) * 1000, 1)
        instruction_started = time.monotonic()
        instructions, tools = await self._control_plane()
        instructions_ms = round((time.monotonic() - instruction_started) * 1000, 1)
        await self.ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                "output_modalities": ["audio"],
                "tools": tools,
                "tool_choice": "auto",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": self.settings.stt_openai_model,
                                          "language": self.settings.default_voice_lang.split("-")[0]},
                        "turn_detection": {
                            "type": "server_vad", "create_response": True,
                            "interrupt_response": True,
                            "silence_duration_ms": int(self.settings.speech_pause_ms),
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": self.settings.realtime_voice,
                    },
                },
            },
        }))
        self._connected_at = time.monotonic()
        self._latency("session_connected", websocket_ms=websocket_ms,
                      control_plane_ms=instructions_ms,
                      model=self.settings.realtime_model,
                      mcp_tools=len(tools))
        self.reader_task = asyncio.create_task(self._read_events())
        self.sender_task = asyncio.create_task(self._send_audio())

    async def append_audio(self, pcm8k: bytes) -> None:
        if self.ws is None or not pcm8k or self._closed:
            return
        try:
            self._input_queue.put_nowait(pcm8k)
        except asyncio.QueueFull:
            self._input_dropped += 1
            if self._input_dropped == 1 or self._input_dropped % 50 == 0:
                self._latency("input_queue_drop", dropped=self._input_dropped)

    async def _send_audio(self) -> None:
        while True:
            pcm8k = await self._input_queue.get()
            if pcm8k is None:
                return
            started = time.monotonic()
            await self.ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(
                    self._input_resampler.convert(pcm8k)).decode("ascii"),
            }))
            elapsed_ms = (time.monotonic() - started) * 1000
            if elapsed_ms > 20:
                self._latency("openai_send_slow", duration_ms=round(elapsed_ms, 1),
                              queue_depth=self._input_queue.qsize())

    async def interrupt(self) -> None:
        if self.ws is None or not self.response_active:
            return
        # The session's server VAD owns model cancellation via
        # interrupt_response=True. Local detection only stops the exact
        # response's playback immediately; sending a second unscoped
        # response.cancel here can race and cancel the replacement response.
        self._latency("local_playback_interrupt", response_id=self.response_id)

    async def _read_events(self) -> None:
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                kind = event.get("type", "")
                if kind == "input_audio_buffer.speech_started":
                    self._speech_started_at = time.monotonic()
                    self._latency("openai_speech_started",
                                  audio_start_ms=event.get("audio_start_ms", ""))
                elif kind == "input_audio_buffer.speech_stopped":
                    self._speech_stopped_at = time.monotonic()
                    self._latency("openai_speech_stopped",
                                  speech_ms=round((self._speech_stopped_at -
                                                   self._speech_started_at) * 1000, 1)
                                  if self._speech_started_at else "")
                elif kind == "response.created":
                    self.response_active = True
                    self.response_id = str((event.get("response") or {}).get("id") or "")
                    self._response_created_at = time.monotonic()
                    self._first_audio_seen = False
                    self._latency("response_created")
                elif kind in {"response.output_audio.delta", "response.audio.delta"}:
                    audio = base64.b64decode(event.get("delta") or "")
                    if audio:
                        self.output_response_id = str(
                            event.get("response_id") or self.response_id)
                        if not self._first_audio_seen:
                            self._first_audio_seen = True
                            self._latency(
                                "first_openai_audio",
                                model_first_audio_ms=round(
                                    (time.monotonic() - self._response_created_at) * 1000, 1)
                                if self._response_created_at else "",
                                estimated_voice_end_to_audio_ms=round(
                                    self.settings.speech_pause_ms +
                                    (time.monotonic() - self._speech_stopped_at) * 1000,
                                    1) if self._speech_stopped_at else "")
                        await self.emit(self._output_resampler.convert(audio))
                elif kind == "conversation.item.input_audio_transcription.completed":
                    self._last_transcript = str(event.get("transcript") or "").strip()
                    self._latency("transcript_completed",
                                  chars=len(self._last_transcript))
                elif kind in {"response.output_audio_transcript.delta",
                              "response.audio_transcript.delta"}:
                    response_id = str(event.get("response_id") or self.response_id)
                    self._reply_parts.setdefault(response_id, []).append(
                        str(event.get("delta") or ""))
                elif kind in {"response.output_audio_transcript.done",
                              "response.audio_transcript.done"}:
                    response_id = str(event.get("response_id") or self.response_id)
                    transcript = str(event.get("transcript") or "").strip()
                    if transcript:
                        self._reply_parts[response_id] = [transcript]
                elif kind == "response.done":
                    done_id = str((event.get("response") or {}).get("id") or "")
                    reply_id = done_id or self.response_id
                    if not done_id or done_id == self.response_id:
                        self.response_active = False
                        self.response_id = ""
                    self._latency("response_done")
                    reply = "".join(self._reply_parts.pop(reply_id, [])).strip()
                    # The input transcription completion can arrive just
                    # after response.done. Persist outside the reader loop so
                    # that event remains receivable and history keeps the
                    # spoken user turn paired with the answer.
                    async def persist(completed_reply: str):
                        await asyncio.sleep(0.25)
                        transcript = self._last_transcript
                        self._last_transcript = ""
                        if transcript or completed_reply:
                            self.store.append_text(
                                self.call_id, transcript=transcript,
                                agent_text=completed_reply)
                    task = asyncio.create_task(persist(reply))
                    self._persist_tasks.add(task)
                    task.add_done_callback(self._persist_tasks.discard)
                elif (kind == "conversation.item.done" and
                      (event.get("item") or {}).get("type") == "mcp_list_tools"):
                    item = event.get("item") or {}
                    self._latency("mcp_tools_loaded", count=len(item.get("tools") or []))
                elif kind == "response.mcp_call.in_progress":
                    self._latency("mcp_call_started", tool=event.get("name", ""))
                elif kind in {"response.mcp_call.completed", "response.mcp_call.failed"}:
                    self._latency("mcp_call_finished", status=kind.rsplit(".", 1)[-1],
                                  tool=event.get("name", ""))
                elif kind == "error":
                    detail = event.get("error") or {}
                    log.warning("Realtime call %s error: %s", self.call_id,
                                detail.get("message") or detail)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                log.warning("Realtime call %s closed: %s", self.call_id, exc)

    async def close(self) -> None:
        self._closed = True
        if self.sender_task is not None:
            self.sender_task.cancel()
            await asyncio.gather(self.sender_task, return_exceptions=True)
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self.reader_task is not None:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        if self._persist_tasks:
            await asyncio.gather(*self._persist_tasks, return_exceptions=True)
