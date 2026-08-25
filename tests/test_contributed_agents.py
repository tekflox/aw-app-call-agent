"""The Call Agent contribution must work on a fresh workspace.

An Agent row can be created while its model or AgentConfig reference is
missing.  Agents Platform deliberately tolerates that shape, but the failure
only appears when the first call arrives: the model cannot start or the agent
has no MCP tools.  Keep the three contributed objects self-contained here.
"""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "aw-app.json").read_text(encoding="utf-8"))
SPEC = MANIFEST["contributes"]["agents"]


def test_call_agent_references_objects_contributed_by_this_app():
    models = {row["slug"] for row in SPEC["models"]}
    configs = {row["slug"] for row in SPEC["agent_configs"]}

    agent = next(row for row in SPEC["agents"]
                 if row["slug"] == "call-agent-openai")
    assert agent["model_slug"] in models
    assert agent["agent_config_slug"] in configs


def test_call_agent_config_resolves_workspace_gateway_by_reference():
    config = next(row for row in SPEC["agent_configs"]
                  if row["slug"] == "agent-config-call-agent-tools")
    assert config["mcp_servers"] == ["aw-gateway"]
    assert "mcp_config" not in config


def test_contribution_contains_no_credentials_or_fixed_gateway_url():
    blob = json.dumps(SPEC).lower()
    assert "sk-" not in blob
    assert "http://" not in blob
    assert "https://" not in blob

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key.lower()
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    sensitive = {"token", "authorization", "api_key", "password", "secret"}
    assert not (set(keys(SPEC)) & sensitive)


def test_realtime_agent_uses_low_reasoning_for_phone_latency():
    model = next(row for row in SPEC["models"]
                 if row["slug"] == "openai-gpt-5-6-luna")
    assert model["provider"] == "openai"
    assert model["params"]["reasoning_effort"] == "low"
