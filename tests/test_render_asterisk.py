import json
import runpy
import socket
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "container" / "render_asterisk.py"


def _render(monkeypatch, tmp_path, external="auto", lan_trunk=None):
    monkeypatch.setenv("ASTERISK_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("INTERNAL_SIP_PASSWORD", "internal-secret-123")
    monkeypatch.setenv("ASTERISK_AMI_SECRET", "ami-secret-value-123")
    monkeypatch.setenv("SIP_EXTERNAL_ADDRESS", external)
    monkeypatch.setenv("AW_WORKSPACE_SLUG", "fresh-workspace")
    for name, value in (lan_trunk or {}).items():
        monkeypatch.setenv(name, value)
    runpy.run_path(str(SCRIPT))
    return (tmp_path / "pjsip.conf").read_text()


LAN_TRUNK_ENV = {
    "LAN_TRUNK_ENABLED": "true",
    "LAN_TRUNK_HOST": "192.168.1.240",
    "LAN_TRUNK_PORT": "5061",
    "LAN_TRUNK_USERNAME": "pstn",
    "LAN_TRUNK_PASSWORD": "ptsn",
}


def _section(config, name, kind="endpoint"):
    """The body of one pjsip.conf section, up to where the next one starts.

    One name can label more than one section — the softphone extension names
    both its aor and its endpoint — so the type disambiguates.
    """
    bodies = (b.split("\n[", 1)[0] for b in config.split(f"[{name}]\n")[1:])
    return next(b for b in bodies if f"type={kind}" in b)


def test_auto_external_address_resolves_public_app_hostname(monkeypatch, tmp_path):
    seen = []

    def resolve(hostname):
        seen.append(hostname)
        return "203.0.113.42"

    monkeypatch.setattr(socket, "gethostbyname", resolve)
    config = _render(monkeypatch, tmp_path)

    assert seen == ["call-agent.app.fresh-workspace.workspace.aw.tekflox.com"]
    assert "external_signaling_address=203.0.113.42" in config
    assert "external_media_address=203.0.113.42" in config
    assert "local_net=172.16.0.0/12" in config


def test_explicit_external_address_does_not_query_dns(monkeypatch, tmp_path):
    monkeypatch.setattr(
        socket, "gethostbyname",
        lambda _hostname: (_ for _ in ()).throw(AssertionError("unexpected DNS lookup")),
    )
    config = _render(monkeypatch, tmp_path, "198.51.100.9")
    assert "external_media_address=198.51.100.9" in config


def test_softphone_media_uses_dialplan_jitter_buffer_and_audio_qos(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path)
    extensions = (tmp_path / "extensions.conf").read_text()

    assert "jitterbuffer=yes" not in config
    assert "tos_audio=ef" in config
    assert "cos_audio=5" in config
    assert "Set(JITTERBUFFER(adaptive)=200,,60)" in extensions
    assert "CHANNEL(rtpqos,audio,all)" in extensions


def test_outbound_originate_context_exists_in_the_runtime_config(monkeypatch, tmp_path):
    """telephony.py Originates into from-call-agent; it must be rendered here.

    It used to exist only in the settings-panel preview renderer, so every
    AMI-originated call had nowhere to land once the far end answered.
    """
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    _render(monkeypatch, tmp_path)
    extensions = (tmp_path / "extensions.conf").read_text()

    assert "[from-call-agent]" in extensions
    assert "exten => s,1," in extensions
    assert "AudioSocket(" in extensions


def test_lan_trunk_renders_endpoint_identify_and_both_dialplan_legs(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path, "192.168.1.73", LAN_TRUNK_ENV)
    extensions = (tmp_path / "extensions.conf").read_text()

    assert "[lan-trunk]" in config
    assert "contact=sip:192.168.1.240:5061" in config
    assert "username=pstn" in config
    assert "password=ptsn" in config
    assert "context=from-lan-trunk" in config
    assert "[lan-trunk-identify]" in config
    assert "match=192.168.1.240" in config
    # No registration: the gateway answers unregistered calls.
    assert "[lan-trunk-registration]" not in config
    assert "[from-lan-trunk]" in extensions
    assert "Dial(PJSIP/${EXTEN}@lan-trunk,60)" in extensions


def test_lan_trunk_identify_can_match_a_natted_source_address(monkeypatch, tmp_path):
    """Inbound packets arrive from the container-network gateway, not the device."""
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(
        monkeypatch, tmp_path, "192.168.1.73",
        {**LAN_TRUNK_ENV, "LAN_TRUNK_IDENTIFY_MATCH": "10.88.0.5"})

    assert "match=10.88.0.5" in config
    assert "match=192.168.1.240" not in config


def test_lan_trunk_answers_the_source_port_rather_than_rewriting_it(monkeypatch, tmp_path):
    """The usual NAT pair is backwards for a gateway reached through our own NAT.

    Inbound packets arrive with the source rewritten to this container's own
    address, so force_rport would make Asterisk send its 100/180/200 to itself
    and rewrite_contact would point the gateway's contact at us.
    """
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path, "192.168.1.73", LAN_TRUNK_ENV)
    lan_trunk = _section(config, "lan-trunk")

    assert "force_rport=no" in lan_trunk
    assert "rewrite_contact=no" in lan_trunk
    # Symmetric RTP is unaffected — the media path never depended on the Via.
    assert "rtp_symmetric=yes" in lan_trunk


