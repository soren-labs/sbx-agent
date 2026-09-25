"""SOR-130: optional JSON Schema output contract on /v1 agents + runs.

End-to-end through the real ControlPlane + LocalProcessBackend + stub_runner:
the request's ``output_contract`` is normalized at the route, persisted on the
run ledger record, dispatched into the sandbox as ``_contract_<n>.json``, and
the terminal verdict (``structured_output`` + ``output_contract`` metadata) is
computed on the control plane — strict violations surface as
``ERROR``/``contract_violation``, never a silent FINISHED.
"""

from __future__ import annotations

from tests.unit.api_v1.conftest import create_agent, wait_run

SCHEMA = {
    "type": "object",
    "required": ["summary", "ok"],
    "properties": {
        "summary": {"type": "string"},
        "ok": {"type": "boolean"},
        "files": {"type": "array", "items": {"type": "string"}},
    },
}
VALUE = {"summary": "created hello.txt", "ok": True, "files": ["hello.txt"]}


def _contract(enforcement: str = "strict") -> dict:
    return {"schema": SCHEMA, "enforcement": enforcement}


class TestContractValidation:
    def test_malformed_contract_rejected_400(self, client, auth) -> None:
        # Non-dict schema fails pydantic validation — still a contract
        # failure, so it reports the dedicated code, not the generic 400.
        body = {
            "prompt": {"text": "hi"},
            "agent": {"provider": "codex"},
            "output_contract": {"schema": "not an object"},
        }
        resp = client.post("/v1/agents", json=body, headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_output_contract"

    def test_unsupported_schema_keyword_rejected_400(self, client, auth) -> None:
        # A dict schema that parses but uses keywords outside the
        # deterministic validator subset → invalid_output_contract.
        body = {
            "prompt": {"text": "hi"},
            "agent": {"provider": "codex"},
            "output_contract": {"schema": {"type": "nonsense"}},
        }
        resp = client.post("/v1/agents", json=body, headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_output_contract"

    def test_bad_enforcement_rejected(self, client, auth) -> None:
        body = {
            "prompt": {"text": "hi"},
            "agent": {"provider": "codex"},
            "output_contract": {"schema": SCHEMA, "enforcement": "maybe"},
        }
        resp = client.post("/v1/agents", json=body, headers=auth)
        assert resp.status_code in (400, 422)

    def test_extra_contract_field_rejected(self, client, auth) -> None:
        body = {
            "prompt": {"text": "hi"},
            "agent": {"provider": "codex"},
            "output_contract": {"schema": SCHEMA, "bogus": 1},
        }
        resp = client.post("/v1/agents", json=body, headers=auth)
        assert resp.status_code in (400, 422)

    def test_deep_schema_rejected_400_not_500(self, client, auth) -> None:
        """A schema nested deeper than the compile bound is refused as
        ``invalid_output_contract`` — request-time validation must not
        propagate a RecursionError into a 500."""
        schema: dict = {"type": "object"}
        for _ in range(3000):
            schema = {"allOf": [schema]}
        body = {
            "prompt": {"text": "hi"},
            "agent": {"provider": "codex"},
            "output_contract": {"schema": schema},
        }
        resp = client.post("/v1/agents", json=body, headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_output_contract"
        # Same bound on create-run.
        agent = create_agent(client, auth)["agent"]
        resp = client.post(
            f"/v1/agents/{agent['id']}/runs",
            json={"prompt": {"text": "again"}, "output_contract": {"schema": schema}},
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_output_contract"

    def test_non_contract_validation_error_stays_generic(self, client, auth) -> None:
        # Errors outside output_contract keep the canonical malformed code.
        body = {"agent": {"provider": "codex"}, "output_contract": {"schema": "x"}}
        resp = client.post("/v1/agents", json=body, headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"


class TestContractVerdict:
    def test_valid_output_persists_structured_fields(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "structured")
        agent = create_agent(client, auth, output_contract=_contract())["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "FINISHED"
        assert run["error"] is None
        assert run["structured_output"] == VALUE
        # Raw text result stays intact (backward compat).
        assert '"summary"' in run["result"]["text"]
        verdict = run["output_contract"]
        assert verdict["status"] == "valid"
        assert verdict["enforcement"] == "strict"
        assert verdict["schema_digest"].startswith("sha256:")
        assert verdict["extraction"] == "raw"
        assert verdict["violations"] == []

    def test_strict_malformed_output_is_contract_violation(self, client, auth, monkeypatch) -> None:
        # The default "success" fixture's agent_message is plain text.
        agent = create_agent(client, auth, output_contract=_contract())["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"  # never a silent success
        error = run["error"]
        assert error["code"] == "contract_violation"
        assert error["source"] == "control"
        assert error["retryable"] is True
        assert run["structured_output"] is None
        verdict = run["output_contract"]
        assert verdict["status"] == "invalid"
        assert verdict["extraction"] is None
        assert verdict["violations"][0]["code"] == "not_json"
        # The raw text is still the run's result.
        assert run["result"]["text"]

    def test_warn_enforcement_stays_finished_with_diagnostic(
        self, client, auth, monkeypatch
    ) -> None:
        agent = create_agent(client, auth, output_contract=_contract("warn"))["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "FINISHED"
        assert run["error"]["code"] == "contract_violation"  # explicit, not silent
        assert run["output_contract"]["status"] == "invalid"
        assert run["output_contract"]["enforcement"] == "warn"

    def test_create_run_contract_on_followup(self, client, auth, monkeypatch) -> None:
        agent = create_agent(client, auth)["agent"]
        assert wait_run(client, auth, agent["id"], "run-1")["status"] == "FINISHED"
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "structured")
        resp = client.post(
            f"/v1/agents/{agent['id']}/runs",
            json={"prompt": {"text": "follow up"}, "output_contract": _contract()},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        run = wait_run(client, auth, agent["id"], "run-2")
        assert run["status"] == "FINISHED"
        assert run["structured_output"] == VALUE
        assert run["output_contract"]["status"] == "valid"
        # The uncontracted run-1 is unaffected.
        run1 = client.get(f"/v1/agents/{agent['id']}/runs/run-1", headers=auth).json()
        assert run1["status"] == "FINISHED"
        assert run1["structured_output"] is None
        assert run1["output_contract"] is None

    def test_pathological_output_is_diagnosed_not_wedged(self, client, auth, monkeypatch) -> None:
        """The turn payload's ``message`` is sandbox-written and untrusted:
        JSON nested past the eval bound must produce a terminal ERROR +
        contract_violation, never kill the watcher or wedge the run open."""
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "structured_deep")
        agent = create_agent(client, auth, output_contract=_contract())["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
        assert run["error"]["code"] == "contract_violation"
        verdict = run["output_contract"]
        assert verdict["status"] == "invalid"
        # Parsed-then-depth-checked → max_depth; a scanner that trips at
        # parse time → not_json. Both are diagnosable invalids.
        assert verdict["violations"][0]["code"] in ("max_depth", "not_json")
        # A second GET re-renders the persisted record without error.
        again = client.get(f"/v1/agents/{agent['id']}/runs/run-1", headers=auth).json()
        assert again["status"] == "ERROR"

    def test_failed_turn_contract_is_skipped(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
        agent = create_agent(client, auth, output_contract=_contract())["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] == "ERROR"
        assert run["error"]["code"] == "runtime_error"  # provider error, not contract
        assert run["output_contract"]["status"] == "skipped"

    def test_contract_verdict_computed_outside_plane_lock(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        """Regression (SOR-130 review): the verdict judges untrusted
        schema × agent output — it must never run under the plane's global
        lock, so a pathological contract can strand only the turn's watcher
        thread, never the whole control plane."""
        import control.service as service_module

        plane = v1_env.app.state.plane
        real = service_module.apply_output_contract
        owned: list[bool] = []

        def checking(*args, **kwargs):
            owned.append(plane._lock._is_owned())
            return real(*args, **kwargs)

        monkeypatch.setattr(service_module, "apply_output_contract", checking)
        agent = create_agent(client, auth, output_contract=_contract())["agent"]
        run = wait_run(client, auth, agent["id"], "run-1")
        assert run["status"] in ("FINISHED", "ERROR")
        assert owned, "the contract seam never ran for this run"
        assert not any(owned), "apply_output_contract ran under the plane lock"

    def test_contract_verdict_survives_ledger_backfill(
        self, client, auth, v1_env, monkeypatch
    ) -> None:
        """A second GET re-renders from the persisted record — verdict stays."""
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "structured")
        agent = create_agent(client, auth, output_contract=_contract())["agent"]
        first = wait_run(client, auth, agent["id"], "run-1")
        second = client.get(f"/v1/agents/{agent['id']}/runs/run-1", headers=auth).json()
        assert second == first
        assert second["output_contract"]["status"] == "valid"

    def test_runs_list_carries_contract_fields(self, client, auth, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "structured")
        agent = create_agent(client, auth, output_contract=_contract())["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        runs = client.get(f"/v1/agents/{agent['id']}/runs", headers=auth).json()["runs"]
        assert runs[0]["structured_output"] == VALUE
        assert runs[0]["output_contract"]["status"] == "valid"
