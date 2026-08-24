"""Route + protocol tests against the real sub-app, with agents-platform
stubbed at the httpx layer.

The WS test is the one that matters: it asserts the exact frame sequence the
iOS ``StreamingCallStore`` and the browser panel both depend on
(``ready`` → ``text_delta``* → ``done``), because that protocol is the entire
compatibility contract this port inherited from the monolith.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import uuid
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from call_agent_app import settings as settings_mod  # noqa: E402
from call_agent_app.routes import build_routes  # noqa: E402
from call_agent_app.service import (  # noqa: E402
    CallAgentService, CallSettings, pick_edge_voice, strip_markdown,
)
from call_agent_app.telephony import (  # noqa: E402
    AsteriskAMI, TelephonyError, TelephonySettings, normalise_e164,
    render_asterisk_config,
)
from call_agent_app.audio_socket import (  # noqa: E402
    AudioSocketBridge, KIND_HANGUP, KIND_PCM_8K, KIND_UUID, encode_frame,
    read_frame,
)
from call_agent_app.call_history import CallStore  # noqa: E402
from call_agent_app.__main__ import build_standalone_app  # noqa: E402
from call_agent_app.sip_tester import pcm16_to_ulaw, ulaw_peak  # noqa: E402

CONFIG = {
    "agents_platform_base": "http://ap.test",
    "agents_platform_token": "tok",
    "agent_slug": "telegram-sonnet",
    "external_id": "test",
    "poll_interval_seconds": 0.01,
    "max_poll_seconds": 2,
}

EVENTS = [
    {"kind": "system.init", "ts": "2026-01-01T00:00:00", "payload": {"session_id": "sess-1"}},
    {"kind": "llm_token", "ts": "2026-01-01T00:00:01", "payload": {"delta": "Oi"}},
    {"kind": "llm_token", "ts": "2026-01-01T00:00:02", "payload": {"delta": ", tudo bem?"}},
    {"kind": "done", "ts": "2026-01-01T00:00:03", "payload": {}},
]


def _stub_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/agents":
            return httpx.Response(200, json=[
                {"slug": "telegram-sonnet", "name": "Telegram Sonnet"},
                {"slug": "coder-sonnet", "name": "Coder"},
            ])
        if path.startswith("/api/targets/") and path.endswith("/runs"):
            return httpx.Response(200, json={"runs": []})
        if path.startswith("/api/targets/"):
            return httpx.Response(404, json={"detail": "not found"})
        if path == "/api/targets":
            return httpx.Response(200, json={"slug": "x"})
        if path.endswith("/run"):
            assert json.loads(request.content)["target_slug"] == "telegram-sonnet-test"
            return httpx.Response(200, json={"run_id": "run-1"})
        if path == "/api/runs/run-1/events":
            return httpx.Response(200, json=EVENTS)
        return httpx.Response(404, json={"detail": path})

    return httpx.MockTransport(handler)


@pytest.fixture()
def client(monkeypatch):
    transport = _stub_transport()
    real_client = CallAgentService.client

    def patched(self, timeout: float = 30.0):
        c = real_client(self, timeout=timeout)
        c._transport = transport
        return c

    monkeypatch.setattr(CallAgentService, "client", patched)
    return TestClient(build_routes(config_provider=lambda: CONFIG))


def test_health_reports_target(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["configured"] is True
    assert body["target_slug"] == "telegram-sonnet-test"


def test_settings_never_leaks_the_token(client):
    body = client.get("/settings").json()
    assert body["has_token"] is True
    assert "tok" not in json.dumps(list(body.values()))


def test_agents_list_is_proxied(client):
    rows = client.get("/agents-list").json()["agents"]
    assert {r["slug"] for r in rows} == {"telegram-sonnet", "coder-sonnet"}


def test_panel_renders_html(client):
    for path in ("/panel", "/panel/status"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "<!doctype html>" in resp.text.lower()


def test_ws_call_streams_deltas_then_done(client):
    with client.websocket_connect("/ws/call") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["target"] == "telegram-sonnet-test"

        ws.send_json({"type": "message", "text": "oi"})

        deltas, final = [], None
        for _ in range(40):
            frame = ws.receive_json()
            if frame["type"] == "text_delta":
                deltas.append(frame["text"])
            elif frame["type"] == "done":
                final = frame
                break
            elif frame["type"] == "error":
                pytest.fail(f"unexpected error frame: {frame}")
        assert "".join(deltas) == "Oi, tudo bem?"
        assert final["text"] == "Oi, tudo bem?"
        assert final["run_id"] == "run-1"


def test_ws_clear_is_acknowledged(client):
    with client.websocket_connect("/ws/call") as ws:
        ws.receive_json()
        ws.send_json({"type": "clear"})
        assert ws.receive_json()["type"] == "cleared"


def test_blank_config_inherits_from_the_runners_app(monkeypatch):
    monkeypatch.setattr(settings_mod, "_runners_config",
                        lambda: {"agents_platform_base": "http://inherited.test",
                                 "agents_platform_token": "inherited-tok"})
    monkeypatch.delenv("AW_AGENTS_PLATFORM_BASE", raising=False)
    monkeypatch.delenv("AW_AGENTS_PLATFORM_TOKEN", raising=False)
    body = TestClient(build_routes(config_provider=dict)).get("/settings").json()
    assert body["agents_platform_base"] == "http://inherited.test"
    assert body["has_token"] is True
    assert body["credentials_source"] == "inherited:agents-platform-runners"


def test_unconfigured_backend_errors_on_the_socket_not_in_a_log(monkeypatch):
    # Nothing anywhere: no app config, no env, no runners app to inherit from.
    monkeypatch.setattr(settings_mod, "_runners_config", dict)
    monkeypatch.delenv("AW_AGENTS_PLATFORM_BASE", raising=False)
    monkeypatch.delenv("AW_AGENTS_PLATFORM_TOKEN", raising=False)
    app = build_routes(config_provider=lambda: {"agents_platform_base": ""})
    with TestClient(app).websocket_connect("/ws/call") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert "agents-platform base URL" in frame["message"]
        assert ws.receive_json()["type"] == "ready"


# ---- ported pure helpers ----------------------------------------------------

@pytest.mark.parametrize("lang,expected", [
    ("pt-BR", "pt-BR-AntonioNeural"),
    ("en-US", "en-US-AndrewMultilingualNeural"),
    # iOS sends the underscore form; the monolith's fix for it came across.
    ("en_US", "en-US-AndrewMultilingualNeural"),
    ("", "pt-BR-AntonioNeural"),
    ("xx", "pt-BR-AntonioNeural"),
])
def test_voice_picking(lang, expected):
    assert pick_edge_voice(lang) == expected


def test_markdown_is_stripped_before_speech():
    assert strip_markdown("**done** see `x` [link](http://a) # h") == "done see x link h"


def test_prompt_template_always_carries_the_users_words():
    assert "olá" in CallSettings(prompt_template="no placeholder").build_prompt("olá")
    assert CallSettings(prompt_template="say: ${text}").build_prompt("oi") == "say: oi"


# ---- the frontend bundle ----------------------------------------------------

def test_bundle_declared_in_the_manifest_exists_and_exports_both_entrypoints():
    """The window body is component mode, so a missing or half-exported bundle
    means the window renders empty chrome and nothing else — the failure shape
    a denied/absent `ui:code` bundle produces, which reads as a bug in the app.
    """
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "aw-app.json").read_text())
    bundle = root / manifest["contributes"]["frontend"]["bundle"]
    assert bundle.is_file(), f"{bundle} is declared in aw-app.json but not committed"

    src = bundle.read_text()
    # `register` is what the SPA calls; `mountCallUI` is what the /panel shell
    # imports. Losing either silently kills one surface and not the other.
    assert "export function register(" in src
    assert "export function mountCallUI(" in src
    assert "export default register" in src
    assert "/telephony/status" in src
    assert "/telephony/calls" in src

    # The window body slot must be the one this app's window declares.
    window_ids = {w["id"] for w in manifest["contributes"]["windows"]}
    for slot in manifest["contributes"]["frontend"].get("slots", []):
        if slot.startswith("core.window.body:"):
            assert slot.split(":", 1)[1] in window_ids


def test_panel_shell_loads_the_same_bundle():
    from call_agent_app.panel import PANEL_HTML
    assert "./ui/call-agent.js" in PANEL_HTML
    assert "mountCallUI" in PANEL_HTML


def test_ws_set_agent_repoints_the_socket_and_its_target(client):
    """Picking an agent mid-call must move the conversation too: each agent
    keeps its own Target, so switching cannot silently continue the previous
    agent's thread under a new name."""
    with client.websocket_connect("/ws/call") as ws:
        assert ws.receive_json()["target"] == "telegram-sonnet-test"

        ws.send_json({"type": "set_agent", "slug": "coder-sonnet"})
        changed = ws.receive_json()
        assert changed["type"] == "agent_changed"
        assert changed["agent"] == "coder-sonnet"
        assert changed["target"] == "coder-sonnet-test"