def test_softphone_endpoint_keeps_the_usual_nat_pair(monkeypatch, tmp_path):
    """Only the LAN trunk is special; a real remote softphone still needs both."""
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path, "192.168.1.73", LAN_TRUNK_ENV)
    softphone = _section(config, "101")

    assert "force_rport=yes" in softphone
    assert "rewrite_contact=yes" in softphone


def test_transport_binds_one_port_and_advertises_another(monkeypatch, tmp_path):
    """On a gvproxy host the bind port must not be the published host port.

    Outbound UDP from a source port that is also published is dropped, so the
    transport binds a port nothing publishes and advertises the published one.
    """
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path, "192.168.1.73",
                     {**LAN_TRUNK_ENV, "SIP_BIND_PORT": "5062",
                      "SIP_ADVERTISED_PORT": "5060"})
    transport = _section(config, "transport-udp", kind="transport")

    assert "bind=0.0.0.0:5062" in transport
    assert "external_signaling_port=5060" in transport
    assert "external_signaling_address=192.168.1.73" in transport


def test_advertising_a_different_port_without_an_external_address_is_fatal(
        monkeypatch, tmp_path):
    """PJSIP only rewrites the port alongside the address.

    With no external address it advertises the container's own address and the
    BIND port, so Via/Contact would name a port nothing forwards to. Verified
    against a live Asterisk 20: the rewrite is suppressed entirely.
    """
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    with pytest.raises(SystemExit):
        _render(monkeypatch, tmp_path, "auto",
                {**LAN_TRUNK_ENV, "SIP_BIND_PORT": "5062",
                 "SIP_ADVERTISED_PORT": "5060"})


def test_ports_default_to_5060_on_both_sides(monkeypatch, tmp_path):
    """An install that sets neither keeps the pre-existing single-port shape."""
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path, "192.168.1.73", LAN_TRUNK_ENV)
    transport = _section(config, "transport-udp", kind="transport")

    assert "bind=0.0.0.0:5060" in transport
    assert "external_signaling_port=5060" in transport


def test_a_non_numeric_port_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    with pytest.raises(SystemExit):
        _render(monkeypatch, tmp_path, "192.168.1.73",
                {**LAN_TRUNK_ENV, "SIP_BIND_PORT": "not-a-port"})


