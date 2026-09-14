"""Render the internal PBX config from app Settings environment variables."""
from __future__ import annotations

import json
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

# The port Asterisk binds is deliberately allowed to differ from the port it
# advertises.  On a gvproxy host (macOS podman) a container's outbound UDP is
# dropped whenever its source port is also a published host port, so the
# transport binds a port nothing publishes and advertises the published one.
sip_bind_port = env("SIP_BIND_PORT", "5060")
sip_advertised_port = env("SIP_ADVERTISED_PORT") or sip_bind_port
for name, value in {"SIP bind port": sip_bind_port,
                    "SIP advertised port": sip_advertised_port}.items():
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise SystemExit(f"{name} must be a port number, got {value!r}")

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
lan_trunk_strip_prefix = env("LAN_TRUNK_STRIP_PREFIX")
lan_trunk_dial_prefix = env("LAN_TRUNK_DIAL_PREFIX")

# The internet-facing transport.  Everything about it is deliberately separate
# from the LAN trunk's: its own bind port, its own external address, its own
# endpoint selected by digest auth alone, and its own dialplan context.  The
# two never share a section, because the whole safety argument rests on a WAN
# packet being unable to reach anything the LAN trunk is trusted for.
wan_enabled = env("WAN_SIP_ENABLED").lower() in {"1", "true", "yes", "on"}
wan_bind_port = env("WAN_SIP_BIND_PORT", "5063")
wan_advertised_port = env("WAN_SIP_ADVERTISED_PORT") or wan_bind_port
wan_address_value = env("WAN_SIP_EXTERNAL_ADDRESS")
wan_user = env("WAN_SIP_USERNAME")
wan_password = env("WAN_SIP_PASSWORD")
wan_rtp_start = env("WAN_RTP_PORT_START", "10000")
wan_rtp_end = env("WAN_RTP_PORT_END", "10019")


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
if sip_advertised_port != sip_bind_port and not external_address:
    # PJSIP only rewrites the port alongside the address: with no external
    # address it advertises the container's own address AND the bind port.
    # Silently advertising a port nothing forwards to is the one outcome that
    # must not be possible, so refuse to start instead.
    raise SystemExit(
        f"SIP advertised port {sip_advertised_port} differs from the bind port "
        f"{sip_bind_port}, but no external address is configured -- Via and "
        f"Contact would advertise the unreachable bind port. "
        f"Set sip_external_address.")

def resolve_local_bind_address() -> str:
    """This container's own IPv4, or "" if it cannot be established.

    The WAN transport binds this instead of ``0.0.0.0``, and that is not
    cosmetic: ``CHANNEL(pjsip,local_addr)`` -- the dialplan's only unforgeable
    view of which socket a call arrived on -- reads the transport's bound
    address through ``pj_sockaddr_has_addr()``, which is FALSE for an "any"
    bind.  Measured on the shipped Asterisk 20.20.1 image: bound to
    ``0.0.0.0`` the function returns empty, bound to a real address it returns
    ``ip:port``.  So the concrete bind is what makes the security guard below
    able to see anything at all.
    """
    candidates = []
    try:
        candidates.append(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    try:
        # Whichever source address the kernel would use to leave this host.
        # 192.0.2.0/24 is TEST-NET-1: connecting a UDP socket sends nothing.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            candidates.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    for address in candidates:
        if not address or address.startswith("127."):
            continue
        try:
            check = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                check.bind((address, 0))
            finally:
                check.close()
        except OSError:
            continue
        return address
    return ""


def resolve_wan_address(value: str) -> str:
    """The literal IPv4 the WAN transport advertises, from a DDNS hostname.

    PJSIP resolves ``external_signaling_address`` once at load, so a hostname
    written straight into the config is a snapshot either way.  Resolving it
    here instead makes the snapshot explicit: the address is recorded next to
    the hostname it came from, and ``wan_watch`` can notice the day the home
    IP changes underneath it.  A literal IP resolves to itself.
    """
    try:
        return socket.gethostbyname(value)
    except OSError as exc:
        raise SystemExit(
            f"WAN SIP address {value!r} does not resolve ({exc}) -- Asterisk "
            f"would advertise nothing and every WAN call would have no audio.")


wan_address = ""
wan_bind_address = ""
if wan_enabled:
    for name, value in {
        "WAN SIP address": wan_address_value,
        "WAN SIP username": wan_user,
        "WAN SIP password": wan_password,
    }.items():
        if not value or "\n" in value or "\r" in value:
            raise SystemExit(f"{name} is required when internet SIP is enabled")
    # An internet-facing endpoint in front of a real PSTN line is a toll-fraud
    # target within hours of the port opening.  These are generated values
    # (x-generate in the manifest, which forbids a literal default), so a weak
    # one can only arrive by someone typing it in -- refuse it loudly here
    # rather than discover it on a phone bill.
    if not re.fullmatch(r"[A-Za-z0-9._~\-]{16,200}", wan_user):
        raise SystemExit(
            "WAN SIP username must be 16-200 unreserved URI characters -- a "
            "short or guessable user is the whole attack.")
    if not re.fullmatch(r"[^\r\n]{16,200}", wan_password):
        raise SystemExit("WAN SIP password must contain 16-200 characters")
    for name, value in {"WAN SIP bind port": wan_bind_port,
                        "WAN SIP advertised port": wan_advertised_port,
                        "WAN RTP start port": wan_rtp_start,
                        "WAN RTP end port": wan_rtp_end}.items():
        if not value.isdigit() or not 1 <= int(value) <= 65535:
            raise SystemExit(f"{name} must be a port number, got {value!r}")
    if int(wan_rtp_start) > int(wan_rtp_end):
        raise SystemExit("WAN RTP start port must not be above the end port")
    if wan_bind_port == sip_bind_port:
        raise SystemExit(
            f"WAN SIP bind port {wan_bind_port} collides with the LAN transport's "
            f"-- the dialplan tells a WAN call from a LAN one by that port.")
    wan_address = resolve_wan_address(wan_address_value)
    wan_bind_address = resolve_local_bind_address()
    if not wan_bind_address:
        # Without a concrete bind the dialplan cannot tell a call that arrived
        # on the internet socket from one that arrived on the LAN socket, and
        # the lan-trunk identify admits unauthenticated INVITEs.  Refusing to
        # start is the only honest option: the alternative is a PBX that looks
        # fine and answers the internet.
        raise SystemExit(
            "could not determine this container's own IP address, so the WAN "
            "transport cannot bind a concrete address -- refusing to open an "
            "internet SIP port the dialplan guard would be blind to.")

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
        f"external_signaling_port={sip_advertised_port}\n"
        f"external_media_address={external_address}\n"
        + "".join(f"local_net={net}\n" for net in local_nets)
    )

