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

import logging
from typing import Callable

from fastapi import FastAPI, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from . import settings as settings_mod
from .panel import PANEL_HTML, STATUS_HTML
from .service import CallAgentError, CallAgentService, CallSettings

log = logging.getLogger("aw_apps.call_agent.routes")


def build_routes(config_provider: Callable[[], dict] | None = None) -> FastAPI:
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
            "agents_platform_base": s.agents_platform_base,
            # Never echo the JWT back into a browser — whether one exists is
            # the only thing the UI needs to know.
            "has_token": bool(s.agents_platform_token),
            "credentials_source": s.source,
            "poll_interval_seconds": s.poll_interval_seconds,
            "max_poll_seconds": s.max_poll_seconds,
        }

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

        claude_session_id: str | None = None
        try:
            async with service.client(timeout=15.0) as client:
                await service.ensure_target(client)
                claude_session_id = await service.latest_target_session_id(client)
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

                if data.get("type") != "message":
                    continue

                user_text = (data.get("text") or "").strip()
                if not user_text:
                    continue

                s = current()
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
