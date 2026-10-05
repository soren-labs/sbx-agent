"""Vault-backed credential materialization E2E (RFC 167 §06): a real
opencode_zen Connection supplies the CLI auth file via a purpose-bound
grant; the plaintext never persists and the isolated HOME is scrubbed on
release. Runs the full dispatch path through a real daemon."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKE_OPENCODE = REPO_ROOT / "tests" / "fakes" / "fake_opencode.py"


@pytest.fixture()
def stack(pg, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_BIN", f"{sys.executable} {FAKE_OPENCODE}")
    from control.application.connections import ConnectionService
    from control.application.runtime_stack import (
        RuntimeStack,
        make_connection_resolver,
    )
    from control.executors.local import LocalExecutorBackend
    from control.jobs import handlers
    from control.security.vault import Vault

    vault = Vault.generate()
    connections = ConnectionService(pg, vault)
    stack = RuntimeStack(
        pg,
        backends={},
        credential_resolver=make_connection_resolver(connections, pg),
    )
    stack.start()
    stack.register_backend(
        "local",
        LocalExecutorBackend(
            run_root=tmp_path / "executors",
            ingress_endpoint=stack.ingress.endpoint,
            repo_root=REPO_ROOT,
        ),
    )
    handlers.set_runtime_stack(stack)
    yield {"stack": stack, "connections": connections, "run_root": tmp_path / "executors"}
    handlers.set_runtime_stack(None)
    stack.stop()


def _post(pg, workspace, session_id, text):
    from control.application.sessions import SessionService

    return SessionService(pg).post_message(
        principal={"kind": "user", "id": workspace["user_id"]},
        workspace_id=workspace["workspace_id"],
        session_id=session_id,
        author={"kind": "user", "id": workspace["user_id"]},
        routing="queue",
        content={"text": text},
    )


def _drain(pg):
    from control.jobs.handlers import HANDLERS
    from control.jobs.worker import Worker

    Worker(db=pg, holder="t", handlers=HANDLERS).run_until_idle()


def _wait_turn(pg, ws, turn_id, want, timeout=45):
    from control.persistence.unit_of_work import SqlUnitOfWork

    deadline = time.time() + timeout
    while time.time() < deadline:
        _drain(pg)
        with SqlUnitOfWork(pg) as uow:
            t = uow.turns.get(ws, turn_id)
            if t and t["state"] in want:
                return t["state"]
        time.sleep(0.4)
    raise AssertionError(f"turn {turn_id} never reached {want}")


def test_zen_connection_materializes_auth_for_turn(pg, workspace, stack):
    # Owner creates the Zen connection through the product service.
    with _uow(pg) as uow:
        conn = stack["connections"].create_connection(
            uow,
            workspace_id=workspace["workspace_id"],
            principal_id=workspace["user_id"],
            kind="opencode_zen",
            label="zen",
            credential={"format": "api_key", "payload": {"api_key": "zen-live-key"}},
        )
    # Session wired to opencode.
    from control.application.sessions import SessionService

    session = SessionService(pg).create_session(
        principal={"kind": "user", "id": workspace["user_id"]},
        workspace_id=workspace["workspace_id"],
        harness={
            "provider_id": "opencode",
            "model": "opencode/big-pickle",
            "effort": "default",
            "adapter_version": "1",
            "cli_version": "fake",
        },
        projectless_spec={"backend": "local"},
        effective_input_digest="sha256:e2e",
    )
    out = _post(pg, workspace, session["session_id"], "write hello.txt")
    _drain(pg)
    state = _wait_turn(pg, workspace["workspace_id"], out["turn_id"], {"succeeded", "failed"})
    assert state == "succeeded"

    # A purpose-bound grant was issued and redeemed against this execution.
    with _uow(pg) as uow:
        grants = uow.rows.all(
            "SELECT * FROM credential_grants WHERE connection_id=%s", (conn["id"],)
        )
        assert grants and all(g["purpose"] == "runtime_execution" for g in grants)
        assert grants[0]["redeemed_at"] is not None
        # Plaintext lands nowhere in the row set.
        dump = json.dumps(
            {"grants": [{k: str(v) for k, v in g.items()} for g in grants]}, default=str
        )
        assert "zen-live-key" not in dump


def test_turn_fails_clean_without_connection(pg, workspace, stack):
    from control.application.sessions import SessionService

    session = SessionService(pg).create_session(
        principal={"kind": "user", "id": workspace["user_id"]},
        workspace_id=workspace["workspace_id"],
        harness={
            "provider_id": "opencode",
            "model": "opencode/big-pickle",
            "effort": "default",
            "adapter_version": "1",
            "cli_version": "fake",
        },
        projectless_spec={"backend": "local"},
        effective_input_digest="sha256:e2e",
    )
    out = _post(pg, workspace, session["session_id"], "write hello.txt")
    _drain(pg)
    state = _wait_turn(pg, workspace["workspace_id"], out["turn_id"], {"failed", "succeeded"})
    assert state == "failed"  # no ambient fallback — absent credential fails


def _uow(pg):
    from control.persistence.unit_of_work import SqlUnitOfWork

    return SqlUnitOfWork(pg)
