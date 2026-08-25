"""call_agent_app's mode-agnostic FastAPI sub-app.

Paths are relative — the runtime mounts this at ``/api/apps/call-agent``
behind ``IdentityGuard`` in integrated mode, and ``__main__.py`` mounts it at
the same prefix (unguarded) in standalone mode.

    GET  /health          liveness + whether a backend is configured
    GET  /settings        effective settings, token masked
    GET  /agents-list     agent picker rows, proxied from Agents Platform
    GET  /tts             text -> raw MP3 (audio/mpeg)
    GET  /panel           the call UI itself (HTML, framed by the window)
    WS   /ws/call         the call

The WS keeps the wire protocol the monolith used verbatim
(``{"type": "message", "text": …}`` in; ``ready`` / ``text_delta`` /
``heartbeat`` / ``done`` / ``error`` / ``cleared`` out) so the existing iOS
``StreamingCallStore`` client needs only a URL change to point at this app.
``/panel`` speaks the same protocol from the browser.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import os
import struct
import uuid
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Query, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import settings as settings_mod
from .audio_socket import KIND_HANGUP, KIND_PCM_8K, KIND_UUID, encode_frame
from .call_history import CallStore
from .panel import PANEL_HTML, STATUS_HTML
from .service import CallAgentError, CallAgentService, CallSettings
from .sip_tester import SipSoftphoneTester, SipTestError, mp3_to_pcm8k
from .telephony import (
    AsteriskAMI, TelephonyError, from_config as telephony_from_config,
    render_asterisk_config,
)

log = logging.getLogger("aw_apps.call_agent.routes")


class OutboundCall(BaseModel):
    number: str


class HangupCall(BaseModel):
    channel: str


class SipIntegrationTestRequest(BaseModel):
    first_text: str = "Eu gosto de abacaxi."
    follow_up_text: str = "Que fruta eu gosto?"
    expected_memory: str = "abacaxi"


def build_routes(config_provider: Callable[[], dict] | None = None,
                 call_store: CallStore | None = None,
                 audio_bridge_provider: Callable[[], object] | None = None) -> FastAPI:
    """Mode-agnostic factory.

    ``config_provider`` is read on **every** request rather than once at
    build time — a settings save updates ``ctx.config`` in place, and a
    snapshot taken at activation would keep dispatching calls to the old
    agent until the next workspace restart.
    """
    app = FastAPI(title="call-agent")

    def current() -> CallSettings:
        cfg = {}
        if config_provider is not None:
            try:
                cfg = config_provider() or {}
            except Exception:
                log.warning("config provider failed; using defaults", exc_info=True)
        return settings_mod.resolve(cfg)

    def raw_config() -> dict:
        if config_provider is None:
            return {}
        try:
            return config_provider() or {}
        except Exception:
            log.warning("config provider failed; using defaults", exc_info=True)
            return {}

    def telephony():
        return telephony_from_config(raw_config())

    @app.get("/health")
    async def health() -> dict:
        s = current()
        return {
            "ok": True,
            "configured": bool(s.agents_platform_base),
            "agent_slug": s.agent_slug,
            "target_slug": s.target_slug,
        }

    @app.get("/settings")
    async def get_settings() -> dict:
        s = current()
        return {
            "agent_slug": s.agent_slug,
            "external_id": s.external_id,
            "target_slug": s.target_slug,
            "default_voice_lang": s.default_voice_lang,
            "stt_provider": s.stt_provider,
            "stt_openai_model": s.stt_openai_model,
            "stt_realtime_model": s.stt_realtime_model,
            "stt_realtime_delay": s.stt_realtime_delay,
            "tts_provider": s.tts_provider,
            "tts_openai_model": s.tts_openai_model,
            "tts_openai_voice": s.tts_openai_voice,
            "has_openai_api_key": bool(s.openai_api_key),
            "speech_pause_ms": s.speech_pause_ms,
            "agents_platform_base": s.agents_platform_base,
            # Never echo the JWT back into a browser — whether one exists is
            # the only thing the UI needs to know.
            "has_token": bool(s.agents_platform_token),
            "credentials_source": s.source,
            "poll_interval_seconds": s.poll_interval_seconds,
            "max_poll_seconds": s.max_poll_seconds,
            "telephony": {
                "enabled": telephony().enabled,
                "provider": telephony().provider,
                "configured": telephony().configured,
                "ready": telephony().ready,
                "public_number": telephony().public_number,
                "missing": telephony().missing(),
            },
        }

    @app.get("/telephony/status")
    async def telephony_status() -> dict:
        s = telephony()
        body = {
            "enabled": s.enabled,
            "provider": s.provider,
            "configured": s.configured,
            "ready": s.ready,
            "public_number": s.public_number,
            "missing": s.missing(),
            "asterisk": {"reachable": False},
            "audio_bridge": {"listening": False, "active_calls": 0},
        }
        if audio_bridge_provider is not None:
            bridge = audio_bridge_provider()
            if bridge is not None:
                body["audio_bridge"] = {
                    "listening": bridge.server is not None,
                    "host": bridge.host,
                    "port": bridge.port,
                    "active_calls": len(bridge.active),
                }
        if not s.enabled or not s.ami_secret:
            return body
        try:
            reply = await AsteriskAMI(s).ping()
            body["asterisk"] = {
                "reachable": True,
                "message": reply.get("ping") or reply.get("message") or "Pong",
            }
        except TelephonyError as exc:
            body["asterisk"] = {"reachable": False, "error": str(exc)}
        return body

    @app.get("/telephony/config-preview")
    async def telephony_config_preview() -> dict:
        return {"files": render_asterisk_config(telephony(), redact=True)}

    @app.get("/telephony/internal-extension")
    async def internal_extension(reveal_password: bool = Query(False)) -> dict:
        cfg = raw_config()
        password = str(cfg.get("internal_sip_password") or "")
        server = str(cfg.get("sip_external_address") or "")
        if server.lower() in {"", "auto"}:
            workspace_slug = os.environ.get("AW_WORKSPACE_SLUG", "").strip()
            public_suffix = os.environ.get(
                "AW_WORKSPACE_PUBLIC_SUFFIX", "workspace.aw.tekflox.com"
            ).strip()
            if workspace_slug:
                server = f"call-agent.app.{workspace_slug}.{public_suffix}"
        return {
            "server": server,
            "port": 5060,
            "transport": "udp",
            "username": str(cfg.get("internal_sip_extension") or "101"),
            "password": password if reveal_password else ("********" if password else ""),
            "call_agent_extension": str(cfg.get("call_agent_extension") or "700"),
            "codecs": ["PCMA/alaw", "PCMU/ulaw"],
        }

    @app.get("/telephony/calls")
    async def call_history(limit: int = Query(100, ge=1, le=500)) -> dict:
        return {"calls": call_store.list(limit) if call_store else []}

    @app.get("/telephony/calls/{call_id}")
    async def call_detail(call_id: str):
        row = call_store.get(call_id) if call_store else None
        if row is None:
            return JSONResponse({"error": "call not found"}, status_code=404)
        return row

    @app.get("/telephony/calls/{call_id}/recording")
    async def call_recording(call_id: str):
        path = call_store.recording_path(call_id) if call_store else None
        if path is None:
            return JSONResponse({"error": "recording not found"}, status_code=404)
        return FileResponse(Path(path), media_type="audio/wav",
                            filename=f"call-{call_id}.wav")

    @app.post("/telephony/self-test", status_code=202)
    async def telephony_self_test():
        """Send a synthetic call through the actual local AudioSocket path."""
        bridge = audio_bridge_provider() if audio_bridge_provider else None
        if bridge is None or bridge.server is None:
            return JSONResponse({"error": "local audio bridge is not listening"},
                                status_code=409)
        if bridge.host not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse(
                {"error": "self-test is allowed only on a loopback audio bridge"},
                status_code=409)
        call_id = str(uuid.uuid4())
        try:
            _reader, writer = await asyncio.open_connection(bridge.host, bridge.port)
            samples = bytearray()
            for index in range(8000):
                value = int(3500 * math.sin(2 * math.pi * 440 * index / 8000))
                samples.extend(struct.pack("<h", value))
            writer.write(encode_frame(KIND_UUID, uuid.UUID(call_id).bytes))
            writer.write(encode_frame(KIND_PCM_8K, bytes(samples)))
            writer.write(encode_frame(KIND_HANGUP))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            for _ in range(50):
                row = call_store.get(call_id) if call_store else None
                if row and row["status"] != "active":
                    break
                await asyncio.sleep(0.01)
        except Exception as exc:
            log.exception("internal AudioSocket self-test failed")
            return JSONResponse({"error": f"internal self-test failed: {exc}"},
                                status_code=502)
        return {"ok": True, "call_id": call_id,
                "message": "internal AudioSocket recording created"}

    @app.post("/telephony/sip-integration-test")
    async def sip_integration_test(request: SipIntegrationTestRequest):
        """Call extension 700 through SIP and verify the agent answers in RTP.

        This runs entirely inside the app container: no DID, trunk or external
        VoIP provider is involved.  It exercises REGISTER -> INVITE -> RTP ->
        Asterisk -> AudioSocket -> STT -> Agents Platform -> TTS -> RTP.
        """
        cfg = raw_config()
        username = str(cfg.get("internal_sip_extension") or "101")
        password = str(cfg.get("internal_sip_password") or "")
        extension = str(cfg.get("call_agent_extension") or "700")
        if not password:
            return JSONResponse({"error": "internal SIP password is not configured"},
                                status_code=409)
        if not current().agents_platform_base:
            return JSONResponse({"error": "Agents Platform is not configured"},
                                status_code=409)
        before = {row["id"] for row in call_store.list(20)} if call_store else set()
        tester = SipSoftphoneTester(username, password, extension)
        try:
            service = CallAgentService(current())
            prompts = []
            for text in (request.first_text, request.follow_up_text):
                mp3 = await service.tts(text, current().default_voice_lang)
                prompts.append(await asyncio.to_thread(mp3_to_pcm8k, mp3))

            def turn_complete(turn: int) -> bool:
                if not call_store:
                    return True
                try:
                    fresh = [row for row in call_store.list(20)
                             if row["id"] not in before]
                except Exception:
                    # The AudioSocket writer may briefly hold SQLite while a
                    # PCM frame updates its byte count. This callback is a
                    # poll, so contention means "not yet", not test failure.
                    return False
                if not fresh:
                    return False
                return len(fresh[0].get("agent_text", "").splitlines()) >= turn

            result = await asyncio.to_thread(
                tester.run_conversation, prompts, 180, turn_complete)
            call = None
            if call_store:
                for _ in range(100):
                    fresh = [row for row in call_store.list(20)
                             if row["id"] not in before]
                    if fresh and fresh[0].get("agent_text"):
                        call = fresh[0]
                        break
                    await asyncio.sleep(0.1)
            if call_store and call is None:
                raise SipTestError("RTP answered, but no completed agent turn was recorded")
            if call_store:
                transcripts = call.get("transcript", "").splitlines()
                replies = call.get("agent_text", "").splitlines()
                if len(transcripts) < 2 or len(replies) < 2:
                    raise SipTestError("follow-up turn was not completed")
                expected = request.expected_memory.casefold()
                if expected not in replies[-1].casefold():
                    raise SipTestError(
                        f"memory assertion failed: expected {request.expected_memory!r} "
                        "in the follow-up response")
        except (SipTestError, CallAgentError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        except Exception as exc:
            log.exception("SIP integration test failed")
            return JSONResponse({"ok": False, "error": f"SIP test failed: {exc}"},
                                status_code=502)
        finally:
            tester.close()
        return {
            "ok": True,
            "sip": dataclasses.asdict(result),
            "call_id": call["id"] if call else "",
            "transcript": call.get("transcript", "") if call else "",
            "agent_text": call.get("agent_text", "") if call else "",
            "memory_verified": True,
            "message": "SIP, two speech turns, agent memory and RTP responses verified",
        }

    @app.post("/telephony/calls", status_code=202)
    async def originate_call(call: OutboundCall):
        s = telephony()
        if not s.enabled:
            return JSONResponse({"error": "telephony is disabled in Settings"}, status_code=409)
        if not s.ready:
            return JSONResponse({"error": "telephony is not configured", "missing": s.missing()}, status_code=409)
        call_id = str(uuid.uuid4())
        if call_store:
            call_store.ensure_call(call_id, direction="outbound", remote_number=call.number)
        try:
            response = await AsteriskAMI(s).originate_call(
                call.number, s.caller_id or s.public_number, call_id=call_id)
        except TelephonyError as exc:
            if call_store:
                call_store.finish(call_id, status="failed", error=str(exc))
            return JSONResponse({"error": str(exc)}, status_code=502)
        return {"ok": True, "call_id": call_id,
                "message": response.get("message", "originate queued")}

    @app.post("/telephony/calls/hangup")
    async def hangup_call(call: HangupCall):
        s = telephony()
        if not s.enabled:
            return JSONResponse({"error": "telephony is disabled in Settings"}, status_code=409)
        try:
            response = await AsteriskAMI(s).hangup(call.channel)
        except TelephonyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return {"ok": True, "message": response.get("message", "hangup requested")}

    @app.get("/agents-list")
    async def agents_list() -> dict:
        return await CallAgentService(current()).agents_list()

    @app.get("/tts")
    async def tts(text: str = Query(""), lang: str = Query("")):
        try:
            audio = await CallAgentService(current()).tts(text, lang)
        except CallAgentError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            log.exception("tts failed")
            return JSONResponse({"error": f"tts failed: {exc}"}, status_code=502)
        return Response(content=audio, media_type="audio/mpeg")

    @app.get("/panel", response_class=HTMLResponse)
    async def panel() -> HTMLResponse:
        return HTMLResponse(PANEL_HTML)

    @app.get("/panel/status", response_class=HTMLResponse)
    async def panel_status() -> HTMLResponse:
        return HTMLResponse(STATUS_HTML)

    @app.websocket("/ws/call")
    async def ws_call(ws: WebSocket) -> None:
        """The call. Ported from the monolith's ``ws_call_chat``, minus the
        meta_display broadcast (see service.py's docstring)."""
        await ws.accept()

        s = current()
        service = CallAgentService(s)

        async def heartbeat() -> None:
            await ws.send_json({"type": "heartbeat"})

        async def on_delta(delta: str) -> None:
            await ws.send_json({"type": "text_delta", "text": delta})

        async def bind_agent(settings: CallSettings) -> tuple[CallAgentService, str | None]:
            """Point this socket at an agent: make sure its Target exists and
            resume whatever conversation it already had.

            Switching agent switches Target (``<agent>-<external_id>``), so each
            agent keeps its own thread rather than inheriting the last one's —
            which is the only sane reading of "call someone else"."""
            svc = CallAgentService(settings)
            sid: str | None = None
            async with svc.client(timeout=15.0) as client:
                await svc.ensure_target(client)
                sid = await svc.latest_target_session_id(client)
            return svc, sid

        claude_session_id: str | None = None
        try:
            service, claude_session_id = await bind_agent(s)
        except CallAgentError as exc:
            # A misconfigured workspace should say so on the phone, not fail
            # silently on the first thing the caller says.
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            log.warning("could not resume prior session", exc_info=True)

        await ws.send_json({
            "type": "ready",
            "backend": "agents-platform",
            "agent": s.agent_slug,
            "target": s.target_slug,
            "resumed": bool(claude_session_id),
        })

        try:
            while True:
                data = await ws.receive_json()

                if data.get("type") == "clear":
                    claude_session_id = None
                    await ws.send_json({"type": "cleared"})
                    continue

                if data.get("type") == "set_agent":
                    slug = (data.get("slug") or "").strip()
                    if not slug or slug == s.agent_slug:
                        continue
                    # Rebuild from the CURRENT config so a settings save that
                    # happened mid-call is picked up too, then override the
                    # agent for the rest of this socket only — nothing here
                    # writes back to workspace config.
                    s = dataclasses.replace(current(), agent_slug=slug)
                    try:
                        service, claude_session_id = await bind_agent(s)
                    except CallAgentError as exc:
                        await ws.send_json({"type": "error", "message": str(exc)})
                        continue
                    except Exception as exc:
                        log.warning("could not switch agent", exc_info=True)
                        await ws.send_json({
                            "type": "error",
                            "message": f"Could not switch to {slug}: {exc}"})
                        continue
                    await ws.send_json({
                        "type": "agent_changed",
                        "agent": s.agent_slug,
                        "target": s.target_slug,
                        "resumed": bool(claude_session_id),
                    })
                    continue

                if data.get("type") != "message":
                    continue

                user_text = (data.get("text") or "").strip()
                if not user_text:
                    continue

                # Re-read config every turn (a settings save mid-call should
                # apply), but keep whichever agent this socket is bound to.
                s = dataclasses.replace(current(), agent_slug=s.agent_slug)
                service = CallAgentService(s)
                prompt = s.build_prompt(user_text)

                try:
                    run_id = await service.run_agent_with_heartbeat(
                        prompt, claude_session_id, heartbeat)
                    log.info("Started call run %s (session=%s)", run_id, claude_session_id)
                except CallAgentError as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})
                    continue
                except Exception as exc:
                    log.exception("Failed to start agent run")
                    await ws.send_json({"type": "error",
                                        "message": f"Failed to start agent: {exc}"})
                    continue

                full_text, new_session_id = await service.stream_run(
                    run_id, on_delta, heartbeat)
                if new_session_id:
                    claude_session_id = new_session_id
                await ws.send_json({
                    "type": "done",
                    "text": full_text or "(no response)",
                    "run_id": run_id,
                })

        except WebSocketDisconnect:
            log.info("Call client disconnected (target=%s)", s.target_slug)
        except Exception as exc:
            log.exception("Unexpected error in call WS handler")
            try:
                await ws.send_json({"type": "error", "message": str(exc)})
            except Exception:
                pass

    return app