pjsip = f"""[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:{sip_bind_port}
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

if wan_enabled:
    # Deliberately NO local_net lines on this transport.  The LAN transport
    # lists 10.0.0.0/8, and a packet arriving through the host's published
    # port reaches this container with its source rewritten to the container's
    # own 10.x address -- so every internet caller would be classified local,
    # the external rewrite skipped, and Via/Contact/SDP would carry the
    # container's own address and bind port.  It presents as "Zoiper says
    # Registered and no call has any audio".
    #
    # Deliberately NO type=identify either, and that is the security argument:
    # identify has no transport or port awareness, it runs BEFORE
    # authentication, and a WAN packet is byte-identical in source address to
    # a LAN one.  An endpoint reachable only through auth= username matching
    # cannot be selected by an INVITE that carries no valid digest response.
    pjsip += f"""
[global]
type=global
; Blunt the scanners that find an open SIP port within hours of it opening.
unidentified_request_count=5
unidentified_request_period=5
unidentified_request_prune_interval=30

[transport-wan]
type=transport
protocol=udp
; A concrete address, not 0.0.0.0 -- see resolve_local_bind_address().  This
; is what lets the dialplan below see which socket a call came in on.
bind={wan_bind_address}:{wan_bind_port}
external_signaling_address={wan_address}
external_signaling_port={wan_advertised_port}
external_media_address={wan_address}

[wan-auth]
type=auth
auth_type=userpass
username={wan_user}
password={wan_password}

[wan]
type=aor
max_contacts=1
remove_existing=yes
; The OPTIONS keeps both the host NAT mapping and the carrier-NAT mapping
; warm.  Without it a call TO this handset has no path home between
; re-REGISTERs.
qualify_frequency=30