def test_the_provider_registrar_port_is_not_the_bind_port(monkeypatch, tmp_path):
    """SIP_PORT belongs to the Zadarma registrar; SIP_BIND_PORT is ours."""
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path, "198.51.100.9",
                     {"TELEPHONY_ENABLED": "true", "SIP_USERNAME": "u",
                      "SIP_PASSWORD": "p", "SIP_PUBLIC_NUMBER": "+351300000000",
                      "SIP_PORT": "5070", "SIP_BIND_PORT": "5062"})

    assert "bind=0.0.0.0:5062" in _section(config, "transport-udp", kind="transport")
    assert "contact=sip:sip.zadarma.com:5070" in config


def test_lan_trunk_keeps_the_gateway_off_local_net(monkeypatch, tmp_path):
    """192.168/16 as local_net makes PJSIP advertise the container's own IP."""
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path, "192.168.1.73", LAN_TRUNK_ENV)

    assert "external_media_address=192.168.1.73" in config
    assert "local_net=192.168.0.0/16" not in config
    assert "local_net=172.16.0.0/12" in config


def test_lan_trunk_does_not_resolve_the_public_workspace_hostname(monkeypatch, tmp_path):
    """'auto' would write the workspace edge IP into the SDP on a LAN host."""
    monkeypatch.setattr(
        socket, "gethostbyname",
        lambda _hostname: (_ for _ in ()).throw(AssertionError("unexpected DNS lookup")),
    )
    config = _render(monkeypatch, tmp_path, "auto", LAN_TRUNK_ENV)

    assert "external_media_address" not in config


def test_telephony_on_with_only_a_lan_trunk_does_not_demand_provider_creds(
        monkeypatch, tmp_path):
    """A LAN-only install still has to switch telephony on.

    TELEPHONY_ENABLED is the telephony master switch AND the provider-trunk
    gate. Treating them as one thing made the container exit on
    "SIP username is required when external telephony is enabled" and
    crash-loop, even though the provider trunk was never wanted.
    """
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path, "192.168.1.73",
                     {**LAN_TRUNK_ENV, "TELEPHONY_ENABLED": "true"})
    extensions = (tmp_path / "extensions.conf").read_text()

    assert "[lan-trunk]" in config
    assert "zadarma" not in config
    assert "from-zadarma" not in extensions


def test_provider_trunk_still_required_when_it_is_the_only_trunk(monkeypatch, tmp_path):
    """Telephony on, no LAN trunk, no credentials — still a hard error."""
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    with pytest.raises(SystemExit):
        _render(monkeypatch, tmp_path, "203.0.113.42", {"TELEPHONY_ENABLED": "true"})


def test_partially_configured_provider_trunk_still_errors_beside_a_lan_trunk(
        monkeypatch, tmp_path):
    """Half-entered provider credentials must not be silently ignored."""
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    with pytest.raises(SystemExit):
        _render(monkeypatch, tmp_path, "192.168.1.73",
                {**LAN_TRUNK_ENV, "TELEPHONY_ENABLED": "true",
                 "SIP_USERNAME": "someone"})


def test_lan_trunk_requires_its_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    with pytest.raises(SystemExit):
        _render(monkeypatch, tmp_path, "192.168.1.73",
                {**LAN_TRUNK_ENV, "LAN_TRUNK_PASSWORD": ""})


def test_lan_trunk_stays_off_unless_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path)
    extensions = (tmp_path / "extensions.conf").read_text()

    assert "lan-trunk" not in config
    assert "from-lan-trunk" not in extensions
    assert "local_net=192.168.0.0/16" in config


# ── the internet-facing transport ───────────────────────────────────────────
#
# Nothing in this file can see gvproxy or the router, so none of it proves the
# feature works on the wire. What it does prove is that the properties the
# whole safety argument rests on cannot be deleted without a test going red.

WAN_ENV = {
    **LAN_TRUNK_ENV,
    "WAN_SIP_ENABLED": "true",
    "WAN_SIP_EXTERNAL_ADDRESS": "home.example.com",
    "WAN_SIP_USERNAME": "3f1c9a02-5b6d-4e7f-8a90-1b2c3d4e5f60",
    "WAN_SIP_PASSWORD": "wan-password-with-enough-entropy",
    "WAN_SIP_BIND_PORT": "5063",
    "WAN_SIP_ADVERTISED_PORT": "45060",
}


