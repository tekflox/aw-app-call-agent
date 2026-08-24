"""Asterisk/SIP control plane for the Call Agent.

The browser call and a PSTN call eventually use the same agent pipeline, but
they enter it differently.  This module deliberately owns only the telephony
control plane: validate settings, render the Asterisk configuration, inspect
AMI, originate calls and hang them up.  Media is a separate concern and can be
added without teaching the UI or routes how SIP works.

Nothing starts or changes Asterisk merely by importing this module.  Telephony
is disabled by default, and every mutating operation checks that flag.  That
makes the app safe to install before a Zadarma account exists.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any


class TelephonyError(RuntimeError):
    """A configuration or AMI error safe to surface in the panel."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def normalise_e164(value: str) -> str:
    """Return a strict E.164 number or raise a user-facing error."""
    number = re.sub(r"[\s().-]", "", _clean(value))
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", number):
        raise TelephonyError("number must be E.164, for example +351300000000")
    return number


@dataclass(frozen=True)
class TelephonySettings:
    enabled: bool = False
    provider: str = "zadarma"
    sip_host: str = "sip.zadarma.com"
    sip_port: int = 5060
    sip_username: str = ""
    sip_password: str = ""
    public_number: str = ""
    caller_id: str = ""
    ami_host: str = "127.0.0.1"
    ami_port: int = 5038
    ami_username: str = "call-agent"
    ami_secret: str = ""
    audio_socket_host: str = "127.0.0.1"
    audio_socket_port: int = 9019

    @property
    def configured(self) -> bool:
        return bool(self.sip_username and self.sip_password and self.public_number)

    @property
    def ready(self) -> bool:
        return self.enabled and self.configured and bool(self.ami_secret)

    def missing(self) -> list[str]:
        fields = []
        if not self.sip_username:
            fields.append("sip_username")
        if not self.sip_password:
            fields.append("sip_password")
        if not self.public_number:
            fields.append("public_number")
        if not self.ami_secret:
            fields.append("asterisk_ami_secret")
        return fields


def from_config(config: dict | None) -> TelephonySettings:
    cfg = config or {}

    def positive_int(name: str, default: int) -> int:
        try:
            value = int(cfg.get(name, default))
        except (TypeError, ValueError):
            return default
        return value if 0 < value < 65536 else default

    return TelephonySettings(
        enabled=bool(cfg.get("telephony_enabled", False)),
        provider=_clean(cfg.get("sip_provider")) or "zadarma",
        sip_host=_clean(cfg.get("sip_host")) or "sip.zadarma.com",
        sip_port=positive_int("sip_port", 5060),
        sip_username=_clean(cfg.get("sip_username")),
        sip_password=_clean(cfg.get("sip_password")),
        public_number=_clean(cfg.get("sip_public_number")),
        caller_id=_clean(cfg.get("sip_caller_id")),
        ami_host=_clean(cfg.get("asterisk_ami_host")) or "127.0.0.1",
        ami_port=positive_int("asterisk_ami_port", 5038),
        ami_username=_clean(cfg.get("asterisk_ami_username")) or "call-agent",
        ami_secret=_clean(cfg.get("asterisk_ami_secret")),
        audio_socket_host=_clean(cfg.get("asterisk_audio_socket_host")) or "127.0.0.1",
        audio_socket_port=positive_int("asterisk_audio_socket_port", 9019),
    )


