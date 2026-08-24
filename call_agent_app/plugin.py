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
from .audio_socket import AudioSocketBridge
from .call_history import CallStore, default_data_dir
from .telephony import from_config as telephony_from_config

log = logging.getLogger("aw_apps.call_agent")


class CallAgentAppPlugin:
    async def activate(self, ctx) -> None:
        self._ctx = ctx
        self.call_store = CallStore(default_data_dir())
        self.audio_bridge = None
        ctx.routes.register(routes_mod.build_routes(
            config_provider=lambda: getattr(ctx, "config", {}) or {},
            call_store=self.call_store,
            audio_bridge_provider=lambda: self.audio_bridge))
        await self._sync_audio_bridge()

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
        await self._sync_audio_bridge()
        s = settings_mod.resolve(getattr(ctx, "config", {}) or {})
        log.info("call-agent settings saved: agent=%s target=%s credentials=%s",
                 s.agent_slug, s.target_slug, s.source)

    async def deactivate(self) -> None:
        if self.audio_bridge is not None:
            await self.audio_bridge.stop()
            self.audio_bridge = None
        self.call_store.close()
        log.info("call-agent deactivated")

    async def _sync_audio_bridge(self) -> None:
        s = telephony_from_config(getattr(self._ctx, "config", {}) or {})
        # Keep the loopback bridge available before a provider exists so the
        # real framing, storage and playback path can be tested internally.
        # Its default loopback bind does not expose a public service.
        if (self.audio_bridge is not None
                and self.audio_bridge.host == s.audio_socket_host
                and self.audio_bridge.port == s.audio_socket_port):
            return
        if self.audio_bridge is not None:
            await self.audio_bridge.stop()
        self.audio_bridge = AudioSocketBridge(
            self.call_store, host=s.audio_socket_host, port=s.audio_socket_port)
        try:
            await self.audio_bridge.start()
        except OSError as exc:
            self.audio_bridge = None
            log.error("could not start AudioSocket bridge on %s:%s: %s",
                      s.audio_socket_host, s.audio_socket_port, exc)