def _fake_network(monkeypatch, *, wan_resolves_to="188.250.165.236",
                  own_address="10.89.0.101"):
    """Stand in for the container's own IP, which CI does not have.

    ``resolve_local_bind_address()`` asks the kernel for this box's address and
    then PROVES the answer by binding it, so a half-faked socket would let the
    renderer take a path it never takes in the container. Both halves are faked
    here; ``own_address=""`` reproduces a container that cannot establish one.
    """
    monkeypatch.setattr(socket, "gethostname", lambda: "call-agent-box")
    monkeypatch.setattr(
        socket, "gethostbyname",
        lambda host: own_address if host == "call-agent-box" else wan_resolves_to)

    class _Sock:
        def __init__(self, *_a, **_k):
            pass

        def connect(self, _addr):
            if not own_address:
                raise OSError("network is unreachable")

        def getsockname(self):
            return (own_address, 0)

        def bind(self, addr):
            if own_address and addr[0] == own_address:
                return None
            raise OSError("cannot assign requested address")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", _Sock)


def _render_wan(monkeypatch, tmp_path, extra=None, **network):
    _fake_network(monkeypatch, **network)
    return _render(monkeypatch, tmp_path, "192.168.1.73",
                   {**WAN_ENV, "SIP_BIND_PORT": "5062",
                    "SIP_ADVERTISED_PORT": "5060", **(extra or {})})


def test_wan_transport_advertises_the_public_address_and_forwarded_port(
        monkeypatch, tmp_path):
    config = _render_wan(monkeypatch, tmp_path)
    transport = _section(config, "transport-wan", kind="transport")

    assert "bind=10.89.0.101:5063" in transport
    assert "external_signaling_address=188.250.165.236" in transport
    assert "external_signaling_port=45060" in transport
    assert "external_media_address=188.250.165.236" in transport


def test_wan_transport_declares_no_local_net(monkeypatch, tmp_path):
    """10.0.0.0/8 covers the rewritten source of every internet caller.

    With it, PJSIP classifies the whole internet as local, skips the external
    rewrite and puts the container's own address and bind port in Via, Contact
    and the SDP — which presents as "Registered, and no call has audio".
    """
    config = _render_wan(monkeypatch, tmp_path)
    transport = _section(config, "transport-wan", kind="transport")

    assert "local_net" not in transport
    # The LAN transport still has its own, unchanged.
    assert "local_net=10.0.0.0/8" in _section(config, "transport-udp", kind="transport")


def test_wan_endpoint_is_selected_by_auth_and_never_by_identify(monkeypatch, tmp_path):
    """The one sentence this whole feature has to be able to state.

    type=identify matches a source address, knows nothing about ports or
    transports, and runs BEFORE authentication. An endpoint with an identify
    is reachable by an INVITE carrying no credentials at all.
    """
    config = _render_wan(monkeypatch, tmp_path)
    endpoint = _section(config, "wan")

    assert "auth=wan-auth" in endpoint
    assert "identify" not in endpoint
    assert "[wan-identify]" not in config
    # No anonymous endpoint may exist either — that is the other way in.
    assert "[anonymous]" not in config


def test_wan_endpoint_uses_the_usual_nat_pair_not_the_lan_trunk_s(monkeypatch, tmp_path):
    """Opposite of the LAN trunk, and for the opposite reason.

    A WAN peer's replies must go back to the packet source, because that
    source is the host NAT's return path.
    """
    config = _render_wan(monkeypatch, tmp_path)
    endpoint = _section(config, "wan")

    assert "force_rport=yes" in endpoint
    assert "rewrite_contact=yes" in endpoint
    assert "rtp_symmetric=yes" in endpoint
    # The LAN trunk keeps its inverted pair.
    assert "force_rport=no" in _section(config, "lan-trunk")


