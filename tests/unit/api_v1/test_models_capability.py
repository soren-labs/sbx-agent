"""SOR-204: ``GET /v1/models`` capability rows + refresh + create vetoes.

The catalog is injected at ``app.state.capabilities`` with a scripted
probe — no sandbox or real credentials involved. Rows are per
(account, model) and carry the full capability surface.
"""

from __future__ import annotations

from typing import Any

from control.capabilities import (
    CapabilityCatalog,
    DiscoveryResult,
    ModelCapability,
)
from control.ports import Account
from tests.unit.api_v1.conftest import create_agent, seed_account, wait_sandbox


def _cap(
    model: str,
    *,
    efforts: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    default_effort: str | None = None,
    family: str = "",
) -> ModelCapability:
    return ModelCapability(
        model=model,
        display_name=model.title(),
        family=family or model.split("-", 1)[0],
        aliases=aliases,
        reasoning_efforts=efforts,
        effort_native={e: e for e in efforts},
        default_effort=default_effort,
    )


class _ScriptedProbe:
    def __init__(self, *results: DiscoveryResult) -> None:
        self.calls: list[str] = []
        self._results = list(results)
        self._last = results[-1] if results else DiscoveryResult(error="empty")

    def probe(self, account: Account, blob: Any) -> DiscoveryResult:
        self.calls.append(account.id)
        return self._results.pop(0) if self._results else self._last


def _catalog(v1_env: Any, probe: _ScriptedProbe) -> CapabilityCatalog:
    catalog = CapabilityCatalog(
        probe,
        get_account=v1_env.registry.get,
        get_blob=v1_env.registry.get_credential_blob,
        ttl_s=600,
        auto_refresh=False,
    )
    v1_env.app.state.capabilities = catalog
    return catalog


class TestListModels:
    def test_rows_carry_full_capability_surface(
        self, client: Any, auth: dict[str, str], v1_env: Any
    ) -> None:
        catalog = _catalog(
            v1_env,
            _ScriptedProbe(
                DiscoveryResult(
                    models=(
                        _cap("gpt-5.6-luna", efforts=("low", "medium", "high")),
                        _cap(
                            "gpt-5.3-codex-xhigh",
                            aliases=("gpt-5.3-codex",),
                            default_effort="xhigh",
                            efforts=("low", "xhigh"),
                        ),
                    ),
                    default_model="gpt-5.6-luna",
                )
            ),
        )
        catalog.refresh(v1_env.registry.get("acct-codex-1"))

        payload = client.get("/v1/models", headers=auth).json()
        rows = [r for r in payload["models"] if r["provider"] == "codex"]
        assert {r["model"] for r in rows} == {"gpt-5.6-luna", "gpt-5.3-codex-xhigh"}
        for row in rows:
            assert row["account"] == "acct-codex-1"
            assert row["source"] == "discovered"
            assert row["refreshed_at"] is not None
            assert row["stale"] is False
            assert row["availability"] == "available"
            assert row["accounts_available"] == 1
            assert row["display_name"]
            assert row["family"]
        luna = next(r for r in rows if r["model"] == "gpt-5.6-luna")
        assert luna["reasoning_efforts"] == ["low", "medium", "high"]
        assert luna["effort_native"]["high"] == "high"
        deep = next(r for r in rows if r["model"] == "gpt-5.3-codex-xhigh")
        assert deep["aliases"] == ["gpt-5.3-codex"]
        assert deep["default_effort"] == "xhigh"

    def test_declared_fallback_until_first_probe(
        self, client: Any, auth: dict[str, str], v1_env: Any
    ) -> None:
        """No probe yet → the account's declared models, source=declared."""
        _catalog(v1_env, _ScriptedProbe())
        payload = client.get("/v1/models", headers=auth).json()
        rows = [r for r in payload["models"] if r["account"] == "acct-codex-1"]
        assert {r["model"] for r in rows} == {"gpt-5.6-luna", "gpt-5.3-codex"}
        assert all(r["source"] == "declared" for r in rows)
        assert all(r["refreshed_at"] is None for r in rows)

    def test_stale_rows_served_after_failed_refresh(
        self, client: Any, auth: dict[str, str], v1_env: Any
    ) -> None:
        probe = _ScriptedProbe(
            DiscoveryResult(models=(_cap("gpt-5.6-luna", efforts=("low",)),)),
            DiscoveryResult(error="cli exploded"),
        )
        catalog = _catalog(v1_env, probe)
        account = v1_env.registry.get("acct-codex-1")
        catalog.refresh(account)
        catalog.refresh(account)  # fails → last-good stays, marked stale
        payload = client.get("/v1/models", headers=auth).json()
        rows = [r for r in payload["models"] if r["account"] == "acct-codex-1"]
        assert [r["model"] for r in rows] == ["gpt-5.6-luna"]
        assert all(r["stale"] is True for r in rows)
        assert all(r["source"] == "discovered" for r in rows)


