"""``GET /v1/providers`` — the catalog/runtime/connection split (SOR-212/215).

The endpoint lists every contract provider regardless of deployment
selection or account presence. ``connection`` derives only from live
account records; ``runtime`` reads deploy-written evidence (injected via
``app.state.runtime_store`` here); ``status``/catalog fields never infer
"connected" from config.
"""

from __future__ import annotations

from typing import Any

import pytest
from control.runtime_state import (
    STATUS_DEGRADED,
    STATUS_READY,
    InMemoryRuntimeStore,
    ProviderRuntimeRecord,
)
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import V1Env, seed_account

CONTRACT_PROVIDERS = {"codex", "antigravity", "grok", "opencode", "devin"}


def _rows(resp_json: dict[str, Any]) -> dict[str, Any]:
    return {row["provider"]: row for row in resp_json["providers"]}


def _record(provider: str, status: str, **kw: Any) -> ProviderRuntimeRecord:
    return ProviderRuntimeRecord(
        provider=provider,
        status=status,
        image=kw.get("image", f"sbx-runtime-{provider}"),
        version=kw.get("version", "1.2.3"),
        detail=kw.get("detail", ""),
        updated_at=kw.get("updated_at", "2026-01-01T00:00:00+00:00"),
    )


def test_lists_every_supported_provider_with_zero_accounts(
    client: TestClient, v1_env: V1Env, auth: dict[str, str]
) -> None:
    v1_env.registry.remove("acct-codex-1")
    resp = client.get("/v1/providers", headers=auth)
    assert resp.status_code == 200, resp.text
    rows = _rows(resp.json())
    assert set(rows) == CONTRACT_PROVIDERS
    for provider, row in rows.items():
        assert row["status"] == "available"
        assert row["connection"]["status"] == "not_connected"
        assert row["connection"]["accounts_total"] == 0
        assert row["distribution"]["kind"] in {"npm", "bundle", "host-binary"}
        assert row["cli_path"].startswith("/")


def test_connected_only_when_an_account_can_take_work(
    client: TestClient, v1_env: V1Env, auth: dict[str, str]
) -> None:
    # conftest seeds an active codex account — codex is connected.
    rows = _rows(client.get("/v1/providers", headers=auth).json())
    codex = rows["codex"]
    assert codex["connection"]["status"] == "connected"
    assert codex["connection"]["accounts_total"] == 1
    assert codex["connection"]["accounts_available"] == 1
    # Other providers are not connected — nothing imported.
    assert rows["grok"]["connection"]["status"] == "not_connected"


def test_connection_degraded_when_accounts_cannot_take_work(
    client: TestClient, v1_env: V1Env, auth: dict[str, str]
) -> None:
    seed_account(v1_env, "acct-grok-1", provider="grok", status="cooling")
    rows = _rows(client.get("/v1/providers", headers=auth).json())
    grok = rows["grok"]
    assert grok["connection"]["status"] == "degraded"
    assert grok["connection"]["accounts_total"] == 1
    assert grok["connection"]["accounts_available"] == 0


def test_connection_connected_by_declared_models_not_models_surface(
    client: TestClient, v1_env: V1Env, auth: dict[str, str]
) -> None:
    """A busy-but-active account still counts toward ``connected`` only via
    free slots: set the account at max concurrency."""
    v1_env.registry.set_running("acct-codex-1", 1)
    rows = _rows(client.get("/v1/providers", headers=auth).json())
    assert rows["codex"]["connection"]["status"] == "degraded"
    assert rows["codex"]["connection"]["accounts_available"] == 0


def test_runtime_disabled_for_unselected_providers(
    client: TestClient, v1_env: V1Env, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBX_PROVIDERS", "codex,grok")
    rows = _rows(client.get("/v1/providers", headers=auth).json())
    assert rows["codex"]["runtime"]["enabled"] is True
    assert rows["grok"]["runtime"]["enabled"] is True
    for provider in ("devin", "opencode", "antigravity"):
        assert rows[provider]["runtime"]["enabled"] is False
        assert rows[provider]["runtime"]["status"] == "disabled"
    # A stale ready record for a deselected provider stays disabled.
    v1_env.app.state.runtime_store = InMemoryRuntimeStore((_record("devin", STATUS_READY),))
    rows = _rows(client.get("/v1/providers", headers=auth).json())
    assert rows["devin"]["runtime"]["status"] == "disabled"


def test_runtime_ready_and_degraded_records(
    client: TestClient, v1_env: V1Env, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBX_PROVIDERS", "codex,grok")
    v1_env.app.state.runtime_store = InMemoryRuntimeStore(
        (
            _record("codex", STATUS_READY, image="sbx-runtime-x", version="0.9.1"),
            _record("grok", STATUS_DEGRADED, detail="grok CLI not found"),
        )
    )
    rows = _rows(client.get("/v1/providers", headers=auth).json())
    codex_rt = rows["codex"]["runtime"]
    assert codex_rt["status"] == "ready"
    assert codex_rt["image"] == "sbx-runtime-x"
    assert codex_rt["version"] == "0.9.1"
    assert codex_rt["updated_at"] == "2026-01-01T00:00:00+00:00"
    grok_rt = rows["grok"]["runtime"]
    assert grok_rt["status"] == "degraded"
    assert grok_rt["detail"] == "grok CLI not found"


def test_runtime_unknown_without_evidence_is_backcompat(
    client: TestClient, v1_env: V1Env, auth: dict[str, str]
) -> None:
    """Pre-SOR-212 deployments keep no records — enabled providers report
    ``unknown`` rather than silently ``ready``."""
    rows = _rows(client.get("/v1/providers", headers=auth).json())
    codex_rt = rows["codex"]["runtime"]  # enabled by default SBX_PROVIDERS
    assert codex_rt["status"] == "unknown"
    assert codex_rt["version"] is None


def test_runtime_record_for_deselected_provider_ignored(
    client: TestClient, v1_env: V1Env, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBX_PROVIDERS", "codex")
    v1_env.app.state.runtime_store = InMemoryRuntimeStore((_record("grok", STATUS_READY),))
    rows = _rows(client.get("/v1/providers", headers=auth).json())
    assert rows["grok"]["runtime"]["status"] == "disabled"
    assert rows["grok"]["runtime"]["enabled"] is False


def test_unreadable_runtime_store_never_500s(
    client: TestClient, v1_env: V1Env, auth: dict[str, str]
) -> None:
    class _BrokenStore:
        def get(self, provider: str) -> Any:
            raise RuntimeError("modal unreachable")

    v1_env.app.state.runtime_store = _BrokenStore()
    resp = client.get("/v1/providers", headers=auth)
    assert resp.status_code == 200, resp.text
    rows = _rows(resp.json())
    assert rows["codex"]["runtime"]["status"] == "unknown"


def test_catalog_carries_spec_truth(
    client: TestClient, v1_env: V1Env, auth: dict[str, str]
) -> None:
    rows = _rows(client.get("/v1/providers", headers=auth).json())
    assert rows["codex"]["support"] == "stable"
    assert rows["codex"]["distribution"]["kind"] == "npm"
    assert rows["codex"]["distribution"]["local_assisted"] is False
    assert rows["codex"]["credential_files"] == [".codex/auth.json"]
    assert rows["grok"]["distribution"]["kind"] == "host-binary"
    assert rows["grok"]["distribution"]["local_assisted"] is True
    assert rows["devin"]["distribution"]["kind"] == "bundle"
    assert "swe-2-high" in rows["devin"]["default_models"]


def test_requires_agents_scope(client: TestClient) -> None:
    resp = client.get("/v1/providers")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
