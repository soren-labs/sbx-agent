"""SOR-129: session-resource registry validation + Modal secret attachment.

Covers ``control.resources`` (allowlist/registry validation, error codes)
and the ``control.backends.modal`` secret resolution for
``SandboxSpec.resource_secrets`` without any Modal SDK import — helpers
take a fake ``modal`` module like the neighboring suites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from control.backend import SandboxHandle, SandboxSpec
from control.backends.modal import ModalBackend, _sandbox_secrets
from control.config import (
    ACCOUNT_SECRET_PREFIX,
    BASIC_SECRET_NAME,
    CODEX_SECRET_NAME,
    V1_BOOTSTRAP_SECRET_NAME,
)
from control.resources import (
    INVALID_RESOURCE,
    MCP_REGISTRY_ENV,
    RESOURCE_SECRETS_ENV,
    UNSUPPORTED,
    McpServerSpec,
    ResourceError,
    ResourceRegistry,
    resolve_resources,
    resource_refs,
)


class _FakeSecret:
    @staticmethod
    def from_name(name: str) -> tuple[str, str]:
        return ("secret", name)

    @staticmethod
    def from_dict(data: dict) -> tuple[str, dict]:
        return ("dict", data)


class _FakeModal:
    Secret = _FakeSecret


LINEAR_SPEC = McpServerSpec(
    name="linear",
    url="https://mcp.linear.app/mcp",
    transport="http",
    headers={"Authorization": "Bearer ${env:SBX_LINEAR_API_KEY}"},
)


def _decl(**kwargs):
    class _Decl:
        secrets = kwargs.get("secrets", [])
        mcp = kwargs.get("mcp", [])

    return _Decl()


# ------------------------------------------------------------ from_env


def test_from_env_parses_allowlist_and_registry() -> None:
    registry = ResourceRegistry.from_env(
        {
            RESOURCE_SECRETS_ENV: "sbx-res-openai, sbx-res-linear ,,",
            MCP_REGISTRY_ENV: json.dumps(
                {
                    "linear": {
                        "url": "https://mcp.linear.app/mcp",
                        "headers": {"Authorization": "Bearer ${env:SBX_LINEAR_API_KEY}"},
                    }
                }
            ),
        }
    )
    assert registry.secret_error("sbx-res-openai") is None
    assert registry.secret_error("sbx-res-linear") is None
    assert registry.mcp_server("linear") is not None
    assert registry.mcp_server("other") is None


def test_from_env_malformed_registry_yields_empty() -> None:
    registry = ResourceRegistry.from_env({MCP_REGISTRY_ENV: "not-json{"})
    assert registry.mcp_server("linear") is None
    registry = ResourceRegistry.from_env({MCP_REGISTRY_ENV: json.dumps(["linear"])})
    assert registry.mcp_server("linear") is None


def test_from_env_skips_malformed_entries() -> None:
    registry = ResourceRegistry.from_env(
        {
            MCP_REGISTRY_ENV: json.dumps(
                {
                    "good": {"url": "https://example.com/mcp"},
                    "no-url": {"transport": "http"},
                    "bad name!": {"url": "https://example.com/mcp"},
                    "literal-auth": {
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer RAW_TOKEN"},
                    },
                }
            ),
        }
    )
    assert registry.mcp_server("good") is not None
    assert registry.mcp_server("no-url") is None
    assert registry.mcp_server("bad name!") is None
    # Credential-shaped headers must use ${env:} indirection — a literal
    # Authorization value makes the entry unreferenceable.
    assert registry.mcp_server("literal-auth") is None


def test_mcp_spec_nonsecret_headers_and_providers() -> None:
    registry = ResourceRegistry.from_env(
        {
            MCP_REGISTRY_ENV: json.dumps(
                {
                    "tools": {
                        "url": "https://example.com/mcp",
                        "transport": "sse",
                        "headers": {"X-Tenant": "acme"},
                        "providers": ["devin"],
                    }
                }
            ),
        }
    )
    spec = registry.mcp_server("tools")
    assert spec is not None
    assert spec.transport == "sse"
    assert spec.headers == {"X-Tenant": "acme"}
    assert spec.providers == ("devin",)


# ------------------------------------------------------------ secrets


def test_secret_error_rejects_malformed_names() -> None:
    registry = ResourceRegistry(secrets=("sbx-res-x",))
    assert registry.secret_error("") is not None
    assert registry.secret_error("has space") is not None
    assert registry.secret_error("1starts-digit") is not None
    assert registry.secret_error("a" * 200) is not None
    assert registry.secret_error(None) is not None
    assert registry.secret_error("sbx-res-x") is None


def test_secret_error_rejects_reserved_names(monkeypatch) -> None:
    registry = ResourceRegistry(
        secrets=(
            f"{ACCOUNT_SECRET_PREFIX}acct-1",
            CODEX_SECRET_NAME,
            BASIC_SECRET_NAME,
            V1_BOOTSTRAP_SECRET_NAME,
            "sbx-gh",
            "sbx-res-ok",
        )
    )
    # Even when listed, control-plane credential Secrets are never
    # attachable as session resources.
    assert registry.secret_error(f"{ACCOUNT_SECRET_PREFIX}acct-1") is not None
    assert registry.secret_error(CODEX_SECRET_NAME) is not None
    assert registry.secret_error(BASIC_SECRET_NAME) is not None
    assert registry.secret_error(V1_BOOTSTRAP_SECRET_NAME) is not None
    # Same for a GitHub-bridge Secret: even when allowlisted it stays
    # reserved (the bridge manages it).
    registry_env = ResourceRegistry.from_env(
        {
            "SBX_GITHUB_SECRET_NAME": "sbx-gh",
            RESOURCE_SECRETS_ENV: "sbx-gh,sbx-res-ok",
        }
    )
    assert registry_env.secret_error("sbx-gh") is not None
    assert registry_env.secret_error("sbx-res-ok") is None
    assert registry.secret_error("sbx-res-ok") is None


def test_secret_error_requires_allowlist() -> None:
    registry = ResourceRegistry()  # nothing allowlisted
    assert registry.secret_error("sbx-res-x") is not None


def test_secret_error_env_overrides(monkeypatch) -> None:
    registry = ResourceRegistry.from_env(
        {
            "SBX_CODEX_SECRET_NAME": "custom-codex",
            "SBX_ACCOUNT_SECRET_PREFIX": "custom-acct-",
            RESOURCE_SECRETS_ENV: "custom-codex,custom-acct-9,sbx-res-x",
        }
    )
    assert registry.secret_error("custom-codex") is not None
    assert registry.secret_error("custom-acct-9") is not None
    assert registry.secret_error("sbx-res-x") is None


# ------------------------------------------------------------ resolve


def test_resolve_none_and_empty_decl() -> None:
    registry = ResourceRegistry()
    assert resolve_resources(None, provider="devin", registry=registry) is None
    assert resolve_resources(_decl(), provider="devin", registry=registry) is None


def test_resolve_secrets_ok() -> None:
    registry = ResourceRegistry(secrets=("sbx-res-a", "sbx-res-b"))
    resolved = resolve_resources(
        _decl(secrets=["sbx-res-a", "sbx-res-b", "sbx-res-a"]),
        provider="codex",
        registry=registry,
    )
    assert resolved == {"secrets": ["sbx-res-a", "sbx-res-b"], "mcp": []}


def test_resolve_unknown_secret_invalid_resource() -> None:
    registry = ResourceRegistry(secrets=("sbx-res-a",))
    with pytest.raises(ResourceError) as exc:
        resolve_resources(_decl(secrets=["sbx-res-missing"]), provider="codex", registry=registry)
    assert exc.value.code == INVALID_RESOURCE


def test_resolve_unknown_mcp_invalid_resource() -> None:
    registry = ResourceRegistry(mcp={"linear": LINEAR_SPEC})
    with pytest.raises(ResourceError) as exc:
        resolve_resources(_decl(mcp=["notion"]), provider="devin", registry=registry)
    assert exc.value.code == INVALID_RESOURCE


def test_resolve_mcp_devin_ok() -> None:
    registry = ResourceRegistry(mcp={"linear": LINEAR_SPEC})
    resolved = resolve_resources(_decl(mcp=["linear"]), provider="devin", registry=registry)
    assert resolved == {
        "secrets": [],
        "mcp": [
            {
                "name": "linear",
                "url": "https://mcp.linear.app/mcp",
                "transport": "http",
                "headers": {"Authorization": "Bearer ${env:SBX_LINEAR_API_KEY}"},
            }
        ],
    }


@pytest.mark.parametrize("provider", ["codex", "antigravity", "grok", "opencode"])
def test_resolve_mcp_unsupported_provider(provider: str) -> None:
    registry = ResourceRegistry(mcp={"linear": LINEAR_SPEC})
    with pytest.raises(ResourceError) as exc:
        resolve_resources(_decl(mcp=["linear"]), provider=provider, registry=registry)
    assert exc.value.code == UNSUPPORTED


def test_resolve_mcp_entry_provider_restriction() -> None:
    spec = McpServerSpec(name="grokonly", url="https://example.com/mcp", providers=("grok",))
    registry = ResourceRegistry(mcp={"grokonly": spec})
    with pytest.raises(ResourceError) as exc:
        resolve_resources(_decl(mcp=["grokonly"]), provider="devin", registry=registry)
    assert exc.value.code == UNSUPPORTED


def test_resolve_combined() -> None:
    registry = ResourceRegistry(secrets=("sbx-res-a",), mcp={"linear": LINEAR_SPEC})
    resolved = resolve_resources(
        _decl(secrets=["sbx-res-a"], mcp=["linear"]),
        provider="devin",
        registry=registry,
    )
    assert resolved["secrets"] == ["sbx-res-a"]
    assert [entry["name"] for entry in resolved["mcp"]] == ["linear"]


def test_resource_refs_names_only() -> None:
    resolved = {
        "secrets": ["sbx-res-a"],
        "mcp": [
            {
                "name": "linear",
                "url": "https://mcp.linear.app/mcp",
                "transport": "http",
                "headers": {"Authorization": "Bearer ${env:SBX_LINEAR_API_KEY}"},
            }
        ],
    }
    refs = resource_refs(resolved)
    assert refs == {"secrets": ["sbx-res-a"], "mcp": ["linear"]}
    # The echo carries refs only — no URL or header templates.
    assert "mcp.linear.app" not in json.dumps(refs)
    assert "SBX_LINEAR_API_KEY" not in json.dumps(refs)
    assert resource_refs(None) is None


# ------------------------------------------------------- modal attach


def test_resource_secrets_stack_with_codex_auth(monkeypatch) -> None:
    """A codex resource ref must not strip the shared Codex auth Secret."""
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    spec = SandboxSpec(
        tags={"provider": "codex"},
        resource_secrets=["sbx-res-a"],
    )
    secrets = _sandbox_secrets(_FakeModal, spec)
    assert ("secret", CODEX_SECRET_NAME) in secrets
    assert ("secret", "sbx-res-a") in secrets


def test_resource_secrets_stack_with_account_secret() -> None:
    spec = SandboxSpec(
        tags={"provider": "devin"},
        secrets=["sbx-acct-1"],
        resource_secrets=["sbx-res-linear"],
    )
    secrets = _sandbox_secrets(_FakeModal, spec)
    assert secrets == [("secret", "sbx-acct-1"), ("secret", "sbx-res-linear")]


def test_resource_secrets_deduped() -> None:
    spec = SandboxSpec(
        tags={"provider": "devin"},
        resource_secrets=["sbx-res-a", "sbx-res-a", "sbx-res-b"],
    )
    secrets = _sandbox_secrets(_FakeModal, spec)
    named = [name for tag, name in secrets if tag == "secret"]
    assert named == ["sbx-res-a", "sbx-res-b"]


def test_exec_secrets_reattach_resource_names() -> None:
    """Modal ``exec(env=)`` replaces process env — resource Secrets must be
    re-attached on every exec in that sandbox (and only that sandbox)."""
    backend = ModalBackend()
    backend._secrets_by_sandbox["sb-devin"] = ["sbx-acct-7"]
    backend._resource_secrets_by_sandbox["sb-devin"] = ["sbx-res-a"]
    handle = SandboxHandle(id="sb-devin", root=Path("/work"), tags={"provider": "devin"})
    secrets = backend._exec_secrets(_FakeModal, handle)
    assert ("secret", "sbx-acct-7") in secrets
    assert ("secret", "sbx-res-a") in secrets
    # Another sandbox (no resources recorded) gets none.
    other = SandboxHandle(id="sb-other", root=Path("/work"), tags={"provider": "devin"})
    assert all(name != "sbx-res-a" for tag, name in backend._exec_secrets(_FakeModal, other))
