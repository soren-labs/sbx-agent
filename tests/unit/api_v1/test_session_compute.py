"""``POST /v1/agents`` compute declaration (SOR-181).

Per-agent sandbox sizing — ``compute.cpu``/``compute.memory_mib`` as a
scalar or ``[request, limit]`` pair — independent of SOR-129
``resources`` (credential/config refs, never sizing). Defaults
``cpu=[1, 2]``/``memory_mib=[1024, 8192]``; malformed, inverted, or
out-of-bounds declarations fail as ``invalid_compute`` before any
sandbox work. The resolved spec is durable session state: it rides
``SandboxSpec.cpu``/``memory_mib`` on create *and* snapshot restore,
survives recovery via ``SessionRecord.compute`` + the ``compute`` tag,
is echoed on the agent view, and the cost estimate bills at the
declared request floor.
"""

from __future__ import annotations

import json

import pytest
from control.compute import ComputeSpec, compute_for_record
from control.config import SANDBOX_USD_PER_S
from control.service import cost_estimate_usd
from tests.unit.api_v1.conftest import create_agent, wait_sandbox
from tests.unit.api_v1.test_verify import RecordingBackend


@pytest.fixture
def spy(v1_env) -> RecordingBackend:
    backend = RecordingBackend(v1_env.backend)
    v1_env.app.state.plane.backend = backend
    return backend


def _post(client, auth, **overrides):
    body = {"prompt": {"text": "hi"}, "agent": {"provider": "codex"}}
    body.update(overrides)
    return client.post("/v1/agents", json=body, headers=auth)