def test_ws_set_agent_ignores_a_no_op(client):
    with client.websocket_connect("/ws/call") as ws:
        ws.receive_json()
        # Same agent, and a blank slug: neither should produce a frame, so the
        # next thing on the wire is the ack for the clear that follows.
        ws.send_json({"type": "set_agent", "slug": "telegram-sonnet"})
        ws.send_json({"type": "set_agent", "slug": "  "})
        ws.send_json({"type": "clear"})
        assert ws.receive_json()["type"] == "cleared"


def test_settings_exposes_the_speech_pause(client):
    assert client.get("/settings").json()["speech_pause_ms"] == 2000.0


# ---- SIP/Asterisk control plane -------------------------------------------

def test_telephony_is_safe_and_disabled_by_default(client):
    status = client.get("/telephony/status").json()
    assert status["enabled"] is False
    assert status["asterisk"]["reachable"] is False
    resp = client.post("/telephony/calls", json={"number": "+351211234567"})
    assert resp.status_code == 409
    assert "disabled" in resp.json()["error"]


def test_asterisk_preview_redacts_every_secret():
    s = TelephonySettings(
        enabled=True,
        sip_username="10001",
        sip_password="sip-super-secret",
        public_number="+351300000000",
        ami_secret="ami-super-secret",
    )
    files = render_asterisk_config(s, redact=True)
    rendered = "\n".join(files.values())
    assert "sip-super-secret" not in rendered
    assert "ami-super-secret" not in rendered
    assert "password=***" in rendered
    assert "secret=***" in rendered
    assert "AudioSocket(" in files["extensions.conf"]
    assert "PJSIP/${EXTEN}@zadarma" in files["extensions.conf"]


