"""Route + protocol tests against the real sub-app, with agents-platform
stubbed at the httpx layer.

The WS test is the one that matters: it asserts the exact frame sequence the
iOS ``StreamingCallStore`` and the browser panel both depend on
(``ready`` → ``text_delta``* → ``done``), because that protocol is the entire
compatibility contract this port inherited from the monolith.
"""
from __future__ import annotations

import json
import sys
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