def render_asterisk_config(settings: TelephonySettings, *, redact: bool = False) -> dict[str, str]:
    """Render the complete minimal Asterisk config for a Zadarma trunk.

    The preview endpoint always uses ``redact=True``.  The unredacted form is
    intended for the bundled container's startup script when that runtime is
    activated.
    """
    s = settings
    user = s.sip_username or "YOUR_SIP_LOGIN"
    password = "***" if redact and s.sip_password else (s.sip_password or "YOUR_SIP_PASSWORD")
    number = s.public_number or "YOUR_PORTUGAL_NUMBER"
    ami_secret = "***" if redact and s.ami_secret else (s.ami_secret or "GENERATE_A_SECRET")
    caller = s.caller_id or number

    pjsip = "[global]\nuser_agent=AW Call Agent\n\n[transport-udp]\n"
    pjsip += "type=transport\nprotocol=udp\nbind=0.0.0.0\n\n"
    pjsip += f"[zadarma-auth]\ntype=auth\nauth_type=userpass\nusername={user}\npassword={password}\n\n"
    pjsip += f"[zadarma-aor]\ntype=aor\ncontact=sip:{s.sip_host}:{s.sip_port}\nqualify_frequency=30\n\n"
    pjsip += (
        f"[zadarma]\ntype=endpoint\ntransport=transport-udp\ncontext=from-zadarma\n"
        "disallow=all\nallow=alaw,ulaw\ndirect_media=no\nrtp_symmetric=yes\n"
        "force_rport=yes\nrewrite_contact=yes\n"
        "outbound_auth=zadarma-auth\naors=zadarma-aor\n"
        f"from_user={user}\nfrom_domain={s.sip_host}\n\n"
        f"[zadarma-identify]\ntype=identify\nendpoint=zadarma\nmatch={s.sip_host}\n\n"
        f"[zadarma-reg]\ntype=registration\ntransport=transport-udp\n"
        f"outbound_auth=zadarma-auth\nserver_uri=sip:{s.sip_host}:{s.sip_port}\n"
        f"client_uri=sip:{user}@{s.sip_host}\ncontact_user={user}\nretry_interval=60\n"
    )

    extensions = f"""[from-zadarma]
exten => _X.,1,NoOp(Inbound Zadarma call to ${{EXTEN}})
 same => n,Answer()
 same => n,Set(CALL_ID=${{UUID()}})
 same => n,AudioSocket(${{CALL_ID}},{s.audio_socket_host}:{s.audio_socket_port})
 same => n,Hangup()

[call-agent-outbound]
exten => _X.,1,NoOp(Call Agent outbound call to ${{EXTEN}})
 same => n,Set(CALLERID(num)={caller})
 same => n,Dial(PJSIP/${{EXTEN}}@zadarma,60)
 same => n,Hangup()

[from-call-agent]
exten => s,1,NoOp(Connect outbound call to Call Agent AudioSocket)
 same => n,Set(CALL_ID=${{IF($["${{CALL_ID}}"=""]?${{UUID()}}:${{CALL_ID}})}})
 same => n,AudioSocket(${{CALL_ID}},{s.audio_socket_host}:{s.audio_socket_port})
 same => n,Hangup()
"""
    manager = f"""[general]
enabled=yes
port={s.ami_port}
bindaddr={s.ami_host}

[{s.ami_username}]
secret={ami_secret}
read=system,call,log,verbose,command,agent,user,config,dtmf,reporting
write=system,call,command,agent,user,config,originate
"""
    return {
        "pjsip.conf": pjsip,
        "extensions.conf": extensions,
        "manager.conf": manager,
    }


def _parse_ami(block: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in block.replace("\r\n", "\n").split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip().lower()] = value.strip()
    return result


class AsteriskAMI:
    """Small dependency-free AMI client; one TCP connection per action."""

    def __init__(self, settings: TelephonySettings, timeout: float = 4.0):
        self.settings = settings
        self.timeout = timeout

    async def _action(self, action: dict[str, str]) -> dict[str, str]:
        s = self.settings
        if not s.ami_secret:
            raise TelephonyError("asterisk_ami_secret is not configured")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(s.ami_host, s.ami_port), self.timeout)
        except Exception as exc:
            raise TelephonyError(f"Asterisk AMI is unreachable at {s.ami_host}:{s.ami_port}: {exc}") from exc
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n"), self.timeout)
            payload = {
                "Action": "Login",
                "Username": s.ami_username,
                "Secret": s.ami_secret,
                "Events": "off",
            }
            writer.write("".join(f"{k}: {v}\r\n" for k, v in payload.items()).encode() + b"\r\n")
            await writer.drain()
            login = _parse_ami((await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.timeout)).decode(errors="replace"))
            if login.get("response", "").lower() != "success":
                raise TelephonyError(login.get("message") or "Asterisk AMI login failed")

            writer.write("".join(f"{k}: {v}\r\n" for k, v in action.items()).encode() + b"\r\n")
            await writer.drain()
            response = _parse_ami((await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.timeout)).decode(errors="replace"))
            if response.get("response", "").lower() == "error":
                raise TelephonyError(response.get("message") or "Asterisk rejected the action")
            return response
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def ping(self) -> dict[str, str]:
        return await self._action({"Action": "Ping"})

    async def originate(self, number: str, caller_id: str = "") -> dict[str, str]:
        return await self.originate_call(number, caller_id=caller_id)

    async def originate_call(self, number: str, caller_id: str = "",
                             call_id: str = "") -> dict[str, str]:
        destination = normalise_e164(number).lstrip("+")
        action = {
            "Action": "Originate",
            "Channel": f"Local/{destination}@call-agent-outbound",
            "Context": "from-call-agent",
            "Exten": "s",
            "Priority": "1",
            "Async": "true",
            "Variable": ",".join(x for x in (
                f"CALL_AGENT_DESTINATION={destination}",
                f"CALL_ID={call_id}" if call_id else "",
            ) if x),
        }
        if caller_id:
            action["CallerID"] = caller_id
        return await self._action(action)

    async def hangup(self, channel: str) -> dict[str, str]:
        channel = _clean(channel)
        if not channel or len(channel) > 160 or "\n" in channel or "\r" in channel:
            raise TelephonyError("invalid Asterisk channel")
        return await self._action({"Action": "Hangup", "Channel": channel})