def test_wan_aor_qualifies_often_enough_to_keep_the_nat_mapping_warm(
        monkeypatch, tmp_path):
    config = _render_wan(monkeypatch, tmp_path)
    aor = _section(config, "wan", kind="aor")

    assert "qualify_frequency=30" in aor
    assert "max_contacts=1" in aor


def test_lan_trunk_context_rejects_a_call_that_arrived_on_another_socket(
        monkeypatch, tmp_path):
    """The latent hole this card had to close before opening any WAN port.

    lan-trunk is chosen by type=identify on a source address, and MEASURED on
    the real host, an internet packet and a gateway packet reach this container
    from the identical rewritten source (10.89.0.x, allocated per container,
    not per origin). Only the destination port differs, identify cannot see it,
    and it runs before authentication. So one unauthenticated INVITE to the
    open WAN port would otherwise reach an AI agent with workspace tools and a
    PSTN trunk.
    """
    _render_wan(monkeypatch, tmp_path)
    extensions = (tmp_path / "extensions.conf").read_text()
    body = extensions.split("[from-lan-trunk]", 1)[1].split("\n\n[", 1)[0]

    assert "CHANNEL(pjsip,local_addr)" in body
    # Empty means the LAN transport (0.0.0.0 bind reports no address); the WAN
    # transport binds concretely so it always reports a port.
    assert '$["${AW_LOCAL_ADDR}"=""]?5062:${CUT(AW_LOCAL_ADDR,:,2)}' in body
    assert '$["${AW_SOCKET_PORT}" != "5062"]' in body
    assert "Hangup(21)" in body
    # The Request-URI also carries the port, and is forgeable in one line by
    # the attacker — it must not be what this rests on.
    assert "request_uri" not in body
    # The guard has to run before the call is answered, or the hole is open
    # for exactly as long as it takes to say hello.
    assert body.index("Hangup(21)") < body.index("Answer()")


def test_the_wan_transport_binds_concretely_so_the_guard_can_see_anything(
        monkeypatch, tmp_path):
    """Measured on the shipped Asterisk 20.20.1 image, not read off a doc.

    CHANNEL(pjsip,local_addr) reads the transport's bound address through
    pj_sockaddr_has_addr(), which is FALSE for an "any" bind: bound to 0.0.0.0
    the function returns EMPTY. A WAN transport on 0.0.0.0 would make the
    guard blind, and blind here means the guard's comparison lands in the
    "must be the LAN socket" branch and lets the internet through.
    """
    config = _render_wan(monkeypatch, tmp_path)

    assert "bind=10.89.0.101:5063" in _section(config, "transport-wan", kind="transport")
    # The LAN transport is deliberately left alone: the in-container SIP
    # self-test dials 127.0.0.1, which a concrete bind would stop answering.
    assert "bind=0.0.0.0:5062" in _section(config, "transport-udp", kind="transport")


def test_wan_refuses_to_open_a_port_the_guard_would_be_blind_to(
        monkeypatch, tmp_path):
    """No own address -> no concrete bind -> no guard. Refuse to start."""
    with pytest.raises(SystemExit):
        _render_wan(monkeypatch, tmp_path, own_address="")


def test_the_guard_is_absent_when_there_is_no_internet_socket(
        monkeypatch, tmp_path):
    """With WAN off the container has one SIP socket and nothing to tell apart.

    The rendered LAN path then has to be byte-identical to the shipped one —
    this is the QA-verified trunk and the guard must not touch it.
    """
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    _render(monkeypatch, tmp_path, "192.168.1.73",
            {**LAN_TRUNK_ENV, "SIP_BIND_PORT": "5062", "SIP_ADVERTISED_PORT": "5060"})
    extensions = (tmp_path / "extensions.conf").read_text()

    assert "CHANNEL(pjsip,local_addr)" not in extensions
    assert "Hangup(21)" not in extensions
    assert "transport-wan" not in (tmp_path / "pjsip.conf").read_text()


