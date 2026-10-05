"""Pure domain state rules (RFC 02 lifecycle table)."""

from __future__ import annotations

import pytest
from control.domain import sessions as srules
from control.domain.digests import canonical_json, digest_of
from control.domain.errors import DomainError
from control.domain.execution import EXECUTION, LEASE
from control.domain.ids import kind_of, new_id
from control.domain.jobs import JOB_KINDS, TARGET_COLUMNS
from control.domain.worktrees import SNAPSHOT
from control.security.redaction import REDACTED, find_secret_fields, redact


def test_turn_transitions_match_rfc() -> None:
    t = srules.TURN
    assert t.allowed("queued", "preparing") and t.allowed("queued", "cancelled")
    assert t.allowed("preparing", "queued") and t.allowed("preparing", "cancelling")
    assert t.allowed("running", "interrupted")
    assert not t.allowed("cancelling", "succeeded"), "late success after cancel intent"
    assert not t.allowed("queued", "running")
    for terminal in ("succeeded", "failed", "cancelled", "interrupted"):
        assert t.is_terminal(terminal)
        assert not any(t.allowed(terminal, other) for other in t.states)
    with pytest.raises(DomainError) as err:
        t.check("succeeded", "failed")
    assert err.value.code == "invalid_transition"


def test_execution_and_lease_never_revive() -> None:
    for terminal in EXECUTION.terminal:
        assert not any(EXECUTION.allowed(terminal, s) for s in EXECUTION.states)
    assert LEASE.allowed("quiescing", "ready")
    assert not LEASE.allowed("released", "ready") and not LEASE.allowed("lost", "ready")
    assert SNAPSHOT.allowed("preparing", "ready") and not SNAPSHOT.allowed("ready", "failed")


def test_session_lifecycle_close_irreversible() -> None:
    s = srules.SESSION
    assert s.allowed("open", "archived") and s.allowed("archived", "open")
    assert s.allowed("archived", "closed") and not s.allowed("closed", "open")


def test_activity_is_projection() -> None:
    assert srules.activity_of("open", "running", 0, None) == "running"
    assert srules.activity_of("open", None, 2, None) == "queued"
    assert srules.activity_of("open", None, 0, "interrupted") == "attention"
    assert srules.activity_of("open", None, 0, "succeeded") == "awaiting_input"
    assert srules.activity_of("archived", None, 0, None) == "idle"


def test_ids_are_prefixed_uuid7_and_sortable() -> None:
    a, b = new_id("session"), new_id("session")
    assert a.startswith("sess_") and len(a) == 5 + 32
    assert a[5:17] <= b[5:17]
    assert a[5 + 12] == "7"
    assert kind_of(a) == "session" and kind_of(new_id("delivery")) == "delivery"


def test_every_job_kind_has_typed_target() -> None:
    for family in JOB_KINDS.values():
        assert family in TARGET_COLUMNS
    required = {
        "turn.dispatch",
        "execution.reconcile",
        "executor.allocate",
        "executor.reconcile",
        "executor.release",
        "environment.build",
        "worktree.restore",
        "snapshot.capture",
        "changeset.capture",
        "changeset.apply",
        "delivery.perform",
        "delivery.reconcile",
        "delivery.merge",
        "delegation.publish_result",
        "delegation.wake_waiters",
        "delegation.cancel",
        "connection.validate",
        "connection.provision",
        "credential.refresh",
        "service.ensure",
        "service.stop",
        "retention.cleanup",
    }
    assert required <= set(JOB_KINDS)


def test_canonical_digest_is_order_independent() -> None:
    assert canonical_json({"b": 1, "a": [1, "é"]}) == canonical_json({"a": [1, "é"], "b": 1})
    assert digest_of({"a": 1}).startswith("sha256:")


def test_structured_redaction() -> None:
    payload = {"token_secret": "abc123456789", "nested": {"api_key": "zzzz"}, "input_tokens": 5}
    assert find_secret_fields(payload) == ["token_secret", "nested.api_key"]
    out = redact(payload, known=["sk-live-value-123"])
    assert out["token_secret"] == REDACTED and out["nested"]["api_key"] == REDACTED
    assert out["input_tokens"] == 5
    assert redact("Bearer abcdefghijklmnopqrstuvwxyz") == REDACTED
    assert redact("x sk-live-value-123 y", known=["sk-live-value-123"]) == f"x {REDACTED} y"