@pytest.mark.parametrize("raw,want", [
    ("+351 211 234 567", "+351211234567"),
    ("+351(300)000-000", "+351300000000"),
])
def test_normalise_e164(raw, want):
    assert normalise_e164(raw) == want


@pytest.mark.parametrize("raw", ["", "351211234567", "+0123", "+351abc"])
def test_normalise_e164_rejects_unsafe_or_ambiguous_numbers(raw):
    with pytest.raises(TelephonyError):
        normalise_e164(raw)


def test_telephony_preview_route_never_leaks_secrets():
    cfg = dict(CONFIG, telephony_enabled=True, sip_username="u",
               sip_password="hidden-sip", sip_public_number="+351300000000",
               asterisk_ami_secret="hidden-ami")
    body = TestClient(build_routes(config_provider=lambda: cfg)).get(
        "/telephony/config-preview").json()
    assert "hidden-sip" not in json.dumps(body)
    assert "hidden-ami" not in json.dumps(body)


def test_internal_extension_hides_password_unless_explicitly_revealed():
    cfg = dict(CONFIG, internal_sip_extension="101",
               internal_sip_password="local-phone-secret",
               call_agent_extension="700", sip_external_address="192.168.1.50")
    api = TestClient(build_routes(config_provider=lambda: cfg))
    hidden = api.get("/telephony/internal-extension").json()
    assert hidden["password"] == "********"
    assert hidden["server"] == "192.168.1.50"
    shown = api.get(
        "/telephony/internal-extension?reveal_password=true").json()
    assert shown["password"] == "local-phone-secret"


