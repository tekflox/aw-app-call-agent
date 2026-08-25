"""Standalone entrypoint — run the call agent without the aw-workspace
runtime:

    AW_AGENTS_PLATFORM_BASE=http://127.0.0.1:10014 \
    AW_AGENTS_PLATFORM_TOKEN=<jwt> \
    python -m call_agent_app                 # 127.0.0.1:9412

Then open http://127.0.0.1:9412/ — ``GET /`` redirects to the same
``/api/apps/call-agent/panel`` the window frames, so the UI, the WS protocol
and the paths are identical in both modes.

There is no ``IdentityGuard`` here (that is runtime machinery, not app code),
so this binds loopback by default. Settings come from the environment, since
there is no workspace config to read — see ``settings.py``.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .call_history import CallStore, default_data_dir
from .audio_socket import AudioSocketBridge
from .speech_pipeline import SipSpeechPipeline
from .realtime_voice import OpenAIRealtimeVoiceSession
from . import settings as settings_mod
from .routes import build_routes

SLUG = "call-agent"
DEFAULT_PORT = 9412
PANEL = f"/api/apps/{SLUG}/panel"
UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


def _container_config() -> dict:
    """Rehydrate the manifest-provided environment for the route layer."""
    mapping = {
        "agent_slug": "AW_CALL_AGENT_SLUG",
        "external_id": "AW_CALL_EXTERNAL_ID",
        "prompt_template": "AW_CALL_PROMPT_TEMPLATE",
        "default_voice_lang": "AW_CALL_DEFAULT_VOICE_LANG",
        "stt_provider": "AW_CALL_STT_PROVIDER",
        "stt_openai_model": "AW_CALL_STT_OPENAI_MODEL",
        "stt_realtime_model": "AW_CALL_STT_REALTIME_MODEL",
        "stt_realtime_delay": "AW_CALL_STT_REALTIME_DELAY",
        "tts_provider": "AW_CALL_TTS_PROVIDER",
        "tts_openai_model": "AW_CALL_TTS_OPENAI_MODEL",
        "tts_openai_voice": "AW_CALL_TTS_OPENAI_VOICE",
        "openai_api_key": "OPENAI_API_KEY",
        "voice_runtime": "AW_CALL_VOICE_RUNTIME",
        "realtime_model": "AW_CALL_REALTIME_MODEL",
        "realtime_voice": "AW_CALL_REALTIME_VOICE",
        "speech_pause_ms": "AW_CALL_SPEECH_PAUSE_MS",
        "poll_interval_seconds": "AW_CALL_POLL_INTERVAL",
        "max_poll_seconds": "AW_CALL_MAX_POLL_SECONDS",
        "agents_platform_base": "AW_AGENTS_PLATFORM_BASE",
        "agents_platform_token": "AW_AGENTS_PLATFORM_TOKEN",
        "sip_host": "SIP_HOST",
        "sip_port": "SIP_PORT",
        "sip_username": "SIP_USERNAME",
        "sip_password": "SIP_PASSWORD",
        "sip_public_number": "SIP_PUBLIC_NUMBER",
        "sip_caller_id": "SIP_CALLER_ID",
        "asterisk_ami_host": "ASTERISK_AMI_HOST",
        "asterisk_ami_secret": "ASTERISK_AMI_SECRET",
        "asterisk_audio_socket_host": "ASTERISK_AUDIO_SOCKET_HOST",
        "asterisk_audio_socket_port": "ASTERISK_AUDIO_SOCKET_PORT",
        "internal_sip_extension": "INTERNAL_SIP_EXTENSION",
        "internal_sip_password": "INTERNAL_SIP_PASSWORD",
        "call_agent_extension": "CALL_AGENT_EXTENSION",
        "sip_external_address": "SIP_EXTERNAL_ADDRESS",
    }
    cfg = {key: os.environ[name] for key, name in mapping.items()
           if os.environ.get(name) not in (None, "")}
    cfg["telephony_enabled"] = os.environ.get(
        "TELEPHONY_ENABLED", "").lower() in {"1", "true", "yes", "on"}
    return cfg


def build_standalone_app() -> FastAPI:
    store = CallStore(default_data_dir())
    pipeline = SipSpeechPipeline(lambda: settings_mod.resolve(_container_config()), store)
    def duplex_factory(call_id, emit):
        return OpenAIRealtimeVoiceSession(
            call_id, settings_mod.resolve(_container_config()), store, emit)
    effective = settings_mod.resolve(_container_config())
    bridge = AudioSocketBridge(
        store,
        host=os.environ.get("ASTERISK_AUDIO_SOCKET_HOST", "127.0.0.1"),
        port=int(os.environ.get("ASTERISK_AUDIO_SOCKET_PORT", "9019")),
        utterance_handler=pipeline.handle,
        utterance_streamer=pipeline.handle_stream,
        # Realtime owns transcription and turn detection. Opening the classic
        # observer here would create a second, unused OpenAI STT websocket.
        audio_observer=(None if effective.voice_runtime == "openai-realtime"
                        else pipeline),
        duplex_session_factory=(duplex_factory
                                if effective.voice_runtime == "openai-realtime"
                                else None),
        speech_pause_ms=int(float(os.environ.get("AW_CALL_SPEECH_PAUSE_MS", "1200"))),
        call_finished=pipeline.forget,
    )

    @asynccontextmanager
    async def lifespan(_app):
        await bridge.start()
        try:
            yield
        finally:
            await bridge.stop()
            store.close()

    app = FastAPI(title="call-agent (standalone)", lifespan=lifespan)
    # Integrated mode gets ``/api/apps/<slug>/ui/<file>`` from core, and an app
    # may not mount routes there itself (core reserves the prefix and would
    # refuse the whole sub-app). Standalone has no core, so serve it here —
    # BEFORE the sub-app mount, since Starlette matches mounts in order and
    # the shorter prefix would otherwise swallow it.
    tier2 = os.environ.get("AW_TIER2_CONTAINER") == "1"
    if UI_DIST.is_dir() and not tier2:
        app.mount(f"/api/apps/{SLUG}/ui", StaticFiles(directory=UI_DIST), name="ui")
    routes = build_routes(config_provider=_container_config, call_store=store,
                          audio_bridge_provider=lambda: bridge)
    app.mount("/" if tier2 else f"/api/apps/{SLUG}", routes)

    @app.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse(PANEL)

    return app


app = build_standalone_app()


def main() -> None:
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    host = os.environ.get("AW_APP_HOST", "127.0.0.1")
    print(f"call-agent standalone → http://{host}:{port}{PANEL}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
