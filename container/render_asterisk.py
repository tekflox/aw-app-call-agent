"""Render the internal PBX config from app Settings environment variables."""
from __future__ import annotations

import os
import re
import socket
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


extension = env("INTERNAL_SIP_EXTENSION", "101")
password = env("INTERNAL_SIP_PASSWORD")
agent_extension = env("CALL_AGENT_EXTENSION", "700")
external_address = env("SIP_EXTERNAL_ADDRESS")
ami_secret = env("ASTERISK_AMI_SECRET")
if not password or not ami_secret:
    raise SystemExit("internal SIP password and AMI secret must be configured")
if not extension.isdigit() or not agent_extension.isdigit():
    raise SystemExit("internal SIP extensions must contain digits only")
for name, value in {"SIP password": password, "AMI secret": ami_secret}.items():
    if not re.fullmatch(r"[^\r\n]{12,200}", value):
        raise SystemExit(f"{name} must contain 12-200 characters without newlines")

lan_trunk_enabled = env("LAN_TRUNK_ENABLED").lower() in {"1", "true", "yes", "on"}
lan_trunk_host = env("LAN_TRUNK_HOST")
lan_trunk_port = env("LAN_TRUNK_PORT", "5061")
lan_trunk_user = env("LAN_TRUNK_USERNAME")
lan_trunk_password = env("LAN_TRUNK_PASSWORD")
lan_trunk_caller_id = env("LAN_TRUNK_CALLER_ID")
# Inbound INVITEs do not necessarily arrive from the gateway's own LAN address.
# On a macOS podman host they are source-NATed to the container network gateway,
# so matching the gateway IP never fires.  Blank keeps the textbook behaviour.
lan_trunk_match = env("LAN_TRUNK_IDENTIFY_MATCH") or lan_trunk_host


def resolve_external_address(value: str, *, allow_public_lookup: bool = True) -> str:
    """Return an explicit address or discover this app's public IPv4.

    Cloud workspaces have a deterministic per-app hostname.  Resolving it at
    container start makes a brand-new install usable from an external
    softphone without baking one workspace's IP into the image.  Self-hosted
    installs can always override this with ``sip_external_address``.

    ``allow_public_lookup`` turns that discovery off.  A host whose trunk is a
    box on the LAN has no use for the workspace's public edge address, and
    writing it into the SDP would point every media stream at the wrong machine.
    """
    if value and value.lower() != "auto":
        return value
    if not allow_public_lookup:
        return ""
    workspace_slug = env("AW_WORKSPACE_SLUG")
    public_suffix = env("AW_WORKSPACE_PUBLIC_SUFFIX", "workspace.aw.tekflox.com")
    if not workspace_slug:
        return ""
    hostname = f"call-agent.app.{workspace_slug}.{public_suffix}"
    try:
        return socket.gethostbyname(hostname)
    except OSError:
        return ""


external_address = resolve_external_address(
    external_address, allow_public_lookup=not lan_trunk_enabled)
transport_extra = ""
if external_address:
    local_nets = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    if lan_trunk_enabled:
        # The LAN trunk sits in 192.168/16 but does not share a network with
        # Asterisk: it reaches the container through the host's NAT.  Calling
        # it "local" makes PJSIP advertise the container's own bind address
        # (10.x) instead of external_media_address, and the RTP goes nowhere
        # while the signalling still completes with a 200 OK.
        local_nets.remove("192.168.0.0/16")
    transport_extra = (
        f"external_signaling_address={external_address}\n"
        f"external_media_address={external_address}\n"
        + "".join(f"local_net={net}\n" for net in local_nets)
    )

pjsip = f"""[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
{transport_extra}
[{extension}-auth]
type=auth
auth_type=userpass
username={extension}
password={password}

[{extension}]
type=aor
max_contacts=2
remove_existing=yes

[{extension}]
type=endpoint
transport=transport-udp
context=internal
disallow=all
; Keep the internal test leg on PCMU for consistent NATed softphone audio.
allow=ulaw
auth={extension}-auth
aors={extension}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
tos_audio=ef
cos_audio=5
"""

zadarma_enabled = env("TELEPHONY_ENABLED").lower() in {"1", "true", "yes", "on"}
sip_user = env("SIP_USERNAME")
sip_password = env("SIP_PASSWORD")
sip_host = env("SIP_HOST", "sip.zadarma.com")
sip_port = env("SIP_PORT", "5060")
public_number = env("SIP_PUBLIC_NUMBER")
caller_id = env("SIP_CALLER_ID") or public_number
# Telephony being switched on does not by itself mean the provider trunk is
# wanted: an install whose only line is the LAN trunk still has to enable
# telephony.  Demand the provider's credentials only when this install is
# actually trying to use it — otherwise a LAN-only host cannot start at all.
zadarma_requested = zadarma_enabled and (
    bool(sip_user or sip_password or public_number) or not lan_trunk_enabled
)
if zadarma_requested:
    for name, value in {
        "SIP username": sip_user, "SIP password": sip_password,
        "public number": public_number,
    }.items():
        if not value or "\n" in value or "\r" in value:
            raise SystemExit(f"{name} is required when external telephony is enabled")
    pjsip += f"""
[zadarma-auth]
type=auth
auth_type=userpass
username={sip_user}
password={sip_password}

[zadarma-aor]
type=aor
contact=sip:{sip_host}:{sip_port}

[zadarma]
type=endpoint
transport=transport-udp
context=from-zadarma
disallow=all
allow=alaw,ulaw
outbound_auth=zadarma-auth
aors=zadarma-aor
from_user={sip_user}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
tos_audio=ef
cos_audio=5

[zadarma-identify]
type=identify
endpoint=zadarma
match={sip_host}

[zadarma-registration]
type=registration
transport=transport-udp
outbound_auth=zadarma-auth
server_uri=sip:{sip_host}:{sip_port}
client_uri=sip:{sip_user}@{sip_host}
contact_user={sip_user}
retry_interval=60
"""

