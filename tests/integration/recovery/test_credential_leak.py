"""SOR-82/A4: credential material must not leak through recovery surfaces.

A canary credential blob is pushed through the real restore path
(``SBX_ACCOUNT_CREDENTIAL`` -> ``$SBX_WORK/home``) to prove the gate is not
vacuous, then every client-visible surface — /v1 response bodies, the
sandbox event logs, the turn record, the durable session record — is
scanned for it, on both success and provider-failure runs.
"""

from __future__ import annotations

import json
import time

import pytest
from control.store import record_to_dict

CANARY = "CANARY-SBX-TOKEN-0123456789abcdef"


def _blob() -> dict:
    return {
        "provider": "codex",
        "files": {".codex/auth.json": json.dumps({"tokens": {"access_token": CANARY}})},
    }


def _assert_no_canary(surfaces: dict[str, str]) -> None:
    hits = [name for name, text in surfaces.items() if CANARY in text]
    assert hits == [], f"credential canary leaked into: {hits}"


def _read(path):
    return path.read_text(encoding="utf-8") if path.is_file() else ""


@pytest.mark.parametrize("scenario", ["success", "nonzero"])
def test_credential_never_reaches_api_or_records(recovery_env, monkeypatch, scenario) -> None:
    env = recovery_env
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", scenario)
    blob = _blob()
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", json.dumps(blob))
    monkeypatch.setenv("SBX_ACCOUNT_ID", "acct-codex-1")
    env.registry.put_credential_blob("acct-codex-1", blob)

    resp = env.post_agent()
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["agent"]["id"]
    run_id = resp.json()["run"]["id"]
    env.wait_run(agent_id, run_id)
    # The durable run verdict publishes while the watcher finishes its
    # post-run window — the SOR-180 eager checkpoint scrubs then re-attaches
    # credential files, so observe the FS only once the record settles.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if env.store.get(agent_id).status != "running":
            break
        time.sleep(0.05)

    handle = env.sandbox_handle(agent_id)
    assert handle is not None
    restored = handle.root / "home" / ".codex" / "auth.json"
    assert restored.is_file(), "credential blob was not restored — gate would be vacuous"
    assert CANARY in restored.read_text(encoding="utf-8")

    rec = env.store.get(agent_id)
    assert rec is not None
    _assert_no_canary(
        {
            "post_agent": resp.text,
            "get_run": env.get_run(agent_id, run_id).text,
            "list_runs": env.list_runs(agent_id).text,
            "get_agent": env.get_agent(agent_id).text,
            "list_agents": env.list_agents().text,
            "usage": env.client.get(f"/v1/agents/{agent_id}/usage", headers=env.auth).text,
            "events.jsonl": _read(handle.root / "events.jsonl"),
            "events.raw.jsonl": _read(handle.root / "events.raw.jsonl"),
            "turns/1.json": _read(handle.root / "turns" / "1.json"),
            "session_record": json.dumps(record_to_dict(rec)),
        }
    )

    # Teardown reads stay clean too (error surfaces included).
    env.teardown(agent_id)
    _assert_no_canary({"get_run_after_teardown": env.get_run(agent_id, run_id).text})


def test_account_verify_response_carries_no_credential(recovery_env) -> None:
    env = recovery_env
    env.registry.put_credential_blob("acct-codex-1", _blob())
    resp = env.client.post("/v1/accounts/acct-codex-1/verify", headers=env.admin_auth)
    assert resp.status_code == 200, resp.text
    _assert_no_canary({"verify_response": resp.text})
    for handle in env.backend.handles:
        _assert_no_canary(
            {
                f"verify-events-{handle.id}": _read(handle.root / "events.jsonl"),
                f"verify-session-{handle.id}": _read(handle.root / "session.json"),
            }
        )
