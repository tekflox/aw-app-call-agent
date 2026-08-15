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
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .routes import build_routes

SLUG = "call-agent"
DEFAULT_PORT = 9412
PANEL = f"/api/apps/{SLUG}/panel"
UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


def build_standalone_app() -> FastAPI:
    app = FastAPI(title="call-agent (standalone)")
    # Integrated mode gets ``/api/apps/<slug>/ui/<file>`` from core, and an app
    # may not mount routes there itself (core reserves the prefix and would
    # refuse the whole sub-app). Standalone has no core, so serve it here —
    # BEFORE the sub-app mount, since Starlette matches mounts in order and
    # the shorter prefix would otherwise swallow it.
    if UI_DIST.is_dir():
        app.mount(f"/api/apps/{SLUG}/ui", StaticFiles(directory=UI_DIST), name="ui")
    app.mount(f"/api/apps/{SLUG}", build_routes())

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
