"""Entrypoint referenced by aw-app.json's runtime.entrypoint
("call_agent_app.plugin:CallAgentAppPlugin").

All this app needs from the runtime is a mount point: ``ctx.routes.register``
puts the sub-app from ``routes.py`` at ``/api/apps/call-agent`` behind
``IdentityGuard``. Settings are read lazily through a provider closure over
``ctx`` rather than snapshotted here, so saving new settings re-points the
next call immediately instead of at the next workspace restart —
``on_config_saved`` therefore only has to log.
"""

from __future__ import annotations

import logging

from . import routes as routes_mod
from . import settings as settings_mod

log = logging.getLogger("aw_apps.call_agent")


class CallAgentAppPlugin:
    async def activate(self, ctx) -> None:
        self._ctx = ctx
        ctx.routes.register(routes_mod.build_routes(
            config_provider=lambda: getattr(ctx, "config", {}) or {}))

        s = settings_mod.resolve(getattr(ctx, "config", {}) or {})
        if not s.agents_platform_base:
            # Not fatal — every route still answers, and Settings is the
            # place to fix it. Say so once, loudly, instead of letting the
            # first call fail with a connection error.
            log.warning(
                "call-agent activated WITHOUT an agents-platform base URL "
                "(source=%s) — calls will fail until it is set in Settings, "
                "or the Agents Platform Runners app is installed.", s.source)
        log.info("call-agent activated: agent=%s target=%s credentials=%s",
                 s.agent_slug, s.target_slug, s.source)

    async def on_config_saved(self, ctx) -> None:
        s = settings_mod.resolve(getattr(ctx, "config", {}) or {})
        log.info("call-agent settings saved: agent=%s target=%s credentials=%s",
                 s.agent_slug, s.target_slug, s.source)

    async def deactivate(self) -> None:
        log.info("call-agent deactivated")