def test_ami_ping_and_originate_use_the_expected_protocol():
    async def scenario():
        actions = []

        async def fake_ami(reader, writer):
            writer.write(b"Asterisk Call Manager/5.0\r\n")
            await writer.drain()
            login = (await reader.readuntil(b"\r\n\r\n")).decode()
            assert "Action: Login" in login
            assert "Secret: local-secret" in login
            writer.write(b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n")
            await writer.drain()
            action = (await reader.readuntil(b"\r\n\r\n")).decode()
            actions.append(action)
            if "Action: Ping" in action:
                writer.write(b"Response: Success\r\nPing: Pong\r\n\r\n")
            else:
                writer.write(b"Response: Success\r\nMessage: Originate successfully queued\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(fake_ami, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        settings = TelephonySettings(
            enabled=True, sip_username="u", sip_password="p",
            public_number="+351300000000", ami_port=port,
            ami_secret="local-secret",
        )
        async with server:
            ami = AsteriskAMI(settings)
            assert (await ami.ping())["ping"] == "Pong"
            await ami.originate("+351 211 234 567", "+351300000000")

        assert "Action: Ping" in actions[0]
        assert "Channel: Local/351211234567@call-agent-outbound" in actions[1]
        assert "CallerID: +351300000000" in actions[1]

    asyncio.run(scenario())


def test_audio_socket_persists_a_playable_recording_and_history_route():
    async def scenario(root):
        store = CallStore(root)
        bridge = AudioSocketBridge(store, port=0)
        await bridge.start()
        call_uuid = uuid.uuid4()
        _reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        pcm = (b"\x00\x00\x10\x00" * 800)  # 0.2s of valid little-endian PCM
        writer.write(encode_frame(KIND_UUID, call_uuid.bytes))
        writer.write(encode_frame(KIND_PCM_8K, pcm))
        writer.write(encode_frame(KIND_HANGUP))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        for _ in range(50):
            row = store.get(str(call_uuid))
            if row and row["status"] != "active":
                break
            await asyncio.sleep(0.01)
        await bridge.stop()
        return store, str(call_uuid)

    with tempfile.TemporaryDirectory() as root:
        store, call_id = asyncio.run(scenario(root))
        row = store.get(call_id)
        assert row["status"] == "completed"
        assert row["has_recording"] is True
        assert row["duration_seconds"] == pytest.approx(0.2)
        with wave.open(str(store.recording_path(call_id)), "rb") as recording:
            assert recording.getframerate() == 8000
            assert recording.getsampwidth() == 2
            assert recording.getnchannels() == 1

        api = TestClient(build_routes(config_provider=lambda: CONFIG, call_store=store))
        history = api.get("/telephony/calls").json()["calls"]
        assert history[0]["id"] == call_id
        audio = api.get(f"/telephony/calls/{call_id}/recording")
        assert audio.status_code == 200
        assert audio.headers["content-type"].startswith("audio/wav")
        assert audio.content.startswith(b"RIFF")
        store.append_text(call_id, transcript="olá", agent_text="Olá!", run_id="run-1")
        enriched = store.get(call_id)
        assert enriched["transcript"] == "olá"
        assert enriched["agent_text"] == "Olá!"
        assert enriched["run_ids"] == ["run-1"]
        store.close()


def test_internal_self_test_uses_live_audio_socket_without_sip_credentials():
    async def scenario(root):
        store = CallStore(root)
        bridge = AudioSocketBridge(store, port=0)
        await bridge.start()
        app = build_routes(
            config_provider=lambda: CONFIG,
            call_store=store,
            audio_bridge_provider=lambda: bridge,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/telephony/self-test")
            assert response.status_code == 202
            call_id = response.json()["call_id"]
            recording = await client.get(f"/telephony/calls/{call_id}/recording")
            assert recording.status_code == 200
            assert recording.content.startswith(b"RIFF")
        row = store.get(call_id)
        assert row["status"] == "completed"
        assert row["duration_seconds"] == pytest.approx(1.0)
        await bridge.stop()
        store.close()

    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(root))


def test_tier2_container_serves_routes_at_proxy_relative_root(monkeypatch, tmp_path):
    monkeypatch.setenv("AW_TIER2_CONTAINER", "1")
    monkeypatch.setenv("ASTERISK_AUDIO_SOCKET_PORT", "0")
    monkeypatch.setenv("AW_CALL_AGENT_DATA", str(tmp_path))
    with TestClient(build_standalone_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        test = client.post("/telephony/self-test")
        assert test.status_code == 202


def test_sip_pcmu_codec_detects_audible_audio():
    silence = pcm16_to_ulaw(b"\x00\x00" * 160)
    speech = pcm16_to_ulaw(b"\x10\x27" * 160)
    assert ulaw_peak(silence) < 500
    assert ulaw_peak(speech) > 5000


def test_sip_integration_test_requires_internal_credentials():
    api = TestClient(build_routes(config_provider=lambda: CONFIG))
    response = api.post("/telephony/sip-integration-test", json={})
    assert response.status_code == 409
    assert "SIP password" in response.json()["error"]


def test_audio_socket_vad_sends_agent_pcm_back_to_caller():
    async def scenario(root):
        store = CallStore(root)
        heard = []

        async def respond(call_id, pcm):
            heard.append((call_id, pcm))
            return b"\x10\x00" * 160

        bridge = AudioSocketBridge(store, port=0, utterance_handler=respond,
                                   speech_pause_ms=20)
        await bridge.start()
        call_uuid = uuid.uuid4()
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        writer.write(encode_frame(KIND_UUID, call_uuid.bytes))
        writer.write(encode_frame(KIND_PCM_8K, b"\xff\x3f" * 160))
        writer.write(encode_frame(KIND_PCM_8K, b"\x00\x00" * 160))
        await writer.drain()
        kind, response = await asyncio.wait_for(read_frame(reader), 1)
        assert kind == KIND_PCM_8K
        assert response == b"\x10\x00" * 160
        assert heard and heard[0][0] == str(call_uuid)
        writer.write(encode_frame(KIND_HANGUP))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await bridge.stop()
        store.close()

    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(root))