def test_wan_context_dials_the_lan_trunk_and_keeps_the_agent_extension(
        monkeypatch, tmp_path):
    config = _render_wan(monkeypatch, tmp_path)
    extensions = (tmp_path / "extensions.conf").read_text()

    assert "context=from-wan" in _section(config, "wan")
    body = extensions.split("[from-wan]", 1)[1].split("\n\n[", 1)[0]
    assert "Dial(PJSIP/${DEST}@lan-trunk,60)" in body
    # _X. would otherwise send 700 down the PSTN line instead of to the agent.
    assert "exten => 700,1,Goto(internal,700,1)" in body
    # _X. does not match a leading "+", which is exactly what a softphone
    # dials out of its address book.
    assert "exten => _+X.,1,Set(DEST=${EXTEN})" in body
    assert "exten => _X.,1,Set(DEST=${EXTEN})" in body


def test_wan_context_normalises_the_number_the_analog_line_cannot_take(
        monkeypatch, tmp_path):
    """A WAN caller dials E.164 from their address book; the line wants national."""
    _render_wan(monkeypatch, tmp_path,
                {"LAN_TRUNK_STRIP_PREFIX": "+351", "LAN_TRUNK_DIAL_PREFIX": "9"})
    extensions = (tmp_path / "extensions.conf").read_text()
    body = extensions.split("[from-wan]", 1)[1].split("\n\n[", 1)[0]

    assert '$["${DEST:0:4}" = "+351"]?Set(DEST=${DEST:4})' in body
    assert "Set(DEST=9${DEST})" in body


def test_the_anti_scanner_limits_are_set_once_the_port_is_open(monkeypatch, tmp_path):
    config = _render_wan(monkeypatch, tmp_path)

    assert "unidentified_request_count=5" in config
    assert "unidentified_request_period=5" in config


def test_rtp_range_stays_inside_the_ports_the_host_publishes(monkeypatch, tmp_path):
    """Asterisk must not allocate a port past the end of the forwarded range.

    A call that lands on 10020 is silently one-way, and nothing logs it.
    """
    _render_wan(monkeypatch, tmp_path)
    rtp = (tmp_path / "rtp.conf").read_text()

    assert "rtpstart=10000" in rtp
    assert "rtpend=10019" in rtp
    # The published container ports stay reachable even with WAN off, so media
    # from anywhere but the negotiated peer has to be dropped.
    assert "strictrtp=yes" in rtp
    assert "icesupport=no" in rtp


def test_wan_records_what_it_advertised_next_to_the_hostname(monkeypatch, tmp_path):
    """wan_watch compares these two to notice the home IP lease renewing."""
    _render_wan(monkeypatch, tmp_path)
    state = json.loads((tmp_path / "wan_state.json").read_text())

    assert state == {"enabled": True, "hostname": "home.example.com",
                     "address": "188.250.165.236", "bind_port": "5063",
                     "advertised_port": "45060"}


def test_wan_stays_off_unless_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "203.0.113.42")
    config = _render(monkeypatch, tmp_path, "192.168.1.73", LAN_TRUNK_ENV)
    extensions = (tmp_path / "extensions.conf").read_text()

    assert "transport-wan" not in config
    assert "[wan]" not in config
    assert "wan-auth" not in config
    assert "from-wan" not in extensions
    assert "unidentified_request_count" not in config


def test_wan_refuses_a_guessable_username(monkeypatch, tmp_path):
    """101 or 1234 on an internet-facing endpoint is the entire attack."""
    with pytest.raises(SystemExit):
        _render_wan(monkeypatch, tmp_path, {"WAN_SIP_USERNAME": "101"})