class TestRefreshModels:
    def test_admin_refresh(self, client: Any, admin_auth: dict[str, str], v1_env: Any) -> None:
        probe = _ScriptedProbe(
            DiscoveryResult(models=(_cap("gpt-9-turbo", efforts=("low", "xhigh")),))
        )
        _catalog(v1_env, probe)
        resp = client.post("/v1/models/refresh?provider=codex", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["refreshed"] == 1
        rows = [r for r in body["models"] if r["account"] == "acct-codex-1"]
        assert [r["model"] for r in rows] == ["gpt-9-turbo"]
        assert rows[0]["reasoning_efforts"] == ["low", "xhigh"]
        assert probe.calls == ["acct-codex-1"]

    def test_refresh_requires_admin(self, client: Any, auth: dict[str, str], v1_env: Any) -> None:
        _catalog(v1_env, _ScriptedProbe())
        resp = client.post("/v1/models/refresh", headers=auth)
        assert resp.status_code == 403

    def test_refresh_rejects_unknown_provider(
        self, client: Any, admin_auth: dict[str, str], v1_env: Any
    ) -> None:
        _catalog(v1_env, _ScriptedProbe())
        resp = client.post("/v1/models/refresh?provider=claude", headers=admin_auth)
        assert resp.status_code == 400


class TestCreateCapabilityVetoes:
    def test_discovered_snapshot_vetoes_unknown_model(
        self, client: Any, auth: dict[str, str], v1_env: Any
    ) -> None:
        catalog = _catalog(
            v1_env,
            _ScriptedProbe(DiscoveryResult(models=(_cap("gpt-5.6-luna", efforts=("low",)),))),
        )
        catalog.refresh(v1_env.registry.get("acct-codex-1"))
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {
                    "provider": "codex",
                    "account_id": "acct-codex-1",
                    "model": "nonexistent-9",
                },
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unsupported"

    def test_declared_rows_do_not_veto_explicit_model(
        self, client: Any, auth: dict[str, str], v1_env: Any
    ) -> None:
        """Until discovery lands, explicit models stay advisory-only."""
        _catalog(v1_env, _ScriptedProbe())
        body = create_agent(client, auth, agent={"provider": "codex", "model": "gpt-9-x"})
        assert body["agent"]["model"] == "gpt-9-x"

    def test_effort_within_discovered_surface(
        self, client: Any, auth: dict[str, str], v1_env: Any
    ) -> None:
        catalog = _catalog(
            v1_env,
            _ScriptedProbe(DiscoveryResult(models=(_cap("grok-4.6", efforts=("low", "xhigh")),))),
        )
        account = seed_account(v1_env, "acct-grok-1", provider="grok")
        catalog.refresh(account)
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {
                    "provider": "grok",
                    "account_id": "acct-grok-1",
                    "model": "grok-4.6",
                    "reasoning_effort": "xhigh",
                },
            },
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["agent"]["reasoning_effort"] == "xhigh"
        rec = wait_sandbox(v1_env, resp.json()["agent"]["id"])
        assert rec.sandbox_tags["effort_surface"] == "low,xhigh"

    def test_effort_outside_discovered_surface_is_400(
        self, client: Any, auth: dict[str, str], v1_env: Any
    ) -> None:
        catalog = _catalog(
            v1_env,
            _ScriptedProbe(DiscoveryResult(models=(_cap("grok-4.6", efforts=("low",)),))),
        )
        account = seed_account(v1_env, "acct-grok-1", provider="grok")
        catalog.refresh(account)
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {
                    "provider": "grok",
                    "account_id": "acct-grok-1",
                    "model": "grok-4.6",
                    "reasoning_effort": "high",
                },
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unsupported"

    def test_effortless_discovered_model_refuses_effort(
        self, client: Any, auth: dict[str, str], v1_env: Any
    ) -> None:
        """``swe-2-max`` encodes its tier in the id — no effort knob."""
        catalog = _catalog(
            v1_env,
            _ScriptedProbe(
                DiscoveryResult(
                    models=(
                        _cap("swe-2-medium"),
                        _cap("swe-2-max", aliases=("swe-2",), default_effort="max"),
                    )
                )
            ),
        )
        account = seed_account(v1_env, "acct-devin-1", provider="devin")
        catalog.refresh(account)
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {
                    "provider": "devin",
                    "account_id": "acct-devin-1",
                    "model": "swe-2-max",
                    "reasoning_effort": "high",
                },
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unsupported"

    def test_model_alias_resolves(self, client: Any, auth: dict[str, str], v1_env: Any) -> None:
        catalog = _catalog(
            v1_env,
            _ScriptedProbe(DiscoveryResult(models=(_cap("swe-2-high", aliases=("swe-2",)),))),
        )
        account = seed_account(v1_env, "acct-devin-2", provider="devin")
        catalog.refresh(account)
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {
                    "provider": "devin",
                    "account_id": "acct-devin-2",
                    "model": "swe-2",
                },
            },
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["agent"]["model"] == "swe-2"