[wan]
type=endpoint
transport=transport-wan
context=from-wan
disallow=all
allow=ulaw,alaw
auth=wan-auth
aors=wan
direct_media=no
; The usual NAT pair, unlike the LAN trunk: replies must go back to the
; packet source, because that source is the host NAT's return path.
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
; The opposite of the usual NAT advice, and deliberate.  Packets from the
; gateway reach this container with their source rewritten to the container's
; own address, so force_rport would make Asterisk answer itself and
; rewrite_contact would rewrite the gateway's contact to us.  The gateway is
; told to answer the source port instead (Handle VIA rport / Send Resp To Src
; Port), which is what makes the reply ride the outbound flow home.
force_rport=no
rewrite_contact=no
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
    # The guard exists only while an internet socket does.  With the WAN
    # transport absent, container port 5063 has nothing listening and the only
    # SIP socket in this container is the LAN one, so there is nothing to tell
    # apart -- and the rendered config is byte-identical to the shipped one.
    wan_guard = ""
    if wan_enabled:
        wan_guard = (
            "; SECURITY.  lan-trunk is selected by type=identify, which matches a\n"
            "; SOURCE ADDRESS -- and measured on this host, a packet from the\n"
            "; internet and a packet from the gateway arrive at this container from\n"
            "; the SAME rewritten source (10.89.0.x, per-container, not per-origin).\n"
            "; Only the destination port differs, identify cannot see it, and it runs\n"
            "; BEFORE authentication.  So without this, one unauthenticated INVITE to\n"
            "; the open WAN port reaches an AI agent holding workspace tools and a\n"
            "; PSTN trunk.  The receiving socket is the one thing the wire cannot\n"
            "; forge (the Request-URI can, which is why it is not used here).\n"
            "; An empty local_addr means the LAN transport, which binds 0.0.0.0 --\n"
            "; pj_sockaddr_has_addr() reports nothing for an \"any\" bind.  The WAN\n"
            "; transport binds a concrete address exactly so it cannot be empty.\n"
            "; Cause 21 is what this Asterisk renders as 403 Forbidden -- measured;\n"
            "; the more obvious 57 comes out as 603 Decline.\n"
            " same => n,Set(AW_LOCAL_ADDR=${CHANNEL(pjsip,local_addr)})\n"
            f' same => n,Set(AW_SOCKET_PORT=${{IF($["${{AW_LOCAL_ADDR}}"=""]'
            f'?{sip_bind_port}:${{CUT(AW_LOCAL_ADDR,:,2)}})}})\n'
            f' same => n,ExecIf($["${{AW_SOCKET_PORT}}" != "{sip_bind_port}"]'
            f'?Verbose(1,REJECT INVITE claiming lan-trunk from socket '
            f'${{AW_LOCAL_ADDR}}))\n'
            f' same => n,ExecIf($["${{AW_SOCKET_PORT}}" != "{sip_bind_port}"]'
            f"?Hangup(21))\n")
    extensions += f"""

[from-lan-trunk]
exten => _X.,1,Goto(s,1)
exten => s,1,NoOp(Inbound PSTN call from the LAN trunk)
{wan_guard} same => n,Answer()
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

if wan_enabled:
    if not lan_trunk_enabled:
        raise SystemExit(
            "internet SIP needs the LAN SIP trunk enabled -- it is the only "
            "line a WAN call can be bridged to.")
    # An analog line rarely accepts E.164, and a WAN caller dials whatever
    # their address book holds, so the normalisation telephony.py does in
    # Python for an AMI-originated call has to happen here for a call that
    # arrives off the wire.
    strip_line = ""
    if lan_trunk_strip_prefix:
        n = len(lan_trunk_strip_prefix)
        strip_line = (
            f' same => n,ExecIf($["${{DEST:0:{n}}}" = "{lan_trunk_strip_prefix}"]'
            f'?Set(DEST=${{DEST:{n}}}))\n')
    dial_prefix_line = ""
    if lan_trunk_dial_prefix:
        dial_prefix_line = f' same => n,Set(DEST={lan_trunk_dial_prefix}${{DEST}})\n'
    extensions += f"""

; Reached only by the single digest-authenticated WAN endpoint.  Nothing else
; has this context, and no unauthenticated INVITE can select that endpoint.
[from-wan]
; The literal agent extension beats _X. on specificity, so dialling it reaches
; the agent instead of being sent down the PSTN line as a phone number.
exten => {agent_extension},1,Goto(internal,{agent_extension},1)
; Two patterns, because _X. does NOT match a leading "+" -- and E.164 is
; exactly what a softphone dials out of its address book, which is also the
; form the strip-prefix rule below exists to undo.
exten => _+X.,1,Set(DEST=${{EXTEN}})
 same => n,Goto(dial,1)
exten => _X.,1,Set(DEST=${{EXTEN}})
 same => n,Goto(dial,1)
exten => dial,1,NoOp(Outbound PSTN call from the WAN softphone to ${{DEST}})
{strip_line}{dial_prefix_line}{caller_id_line} same => n,Dial(PJSIP/${{DEST}}@lan-trunk,60)
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

# Every port Asterisk can allocate has to sit inside the range the host
# publishes and the router forwards, or the call that lands on the port past
# the end is silently one-way.  strictrtp is explicit rather than left to the
# default because the host publish is static in the manifest: those container
# ports stay reachable from the internet even with internet SIP switched off,
# so dropping media from anywhere but the negotiated peer is what actually
# keeps a LAN call from being injected into.
rtp = (f"[general]\nrtpstart={wan_rtp_start}\nrtpend={wan_rtp_end}\n"
       f"strictrtp=yes\nicesupport=no\n")
root = Path(env("ASTERISK_CONFIG_DIR", "/etc/asterisk"))
root.mkdir(parents=True, exist_ok=True)
for name, content in {
    "pjsip.conf": pjsip,
    "extensions.conf": extensions,
    "manager.conf": manager,
    "rtp.conf": rtp,
}.items():
    (root / name).write_text(content, encoding="utf-8")

# What the WAN transport actually advertises, next to the hostname it came
# from.  The home IP is dynamic; PJSIP resolved it once and will keep
# advertising the dead address after the lease renews, which presents as
# "Registered, every call silent" with nothing logged anywhere. wan_watch
# reads this to notice the drift and re-render.
(root / "wan_state.json").write_text(json.dumps({
    "enabled": wan_enabled,
    "hostname": wan_address_value,
    "address": wan_address,
    "bind_port": wan_bind_port,
    "advertised_port": wan_advertised_port,
}), encoding="utf-8")
