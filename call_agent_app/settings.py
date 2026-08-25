"""Where a call's settings come from.

This app needs two things it does not own: the agents-platform base URL and
an identity JWT for it. Both already exist in a workspace that runs agents —
the **Agents Platform Runners** app holds them, mints nothing new, and is
installed on every workspace where a call could possibly work. Asking the
user to paste a JWT into a second app just to make the first call is a
setup step with no decision in it, so the resolution order is:

1. this app's own config (``agents_platform_base`` / ``agents_platform_token``)
   — set these to point calls at a *different* platform than the runners app;
2. environment (``AW_AGENTS_PLATFORM_BASE`` / ``AW_AGENTS_PLATFORM_TOKEN``) —
   how standalone mode is configured, since there is no workspace config there;
3. the runners app's saved config snapshot on disk.

(3) is a read of another app's file, which the capability model has no verb
for — there is no ``config:read:<app>`` today, only ``config:extend:<app>``
for writing. It is best-effort and read-only: the snapshot lives at
``$AW_WORKSPACE_HOME/app-config/agents-platform-runners.json`` (written by
core so an app's settings survive a delete/reinstall), a missing or
unreadable file just leaves the field blank, and ``GET /settings`` reports
which source won so a blank one is diagnosable. If the capability lands
later, swap this function's body for it — nothing else in the app reads
these values directly.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .service import CallSettings

log = logging.getLogger("aw_apps.call_agent.settings")

RUNNERS_APP_ID = "agents-platform-runners"


def _workspace_home() -> Path:
    return Path(os.environ.get("AW_WORKSPACE_HOME")
                or os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
                + "/.aw-workspace")


def _runners_config() -> dict:
    """Best-effort read of the Agents Platform Runners app's saved config."""
    path = _workspace_home() / "app-config" / f"{RUNNERS_APP_ID}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}
    # Core snapshots either the bare config dict or wraps it under "config"
    # depending on version — accept both rather than guessing wrong silently.
    cfg = raw.get("config") if isinstance(raw.get("config"), dict) else raw
    return cfg if isinstance(cfg, dict) else {}


def _num(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def resolve(config: dict | None) -> CallSettings:
    """Build the effective ``CallSettings`` for this workspace."""
    cfg = config or {}
    defaults = CallSettings()

    base = (cfg.get("agents_platform_base") or "").strip()
    token = (cfg.get("agents_platform_token") or "").strip()
    source = "app-config"

    if not base or not token:
        env_base = (os.environ.get("AW_AGENTS_PLATFORM_BASE") or "").strip()
        env_token = (os.environ.get("AW_AGENTS_PLATFORM_TOKEN") or "").strip()
        if env_base or env_token:
            base = base or env_base
            token = token or env_token
            source = "env"

    if not base or not token:
        runners = _runners_config()
        inherited_base = (runners.get("agents_platform_base") or "").strip()
        inherited_token = (runners.get("agents_platform_token") or "").strip()
        if inherited_base or inherited_token:
            base = base or inherited_base
            token = token or inherited_token
            source = f"inherited:{RUNNERS_APP_ID}"

    if not base and not token:
        source = "unset"

    return CallSettings(
        agents_platform_base=base,
        agents_platform_token=token,
        agent_slug=(cfg.get("agent_slug") or defaults.agent_slug).strip(),
        external_id=(cfg.get("external_id") or defaults.external_id).strip(),
        prompt_template=cfg.get("prompt_template") or defaults.prompt_template,
        default_voice_lang=(cfg.get("default_voice_lang")
                            or defaults.default_voice_lang).strip(),
        stt_provider=(cfg.get("stt_provider") or defaults.stt_provider).strip(),
        stt_openai_model=(cfg.get("stt_openai_model")
                          or defaults.stt_openai_model).strip(),
        stt_realtime_model=(cfg.get("stt_realtime_model")
                            or defaults.stt_realtime_model).strip(),
        stt_realtime_delay=(cfg.get("stt_realtime_delay")
                            or defaults.stt_realtime_delay).strip(),
        tts_provider=(cfg.get("tts_provider") or defaults.tts_provider).strip(),
        tts_openai_model=(cfg.get("tts_openai_model")
                          or defaults.tts_openai_model).strip(),
        tts_openai_voice=(cfg.get("tts_openai_voice")
                          or defaults.tts_openai_voice).strip(),
        openai_api_key=(cfg.get("openai_api_key") or "").strip(),
        speech_pause_ms=_num(cfg.get("speech_pause_ms"), defaults.speech_pause_ms),
        poll_interval_seconds=_num(cfg.get("poll_interval_seconds"),
                                   defaults.poll_interval_seconds),
        max_poll_seconds=_num(cfg.get("max_poll_seconds"), defaults.max_poll_seconds),
        source=source,
    )
