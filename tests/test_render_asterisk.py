import runpy
import socket
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "container" / "render_asterisk.py"


def _render(monkeypatch, tmp_path, external="auto"):
    monkeypatch.setenv("ASTERISK_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("INTERNAL_SIP_PASSWORD", "internal-secret-123")
    monkeypatch.setenv("ASTERISK_AMI_SECRET", "ami-secret-value-123")
    monkeypatch.setenv("SIP_EXTERNAL_ADDRESS", external)
    monkeypatch.setenv("AW_WORKSPACE_SLUG", "fresh-workspace")
    runpy.run_path(str(SCRIPT))
    return (tmp_path / "pjsip.conf").read_text()


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
    assert "Set(JITTERBUFFER(adaptive)=default)" in extensions