if lan_trunk_enabled:
    for name, value in {
        "LAN trunk host": lan_trunk_host,
        "LAN trunk username": lan_trunk_user,
        "LAN trunk password": lan_trunk_password,
    }.items():
        if not value or "\n" in value or "\r" in value:
            raise SystemExit(f"{name} is required when the LAN SIP trunk is enabled")
    # Deliberately no type=registration: the gateway answers unregistered
    # calls (its "Ans Call Without Reg" mode), and nothing here registers
    # anywhere.  Adding one would only give the trunk a second way to fail.
    pjsip += f"""
[lan-trunk-auth]
type=auth
auth_type=userpass
username={lan_trunk_user}
password={lan_trunk_password}

[lan-trunk-aor]
type=aor
contact=sip:{lan_trunk_host}:{lan_trunk_port}
qualify_frequency=60

[lan-trunk]
type=endpoint
transport=transport-udp
context=from-lan-trunk
disallow=all
allow=ulaw,alaw
outbound_auth=lan-trunk-auth
aors=lan-trunk-aor
from_user={lan_trunk_user}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
tos_audio=ef
cos_audio=5

[lan-trunk-identify]
type=identify
endpoint=lan-trunk
match={lan_trunk_match}
"""

extensions = f"""[internal]
exten => {agent_extension},1,NoOp(Internal call to AW Call Agent)
 same => n,Answer()
 same => n,Playtones(440/250)
 same => n,Wait(0.3)
 same => n,StopPlaytones()
 same => n,Set(JITTERBUFFER(adaptive)=200,,60)
 same => n,Set(CALL_ID=${{UUID()}})
 same => n,AudioSocket(${{CALL_ID}},127.0.0.1:9019)
 same => n,Verbose(1,RTP_QOS ${{CHANNEL(rtpqos,audio,all)}})
 same => n,Hangup()

; The agent's own leg of an AMI-originated outbound call.  telephony.py's
; Originate names this context, so it has to exist here in the runtime config
; and not only in the settings-panel preview -- without it every outbound call
; fails the moment the far end answers.
[from-call-agent]
exten => s,1,NoOp(Connect outbound call to the Call Agent audio bridge)
 same => n,Set(CALL_ID=${{IF($["${{CALL_ID}}"=""]?${{UUID()}}:${{CALL_ID}})}})
 same => n,Set(JITTERBUFFER(adaptive)=200,,60)
 same => n,AudioSocket(${{CALL_ID}},127.0.0.1:9019)
 same => n,Verbose(1,RTP_QOS ${{CHANNEL(rtpqos,audio,all)}})
 same => n,Hangup()
"""
if lan_trunk_enabled:
    # The analog line supplies its own caller ID unless one is configured.
    caller_id_line = ""
    if lan_trunk_caller_id:
        caller_id_line = f" same => n,Set(CALLERID(num)={lan_trunk_caller_id})\n"
    extensions += f"""

[from-lan-trunk]
exten => _X.,1,Goto(s,1)
exten => s,1,NoOp(Inbound PSTN call from the LAN trunk)
 same => n,Answer()
 same => n,Set(JITTERBUFFER(adaptive)=200,,60)
 same => n,Set(CALL_ID=${{UUID()}})
 same => n,AudioSocket(${{CALL_ID}},127.0.0.1:9019)
 same => n,Verbose(1,RTP_QOS ${{CHANNEL(rtpqos,audio,all)}})
 same => n,Hangup()

[call-agent-lan-outbound]
exten => _X.,1,NoOp(Outbound PSTN call via the LAN trunk to ${{EXTEN}})
{caller_id_line} same => n,Dial(PJSIP/${{EXTEN}}@lan-trunk,60)
 same => n,Hangup()
"""
if zadarma_requested:
    extensions += f"""

[from-zadarma]
exten => _X.,1,Answer()
 same => n,Playtones(440/250)
 same => n,Wait(0.3)
 same => n,StopPlaytones()
 same => n,Set(JITTERBUFFER(adaptive)=200,,60)
 same => n,Set(CALL_ID=${{UUID()}})
 same => n,AudioSocket(${{CALL_ID}},127.0.0.1:9019)
 same => n,Verbose(1,RTP_QOS ${{CHANNEL(rtpqos,audio,all)}})
 same => n,Hangup()

[call-agent-outbound]
exten => _X.,1,Set(CALLERID(num)={caller_id})
 same => n,Dial(PJSIP/${{EXTEN}}@zadarma,60)
 same => n,Hangup()
"""

manager = f"""[general]
enabled=yes
port=5038
bindaddr=127.0.0.1

[call-agent]
secret={ami_secret}
read=system,call,log,verbose,command,agent,user,config,dtmf,reporting
write=system,call,command,agent,user,config,originate
"""

rtp = "[general]\nrtpstart=10000\nrtpend=10100\n"
root = Path(env("ASTERISK_CONFIG_DIR", "/etc/asterisk"))
root.mkdir(parents=True, exist_ok=True)
for name, content in {
    "pjsip.conf": pjsip,
    "extensions.conf": extensions,
    "manager.conf": manager,
    "rtp.conf": rtp,
}.items():
    (root / name).write_text(content, encoding="utf-8")
