"""``POST /v1/agents`` session resources (SOR-129).

Per-agent resource refs — allowlisted Modal Secret names and registry MCP
refs — validated at create time, injected into that agent's sandbox only,
echoed back as names only (never values). MCP on a provider without an MCP
channel is a machine-readable ``unsupported``, never silently ignored.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from control.resources import (
    MCP_REGISTRY_ENV,
    RESOURCE_SECRETS_ENV,
    McpServerSpec,
    ResourceRegistry,
)
from tests.unit.api_v1.conftest import create_agent, seed_account, wait_sandbox
from tests.unit.api_v1.test_verify import RecordingBackend, _argv_opt

LINEAR = McpServerSpec(
    name="linear",
    url="https://mcp.linear.app/mcp",
    transport="http",
    headers={"Authorization": "Bearer ${env:SBX_LINEAR_API_KEY}"},
)


@pytest.fixture
def spy(v1_env) -> RecordingBackend:
    backend = RecordingBackend(v1_env.backend)
    v1_env.app.state.plane.backend = backend
    return backend


def _registry(v1_env, **kwargs) -> ResourceRegistry:
    registry = ResourceRegistry(**kwargs)
    v1_env.app.state.resource_registry = registry
    return registry


def _post(client, auth, **overrides):
    body = {"prompt": {"text": "hi"}, "agent": {"provider": "codex"}}
    body.update(overrides)
    return client.post("/v1/agents", json=body, headers=auth)


class TestResourcesValidation:
    def test_omitted_resources_unchanged(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth)
        assert body["agent"]["resources"] is None
        rec = wait_sandbox(v1_env, body["agent"]["id"])
        assert rec.status in ("idle", "running")
        spec = spy.specs[0]
        assert spec.resource_secrets == []
        argv, env = spy.execs[0]
        assert "init" in argv
        assert "SBX_MCP_SERVERS" not in env
        # The legacy tag shape is preserved when nothing was declared.
        assert "resources" not in (rec.sandbox_tags or {})

    def test_empty_resources_unchanged(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth, resources={"secrets": [], "mcp": []})
        assert body["agent"]["resources"] is None
        assert spy.specs[0].resource_secrets == []

    def test_allowlisted_secret_attached(self, client, auth, v1_env, spy) -> None:
        _registry(v1_env, secrets=("sbx-res-openai",))
        body = create_agent(client, auth, resources={"secrets": ["sbx-res-openai"]})
        assert body["agent"]["resources"] == {
            "secrets": ["sbx-res-openai"],
            "mcp": [],
        }
        rec = wait_sandbox(v1_env, body["agent"]["id"])
        assert rec.status in ("idle", "running")
        spec = spy.specs[0]
        assert spec.resource_secrets == ["sbx-res-openai"]
        # The account Secret path is untouched — only the resource names
        # were added, and only to this spec.
        assert spec.secrets == []
        refs = json.loads(rec.sandbox_tags["resources"])
        assert refs == {"secrets": ["sbx-res-openai"], "mcp": []}

    def test_get_echoes_refs_names_only(self, client, auth, v1_env, spy) -> None:
        _registry(v1_env, secrets=("sbx-res-openai",), mcp={"linear": LINEAR})
        seed_account(v1_env, "acct-devin-1", provider="devin", models=("swe-2-high",))
        body = create_agent(
            client,
            auth,
            agent={"provider": "devin"},
            resources={"secrets": ["sbx-res-openai"], "mcp": ["linear"]},
        )
        agent_id = body["agent"]["id"]
        got = client.get(f"/v1/agents/{agent_id}", headers=auth)
        assert got.status_code == 200
        resources = got.json()["resources"]
        assert resources == {"secrets": ["sbx-res-openai"], "mcp": ["linear"]}
        # Names only — no URL, no header template, no env-var indirection.
        serialized = json.dumps(resources)
        assert "mcp.linear.app" not in serialized
        assert "SBX_LINEAR_API_KEY" not in serialized
        assert "Authorization" not in serialized

    def test_unknown_secret_invalid_resource(self, client, auth, v1_env, spy) -> None:
        _registry(v1_env, secrets=("sbx-res-openai",))
        resp = _post(client, auth, resources={"secrets": ["sbx-res-missing"]})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_resource"
        # Validation precedes provisioning — no sandbox was created.
        assert spy.specs == []

    def test_reserved_secret_invalid_resource(self, client, auth, v1_env, spy) -> None:
        # Even when the operator allowlists it, a control-plane credential
        # Secret can never ride a session resource ref.
        _registry(v1_env, secrets=("sbx-acct-1", "sbx-codex-auth"))
        for name in ("sbx-acct-1", "sbx-codex-auth"):
            resp = _post(client, auth, resources={"secrets": [name]})
            assert resp.status_code == 400, name
            assert resp.json()["error"]["code"] == "invalid_resource"
        assert spy.specs == []

    def test_malformed_secret_invalid_resource(self, client, auth, v1_env, spy) -> None:
        _registry(v1_env, secrets=("sbx-res-openai",))
        resp = _post(client, auth, resources={"secrets": ["has space"]})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_resource"

    def test_unknown_mcp_invalid_resource(self, client, auth, v1_env, spy) -> None:
        _registry(v1_env, mcp={"linear": LINEAR})
        seed_account(v1_env, "acct-devin-1", provider="devin", models=("swe-2-high",))
        resp = _post(
            client,
            auth,
            agent={"provider": "devin"},
            resources={"mcp": ["notion"]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_resource"
        assert spy.specs == []

    @pytest.mark.parametrize("provider", ["codex", "antigravity", "grok", "opencode"])
    def test_mcp_unsupported_provider(self, client, auth, v1_env, spy, provider) -> None:
        _registry(v1_env, mcp={"linear": LINEAR})
        resp = _post(client, auth, agent={"provider": provider}, resources={"mcp": ["linear"]})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unsupported"
        assert spy.specs == []

    def test_mcp_entry_provider_restriction_unsupported(self, client, auth, v1_env, spy) -> None:
        spec = McpServerSpec(
            name="agyonly", url="https://example.com/mcp", providers=("antigravity",)
        )
        _registry(v1_env, mcp={"agyonly": spec})
        seed_account(v1_env, "acct-devin-1", provider="devin", models=("swe-2-high",))
        resp = _post(
            client,
            auth,
            agent={"provider": "devin"},
            resources={"mcp": ["agyonly"]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unsupported"


class TestResourcesInjection:
    def test_mcp_devin_reaches_init_env_and_session(self, client, auth, v1_env, spy) -> None:
        _registry(v1_env, mcp={"linear": LINEAR})
        seed_account(v1_env, "acct-devin-1", provider="devin", models=("swe-2-high",))
        body = create_agent(
            client,
            auth,
            agent={"provider": "devin"},
            resources={"mcp": ["linear"]},
        )
        rec = wait_sandbox(v1_env, body["agent"]["id"])
        assert rec.status in ("idle", "running")
        argv, env = spy.execs[0]
        assert "init" in argv
        assert _argv_opt(argv, "--provider") == "devin"
        servers = json.loads(env["SBX_MCP_SERVERS"])
        assert servers == [
            {
                "name": "linear",
                "url": "https://mcp.linear.app/mcp",
                "transport": "http",
                "headers": {"Authorization": "Bearer ${env:SBX_LINEAR_API_KEY}"},
            }
        ]
        session = json.loads((Path(rec.sandbox_root) / "session.json").read_text())
        assert session["mcp_servers"] == ["linear"]

    def test_mcp_env_only_in_init_exec(self, client, auth, v1_env, spy) -> None:
        _registry(v1_env, mcp={"linear": LINEAR})
        seed_account(v1_env, "acct-devin-1", provider="devin", models=("swe-2-high",))
        body = create_agent(
            client,
            auth,
            agent={"provider": "devin"},
            resources={"mcp": ["linear"]},
        )
        wait_sandbox(v1_env, body["agent"]["id"])
        carriers = [env for argv, env in spy.execs if "SBX_MCP_SERVERS" in env]
        assert len(carriers) == 1

    def test_resources_isolated_between_agents(self, client, auth, v1_env, spy) -> None:
        _registry(v1_env, secrets=("sbx-res-openai",))
        first = create_agent(client, auth, resources={"secrets": ["sbx-res-openai"]})
        second = create_agent(client, auth)
        wait_sandbox(v1_env, first["agent"]["id"])
        wait_sandbox(v1_env, second["agent"]["id"])
        assert spy.specs[0].resource_secrets == ["sbx-res-openai"]
        assert spy.specs[1].resource_secrets == []
        # The second agent's tags carry no resource refs.
        rec2 = v1_env.store.get(second["agent"]["id"])
        assert "resources" not in (rec2.sandbox_tags or {})

    def test_account_secret_still_attached_with_resources(self, client, auth, v1_env, spy) -> None:
        _registry(v1_env, secrets=("sbx-res-openai",))
        seed_account(v1_env, "acct-codex-2", secret_name="sbx-acct-2")
        body = create_agent(
            client,
            auth,
            agent={"provider": "codex", "account_id": "acct-codex-2"},
            resources={"secrets": ["sbx-res-openai"]},
        )
        wait_sandbox(v1_env, body["agent"]["id"])
        spec = spy.specs[0]
        assert spec.secrets == ["sbx-acct-2"]
        assert spec.resource_secrets == ["sbx-res-openai"]

    def test_env_configured_registry(self, client, auth, v1_env, spy, monkeypatch) -> None:
        # No injected registry — ``get_resources`` falls back to the
        # deployment env (SBX_RESOURCE_SECRETS / SBX_MCP_REGISTRY).
        monkeypatch.setenv(RESOURCE_SECRETS_ENV, "sbx-res-env")
        monkeypatch.setenv(
            MCP_REGISTRY_ENV,
            json.dumps({"linear": {"url": "https://mcp.linear.app/mcp"}}),
        )
        body = create_agent(client, auth, resources={"secrets": ["sbx-res-env"]})
        wait_sandbox(v1_env, body["agent"]["id"])
        assert spy.specs[0].resource_secrets == ["sbx-res-env"]
        resp = _post(client, auth, resources={"secrets": ["sbx-res-other"]})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_resource"

    def test_refs_only_in_sandbox_tags(self, client, auth, v1_env, spy) -> None:
        _registry(v1_env, mcp={"linear": LINEAR})
        seed_account(v1_env, "acct-devin-1", provider="devin", models=("swe-2-high",))
        body = create_agent(
            client,
            auth,
            agent={"provider": "devin"},
            resources={"mcp": ["linear"]},
        )
        rec = wait_sandbox(v1_env, body["agent"]["id"])
        tag = rec.sandbox_tags["resources"]
        assert json.loads(tag) == {"secrets": [], "mcp": ["linear"]}
        assert "mcp.linear.app" not in tag
        assert "SBX_LINEAR_API_KEY" not in tag
