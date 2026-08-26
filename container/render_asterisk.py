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

def resolve_external_address(value: str) -> str:
    """Return an explicit address or discover this app's public IPv4.

    Cloud workspaces have a deterministic per-app hostname.  Resolving it at
    container start makes a brand-new install usable from an external
    softphone without baking one workspace's IP into the image.  Self-hosted
    installs can always override this with ``sip_external_address``.
    """
    if value and value.lower() != "auto":
        return value
    workspace_slug = env("AW_WORKSPACE_SLUG")
    public_suffix = env("AW_WORKSPACE_PUBLIC_SUFFIX", "workspace.aw.tekflox.com")
    if not workspace_slug:
        return ""
    hostname = f"call-agent.app.{workspace_slug}.{public_suffix}"
    try:
        return socket.gethostbyname(hostname)
    except OSError:
        return ""


external_address = resolve_external_address(external_address)
transport_extra = ""
if external_address:
    transport_extra = (
        f"external_signaling_address={external_address}\n"
        f"external_media_address={external_address}\n"
        "local_net=127.0.0.0/8\n"
        "local_net=10.0.0.0/8\n"
        "local_net=172.16.0.0/12\n"
        "local_net=192.168.0.0/16\n"
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
if zadarma_enabled:
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
"""
if zadarma_enabled:
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