class TestComputeDeclaration:
    def test_omitted_compute_resolves_defaults(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth)
        # The resolved default is still stored + echoed — the durable
        # record is honest about the sandbox's real sizing.
        assert body["agent"]["compute"] == {"cpu": [1.0, 2.0], "memory_mib": [1024, 8192]}
        rec = wait_sandbox(v1_env, body["agent"]["id"])
        assert rec.status in ("idle", "running")
        spec = spy.specs[0]
        assert spec.cpu == (1.0, 2.0)
        assert spec.memory_mib == (1024, 8192)
        # The legacy tag shape gains a ``compute`` entry.
        stored = json.loads(rec.sandbox_tags["compute"])
        assert stored == {"cpu": [1.0, 2.0], "memory_mib": [1024, 8192]}

    def test_declared_compute_reaches_spec(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth, compute={"cpu": [4, 8], "memory_mib": [4096, 16384]})
        assert body["agent"]["compute"] == {"cpu": [4.0, 8.0], "memory_mib": [4096, 16384]}
        rec = wait_sandbox(v1_env, body["agent"]["id"])
        spec = spy.specs[0]
        assert spec.cpu == (4.0, 8.0)
        assert spec.memory_mib == (4096, 16384)
        assert rec.compute == {"cpu": [4.0, 8.0], "memory_mib": [4096, 16384]}

    def test_scalar_and_partial_declarations(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth, compute={"cpu": 4, "memory_mib": 4096})
        assert body["agent"]["compute"] == {"cpu": [4.0, 4.0], "memory_mib": [4096, 4096]}
        body = create_agent(client, auth, compute={"memory_mib": [2048, 8192]})
        assert body["agent"]["compute"] == {
            "cpu": [1.0, 2.0],  # default fills the omitted field
            "memory_mib": [2048, 8192],
        }

    def test_get_echoes_resolved_spec(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth, compute={"cpu": [2, 4], "memory_mib": [2048, 4096]})
        agent_id = body["agent"]["id"]
        got = client.get(f"/v1/agents/{agent_id}", headers=auth)
        assert got.status_code == 200
        assert got.json()["compute"] == {"cpu": [2.0, 4.0], "memory_mib": [2048, 4096]}

    def test_independent_of_resources(self, client, auth, v1_env, spy) -> None:
        """Compute never rides ``resources.secrets``/``mcp``: a valid
        compute declaration can't smuggle sizing past resource
        validation, and compute needs no resource registry entry."""
        # Valid compute + unregistered secret → the resource side still
        # fails on its own channel.
        resp = _post(
            client,
            auth,
            compute={"cpu": 2},
            resources={"secrets": ["sbx-res-missing"]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_resource"
        assert spy.specs == []
        # Compute alone needs no registry and no resources declaration.
        body = create_agent(client, auth, compute={"cpu": 2})
        assert body["agent"]["compute"] == {"cpu": [2.0, 2.0], "memory_mib": [1024, 8192]}
        assert body["agent"]["resources"] is None

    @pytest.mark.parametrize(
        "decl",
        [
            {"cpu": [4, 2]},  # inverted
            {"cpu": [0, 2]},  # below floor
            {"cpu": [1, 65]},  # above ceiling
            {"cpu": []},
            {"cpu": [1, 2, 4]},
            {"memory_mib": 64},
            {"memory_mib": [4096, 2048]},
            {"memory_mib": 300000},
        ],
    )
    def test_invalid_compute(self, client, auth, v1_env, spy, decl) -> None:
        # Well-formed shape, invalid semantics → domain ``invalid_compute``.
        resp = _post(client, auth, compute=decl)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_compute"
        # Validation precedes provisioning — no sandbox was created.
        assert spy.specs == []

    @pytest.mark.parametrize(
        "decl",
        [
            {"cpu": "fast"},
            {"cpu": True},  # strict types refuse bool coercion
            {"cpu": [None, 2]},
            {"memory_mib": 1.5},
            {"memory_mib": [2048, "x"]},
            {"bogus": 1},  # schema extra=forbid
            {"cpu": 2, "bogus": 1},
        ],
    )
    def test_malformed_compute(self, client, auth, v1_env, spy, decl) -> None:
        # Shape-level malformed declarations fail at request validation,
        # same as every other malformed /v1 body (invalid_request).
        resp = _post(client, auth, compute=decl)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"
        assert spy.specs == []


class TestComputeRecovery:
    def test_reprovision_from_record_preserves_sizing(self, client, auth, v1_env, spy) -> None:
        """A post-restart provision reads sizing off the durable record —
        no caller re-passes ``compute``."""
        plane = v1_env.app.state.plane
        sid = plane.open_session(
            owner="k2",
            title=None,
            model=None,
            provider="codex",
            compute={"cpu": [2, 4], "memory_mib": [2048, 4096]},
        )
        plane.provision_session(sid)
        spec = spy.specs[-1]
        assert spec.cpu == (2.0, 4.0)
        assert spec.memory_mib == (2048, 4096)

    def test_compute_survives_record_round_trip(self, client, auth, v1_env, spy) -> None:
        """Serialized → reloaded record resolves the same spec (the shape
        Modal Dict persistence + post-restore reprovisioning relies on)."""
        from control.store import record_from_dict, record_to_dict

        body = create_agent(client, auth, compute={"cpu": [4, 8], "memory_mib": [8192, 8192]})
        rec = wait_sandbox(v1_env, body["agent"]["id"])
        reloaded = record_from_dict(record_to_dict(rec))
        spec = compute_for_record(reloaded.compute, reloaded.sandbox_tags)
        assert spec == ComputeSpec(cpu=(4.0, 8.0), memory_mib=(8192, 8192))

    def test_tag_fallback_when_field_absent(self, client, auth, v1_env, spy) -> None:
        """A record with only the ``compute`` tag (pre-field writer)
        still provisions at its declared sizing."""
        plane = v1_env.app.state.plane
        sid = plane.open_session(
            owner="k3",
            title=None,
            model=None,
            provider="codex",
            compute={"cpu": [3, 3], "memory_mib": [3072, 6144]},
        )
        rec = v1_env.store.get(sid)
        rec.compute = None  # simulate a record written before the field existed
        v1_env.store.put(rec)
        plane.provision_session(sid)
        spec = spy.specs[-1]
        assert spec.cpu == (3.0, 3.0)
        assert spec.memory_mib == (3072, 6144)


class TestComputeCost:
    def test_cost_estimate_uses_declared_floor(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth, compute={"cpu": [8, 16], "memory_mib": [16384, 32768]})
        rec = wait_sandbox(v1_env, body["agent"]["id"])
        spec = compute_for_record(rec.compute, rec.sandbox_tags)
        assert spec is not None
        # Bill at request floor (cpu[0]/memory_mib[0]), above the P0 default.
        assert cost_estimate_usd(60.0, spec) > 60.0 * SANDBOX_USD_PER_S
        # And the *default* (1 core / 1 GiB) estimate still holds when a
        # record carries no compute at all (pre-SOR-181 sessions).
        assert cost_estimate_usd(60.0, None) == round(60.0 * SANDBOX_USD_PER_S, 6)

    def test_public_payload_cost_is_compute_aware(self, client, auth, v1_env, spy) -> None:
        plane = v1_env.app.state.plane
        body = create_agent(client, auth, compute={"cpu": [8, 8], "memory_mib": [16384, 16384]})
        rec = wait_sandbox(v1_env, body["agent"]["id"])
        big = plane.public(rec)
        rec.compute = None
        rec.sandbox_tags = {k: v for k, v in (rec.sandbox_tags or {}).items() if k != "compute"}
        small = plane.public(rec)
        assert big["cost_estimate_usd"] > small["cost_estimate_usd"]
        assert small["cost_estimate_usd"] == cost_estimate_usd(small["sandbox_seconds"], None)
