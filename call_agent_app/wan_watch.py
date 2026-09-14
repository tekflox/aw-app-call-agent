"""Notice when the home IP behind the WAN SIP hostname moves.

The internet-facing transport advertises a literal address in Via, Contact and
the SDP, because PJSIP resolves ``external_signaling_address`` exactly once at
load.  A home connection's public address is a lease, and when it renews
Asterisk keeps advertising the dead one.

That failure is completely silent: the softphone still shows **Registered**,
because REGISTER travels to an address the phone resolves for itself, and only
the media and the responses go to nowhere.  Nothing logs, nothing alerts, and
the symptom the user reports is "calls have no audio" — days after the cause.

So this re-resolves the hostname on a timer, and when it has moved re-runs the
renderer and asks Asterisk to reload PJSIP.  The last check is reported through
``GET /telephony/wan-extension`` either way, so a stale address is visible in
Settings rather than inferred from a broken call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import time
from pathlib import Path

from .telephony import AsteriskAMI, TelephonyError, from_config

log = logging.getLogger("aw_apps.call_agent.wan_watch")

#: Written by ``container/render_asterisk.py`` beside the configs it renders.
STATE_FILE = "wan_state.json"

DEFAULT_INTERVAL = 300.0


def config_dir() -> Path:
    return Path(os.environ.get("ASTERISK_CONFIG_DIR", "/etc/asterisk"))


def rendered_state() -> dict:
    """What the running Asterisk was rendered with, or ``{}`` if unknown."""
    try:
        data = json.loads((config_dir() / STATE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 — a corrupt sidecar must not kill the app
        log.warning("could not read %s: %s", STATE_FILE, exc)
        return {}
    return data if isinstance(data, dict) else {}


class WanAddressWatch:
    """Resolve the WAN hostname on a timer; re-render Asterisk when it moves."""

    def __init__(self, config_provider, interval: float = DEFAULT_INTERVAL):
        self._config = config_provider
        self.interval = interval
        self.last_checked: float | None = None
        self.resolved_ip: str = ""
        self.error: str = ""
        self.reloads: int = 0
        self._task: asyncio.Task | None = None

    # ── what Settings shows ────────────────────────────────────────────────
    def status(self) -> dict:
        state = rendered_state()
        advertised = str(state.get("address") or "")
        hostname = str(state.get("hostname") or "")
        # Unknown is not the same as out of sync: before the first check there
        # is nothing to compare, and saying "in sync" then would be a guess.
        in_sync = None
        if advertised and self.resolved_ip:
            in_sync = self.resolved_ip == advertised
        return {
            "hostname": hostname,
            "advertised_ip": advertised,
            "resolved_ip": self.resolved_ip,
            "in_sync": in_sync,
            "last_checked": self.last_checked,
            "reloads": self.reloads,
            "error": self.error,
        }

    # ── one pass, also the unit of testing ─────────────────────────────────
    async def check_once(self) -> dict:
        state = rendered_state()
        if not state.get("enabled"):
            return self.status()
        hostname = str(state.get("hostname") or "")
        if not hostname:
            return self.status()
        try:
            self.resolved_ip = await asyncio.to_thread(socket.gethostbyname, hostname)
            self.error = ""
        except OSError as exc:
            # A resolver hiccup is not drift — keep the last good answer and
            # say why the check did not conclude.
            self.error = str(exc)
            log.warning("WAN hostname %s did not resolve: %s", hostname, exc)
            self.last_checked = time.time()
            return self.status()
        self.last_checked = time.time()
        if self.resolved_ip != str(state.get("address") or ""):
            log.warning("WAN address moved: %s now resolves to %s (was %s) "
                        "-- re-rendering Asterisk",
                        hostname, self.resolved_ip, state.get("address"))
            await self._rerender_and_reload()
        return self.status()

    async def _rerender_and_reload(self) -> None:
        script = Path(__file__).resolve().parent.parent / "container" / "render_asterisk.py"
        try:
            result = await asyncio.to_thread(
                subprocess.run, ["python", str(script)],
                capture_output=True, text=True, timeout=30)
        except Exception as exc:  # noqa: BLE001 — never kill the watch loop
            self.error = f"re-render failed: {exc}"
            log.exception("WAN re-render failed")
            return
        if result.returncode != 0:
            self.error = (result.stderr or result.stdout or "").strip()[:400]
            log.error("WAN re-render exited %s: %s", result.returncode, self.error)
            return
        try:
            await AsteriskAMI(from_config(self._config() or {})).reload_pjsip()
            self.reloads += 1
            self.error = ""
        except TelephonyError as exc:
            # Rendered but not applied is worth saying out loud: the file on
            # disk and the running PBX now disagree until the next restart.
            self.error = f"rendered, but PJSIP reload failed: {exc}"
            log.error("WAN re-render applied to disk but PJSIP reload failed: %s", exc)

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a watch that dies is worse than one that errs
                log.exception("WAN address check failed")
            await asyncio.sleep(self.interval)

    async def start(self) -> None:
        if self._task is None and rendered_state().get("enabled"):
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
