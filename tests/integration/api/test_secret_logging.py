"""Selected credentials never reach control-plane logs or surfaced fault messages."""

from __future__ import annotations

import io
import json
import logging

import pytest
from control.jobs.model import Retry
from control.jobs.worker import Worker
from control.security.redaction import KNOWN_SECRETS, _KnownSecrets, install_log_redaction
from tests.support.api import ApiStack, User, inference

ZEN_KEY = "zen-injected-fault-key-7a6b5c4d"  # fake selected credential
GH_LIKE = "ghp_" + "x" * 36


@pytest.fixture
def stack(db, tmp_path):
    s = ApiStack(db, tmp_path)
    yield s
    s.shutdown()


def _stored_text(stack: ApiStack) -> str:
    def fn(u) -> str:
        rows = []
        for table in ("jobs", "job_attempts", "session_events", "connection_observations"):
            rows.extend(u.find(table, {}))
        rows.extend(u.find("connections", {}))
        return repr(rows)

    return stack.db.read(fn)


def test_injected_connector_exception_never_reaches_logs(stack, caplog) -> None:
    def exploding(material, **_):
        raise RuntimeError(f"upstream rejected key {material['api_key']} (Bearer {GH_LIKE})")

    stack.services.connections.validators["inference_api"] = exploding
    user = User(stack)
    caplog.set_level(logging.DEBUG)
    con = user.connect("inference_api", inference(ZEN_KEY))
    stack.drain(rounds=3)
    assert ZEN_KEY not in caplog.text and GH_LIKE not in caplog.text
    assert "RuntimeError" in caplog.text, "the failure itself is still diagnosable"
    job = stack.db.read(
        lambda u: u.find_one("jobs", {"kind": "connection.validate", "connection_id": con["id"]})
    )
    assert job["last_error_code"] == "validation_unavailable"
    stored = _stored_text(stack)
    assert ZEN_KEY not in stored and GH_LIKE not in stored
    assert ZEN_KEY not in user.all_text()


def test_worker_scrubs_raw_handler_exceptions(stack, caplog) -> None:
    user = User(stack)
    con = user.connect("inference_api", inference(ZEN_KEY))  # sealed => registered
    stack.drain()

    def handler(ctx):
        raise RuntimeError(f"bad {ZEN_KEY} and {GH_LIKE}")

    worker = Worker(stack.db, {"connection.validate": handler})
    stack.db.run(
        lambda u: u.enqueue_job(
            workspace_id=user.workspace_id,
            kind="connection.validate",
            target_id=con["id"],
            dedupe_key="fault",
        )
    )
    caplog.set_level(logging.DEBUG)
    assert worker.run_once()
    assert "RuntimeError" in caplog.text and "REDACTED" in caplog.text
    assert ZEN_KEY not in caplog.text and GH_LIKE not in caplog.text
    job = stack.db.read(lambda u: u.find_one("jobs", {"dedupe_key": "fault"}))
    assert job["last_error_code"] == "internal_error" and ZEN_KEY not in repr(job)


def test_root_log_handlers_redact_known_values_and_patterns() -> None:
    KNOWN_SECRETS.add({"api_key": ZEN_KEY})
    logger = logging.getLogger("sbx.test.redaction")
    logger.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        install_log_redaction(logger)
        install_log_redaction(logger)  # idempotent
        assert len(handler.filters) == 1
        try:
            raise RuntimeError(f"bad {ZEN_KEY} and {GH_LIKE}")
        except RuntimeError:
            logger.exception("request failed with %s", ZEN_KEY)
        out = stream.getvalue()
        assert "request failed with REDACTED" in out and "RuntimeError: bad REDACTED" in out
        assert ZEN_KEY not in out and GH_LIKE not in out
    finally:
        logger.removeHandler(handler)


def test_native_auth_json_registers_individual_tokens() -> None:
    registry = _KnownSecrets()
    registry.add({"auth_json": json.dumps({"access_token": ZEN_KEY})})
    assert registry.redact(f"native fault: {ZEN_KEY}") == "native fault: REDACTED"


def test_surfaced_fault_messages_are_redacted(stack) -> None:
    KNOWN_SECRETS.add(ZEN_KEY)
    user = User(stack)
    con = user.connect("inference_api", inference("zen-other-key-for-session-0001"))
    stack.drain()
    body = {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}}
    created = user.post(f"/api/workspaces/{user.workspace_id}/sessions", body)
    assert created.status_code == 201, created.text
    session_id = created.json()["session"]["id"]

    def fn(u):
        session = u.get("sessions", session_id, lock=True)
        u.append_event(
            session,
            "snapshot.failed",
            {"snapshot_id": "snap_x", "kind": "checkpoint", "error": f"tar failed: {ZEN_KEY}"},
            actor="application",
        )

    stack.db.run(fn)
    events = user.get(f"/api/sessions/{session_id}/events").text
    assert ZEN_KEY not in events and "tar failed: REDACTED" in events

    def worker_outcome(ctx):
        return Retry("executor_unavailable", f"connect failed for {ZEN_KEY}")

    stack.db.run(
        lambda u: u.enqueue_job(
            workspace_id=user.workspace_id,
            kind="connection.validate",
            target_id=con["id"],
            dedupe_key="fault",
        )
    )
    Worker(stack.db, {"connection.validate": worker_outcome}).run_once()
    job = stack.db.read(lambda u: u.find_one("jobs", {"dedupe_key": "fault"}))
    assert job["last_error"] == "connect failed for REDACTED"