def test_wan_refuses_a_short_password(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _render_wan(monkeypatch, tmp_path, {"WAN_SIP_PASSWORD": "hunter2"})


def test_wan_requires_its_credentials_and_address(monkeypatch, tmp_path):
    for missing in ("WAN_SIP_USERNAME", "WAN_SIP_PASSWORD",
                    "WAN_SIP_EXTERNAL_ADDRESS"):
        with pytest.raises(SystemExit):
            _render_wan(monkeypatch, tmp_path, {missing: ""})


def test_wan_refuses_to_share_the_lan_bind_port(monkeypatch, tmp_path):
    """The dialplan guard tells a WAN call from a LAN one by that port alone."""
    with pytest.raises(SystemExit):
        _render_wan(monkeypatch, tmp_path, {"WAN_SIP_BIND_PORT": "5062"})


def test_wan_refuses_to_start_when_the_hostname_does_not_resolve(
        monkeypatch, tmp_path):
    """Advertising nothing would register fine and carry no audio at all."""
    def boom(_hostname):
        raise OSError("NXDOMAIN")

    monkeypatch.setattr(socket, "gethostbyname", boom)
    with pytest.raises(SystemExit):
        _render(monkeypatch, tmp_path, "192.168.1.73",
                {**WAN_ENV, "SIP_BIND_PORT": "5062", "SIP_ADVERTISED_PORT": "5060"})


def test_wan_needs_a_lan_trunk_to_bridge_to(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "gethostbyname", lambda _hostname: "188.250.165.236")
    wan_only = {k: v for k, v in WAN_ENV.items() if not k.startswith("LAN_TRUNK")}
    with pytest.raises(SystemExit):
        _render(monkeypatch, tmp_path, "192.168.1.73", wan_only)


# ── the three numbering planes only agree by convention ─────────────────────

def _manifest():
    return json.loads((ROOT / "aw-app.json").read_text(encoding="utf-8"))


def _publish(manifest, host):
    return next(p for p in manifest["runtime"]["publish"] if str(p["host"]) == host)


def test_manifest_publishes_the_ports_the_renderer_binds_and_advertises():
    """runtime.publish is static and cannot be templated from config.

    So the bind/advertise pair in runtime.env and the publish entry are two
    hand-maintained copies of the same fact, and a drift between them is
    invisible until a call fails.
    """
    manifest = _manifest()
    env = manifest["runtime"]["env"]
    wan = _publish(manifest, "45060")

    assert str(wan["container"]) == env["WAN_SIP_BIND_PORT"]
    assert str(wan["host"]) == env["WAN_SIP_ADVERTISED_PORT"]
    assert wan["protocol"] == "udp"


def test_manifest_rtp_publish_is_offset_equal_length_and_disjoint():
    """The offset is not a typo — it is what escapes the host's UDP drop.

    Outbound RTP from a source port that is also a published host port is
    dropped on this host, so the container binds 10000-10019 (unpublished as
    host ports) and the host publishes 30000-30019 onto them.
    """
    manifest = _manifest()
    env = manifest["runtime"]["env"]
    rtp = _publish(manifest, "30000-30019")

    inside = [int(x) for x in str(rtp["container"]).split("-")]
    outside = [int(x) for x in str(rtp["host"]).split("-")]
    assert inside[1] - inside[0] == outside[1] - outside[0]
    assert set(range(inside[0], inside[1] + 1)).isdisjoint(
        range(outside[0], outside[1] + 1))
    assert str(inside[0]) == env["WAN_RTP_PORT_START"]
    assert str(inside[1]) == env["WAN_RTP_PORT_END"]


def test_the_wan_credentials_are_generated_and_cannot_be_defaulted():
    """x-generate forbids a literal default, which is the point.

    A default would win on first install and the generator would never fire —
    shipping one guessable internet-facing credential to every install.
    """
    schema = _manifest()["config_schema"]["properties"]

    for key in ("wan_sip_username", "wan_sip_password"):
        assert "x-generate" in schema[key]
        assert "default" not in schema[key]
    assert schema["wan_sip_password"].get("x-secret") is True
    assert schema["wan_sip_enabled"]["default"] is False
