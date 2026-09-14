import runpy
import socket
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "container" / "render_asterisk.py"


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
