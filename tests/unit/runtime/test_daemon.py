"""sbx-runtime daemon behavior against a fake official OpenCode CLI (A06/A08/A13/A14/A15/A27)."""

from __future__ import annotations

import time

import pytest
from tests.support.runtime import DaemonHarness

ZEN = {"opencode_zen": {"api_key": "zen-test-key-1234567890"}}


def start_payload(prompt: str, execution_id: str = "exec_1", binding=None) -> dict:
    return {
        "provider_id": "opencode",
        "turn_id": "turn_1",
        "execution_id": execution_id,
        "prompt": prompt,
        "model": "opencode/big-pickle",
        "native_binding": binding,
        "deadline_seconds": 30,
    }


@pytest.fixture
def rt(tmp_path):
    harness = DaemonHarness(tmp_path)
    assert (
        harness.op("worktree.restore", "op_restore", {"generation": 0}).json()["status"]
        == "succeeded"
    )
    yield harness
    harness.stop()


def types(events):
    return [e["type"] for e in events]


def test_hello_reports_epoch_manifests_and_rejects_incompatible_major(rt) -> None:
    hello = rt.post("/rt/hello", {"protocol_majors": [1]}).json()
    assert hello["lease_id"] == "lease_test" and hello["runtime_epoch"].startswith("rte_")
    providers = {h["provider_id"]: h for h in hello["harnesses"]}
    assert providers["opencode"]["capabilities"]["steer"]["status"] == "unsupported"
    bad = rt.post("/rt/hello", {"protocol_majors": [2]})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "runtime_incompatible"


def test_grants_and_fences(rt) -> None:
    assert rt.post("/rt/hello", {}, token="garbage").status_code == 401
    stale = rt.post("/rt/hello", {}, token=rt.token(generation=0))
    assert stale.json()["error"]["code"] == "stale_fence"
    expired = rt.post("/rt/hello", {}, token=rt.token(ttl=-5))
    assert expired.status_code == 401
    read_only = rt.post(
        "/rt/op",
        {"operation_kind": "lease.renew", "lease_generation": 1},
        token=rt.token(scope="read"),
    )
    assert read_only.status_code == 403


def test_turn_streams_redacted_evidence_and_native_resume(rt) -> None:
    first = rt.op(
        "turn.start",
        "exec_1",
        start_payload("remember ALPHA [write:notes.txt=alpha] key zen-test-key-1234567890"),
        secrets_=ZEN,
    )
    assert first.json()["status"] == "accepted"
    assert rt.wait_op("exec_1")["status"] == "succeeded"
    events = rt.events()
    assert types(events)[0] == "execution.started"
    assert types(events)[-2:] == ["execution.observed_terminal", "execution.stopped"]
    assert events[-1]["payload"]["final_local_seq"] == events[-1]["local_seq"]
    assert "zen-test-key-1234567890" not in str(events), "known secret must be redacted"
    native = next(e for e in events if e["type"] == "execution.native_bound")["payload"][
        "native_id"
    ]
    parts = [e for e in events if e["type"].startswith("message.part")]
    assert [p["payload"]["revision"] for p in parts] == [1, 2] and parts[1]["payload"][
        "mode"
    ] == "replace"
    assert any(e["type"] == "usage.observed" for e in events)
    auth = rt.base / "state" / "homes" / "sess_test" / ".local" / "share" / "opencode" / "auth.json"
    assert not auth.exists(), "credential scrubbed after the Turn"
    assert (rt.base / "work" / "worktree" / "notes.txt").read_text() == "alpha\n"
    last = events[-1]["local_seq"]
    rt.op(
        "turn.start",
        "exec_2",
        start_payload(
            "what did I say? [recall]", "exec_2", {"provider_id": "opencode", "native_id": native}
        ),
        secrets_=ZEN,
    )
    assert rt.wait_op("exec_2")["status"] == "succeeded"
    second = rt.events(after=last)
    assert (
        next(e for e in second if e["type"] == "execution.native_bound")["payload"]["native_id"]
        == native
    )
    text = [e for e in second if e["type"] == "message.part_updated"][-1]["payload"]["content"]
    assert "remember ALPHA" in text


def test_operation_dedupe_conflict_and_lost_start_response(rt) -> None:
    payload = start_payload("hello")
    rt.op("turn.start", "exec_1", payload, secrets_=ZEN)
    replay = rt.op("turn.start", "exec_1", payload, secrets_=ZEN).json()
    assert replay["replayed"] is True, "lost response: same id returns durable status, no relaunch"
    conflict = rt.op("turn.start", "exec_1", start_payload("different"), secrets_=ZEN)
    assert conflict.json()["error"]["code"] == "operation_conflict"
    rt.wait_op("exec_1")
    assert types(rt.events()).count("execution.started") == 1
    tampered = rt.op("lease.renew", "op_x", {"ttl_seconds": 5}, digest="sha256:bad")
    assert tampered.json()["error"]["code"] == "validation_failed"


def test_single_mutating_cli_and_cancel_confirms_stop(rt) -> None:
    rt.op("turn.start", "exec_1", start_payload("[hang]"), secrets_=ZEN)
    time.sleep(0.3)
    busy = rt.op("turn.start", "exec_2", start_payload("other", "exec_2"), secrets_=ZEN)
    assert busy.json()["error"]["code"] == "busy"
    assert (
        rt.op("files.write", "op_w", {"path": "a.txt", "content": "x"}).json()["result"]["error"][
            "code"
        ]
        == "busy"
    )
    cancel = rt.op("turn.cancel", "exec_1:cancel", {"target_operation_id": "exec_1"}).json()
    assert cancel["result"]["stopped"] is True
    events = rt.events()
    terminal = next(e for e in events if e["type"] == "execution.observed_terminal")["payload"]
    assert terminal["cancelled"] is True and terminal["verdict"] == "unknown"
    assert events[-1]["type"] == "execution.stopped" and events[-1]["payload"]["stopped"] is True


def test_invalid_credential_classified(rt) -> None:
    rt.op(
        "turn.start",
        "exec_1",
        start_payload("hi"),
        secrets_={"opencode_zen": {"api_key": "invalid-zen-key-000"}},
    )
    rt.wait_op("exec_1")
    terminal = next(e for e in rt.events() if e["type"] == "execution.observed_terminal")["payload"]
    assert terminal["verdict"] == "failure" and terminal["credential_health"] == "invalid"
    assert terminal["error_code"] == "credential_invalid"


def test_missing_native_session_is_refused_not_forked(rt) -> None:
    rt.op(
        "turn.start",
        "exec_1",
        start_payload("x", binding={"provider_id": "opencode", "native_id": "ses_missing"}),
        secrets_=ZEN,
    )
    rt.wait_op("exec_1")
    terminal = next(e for e in rt.events() if e["type"] == "execution.observed_terminal")["payload"]
    assert terminal["verdict"] == "failure" and terminal["error_code"] == "context_unavailable"


def test_restart_preserves_epoch_and_never_relaunches(tmp_path) -> None:
    """A06/A14: crash mid-Turn -> recovered op is stopped and reported unknown; epoch kept."""
    rt = DaemonHarness(tmp_path)
    rt.op("worktree.restore", "op_restore", {"generation": 0})
    epoch = rt.post("/rt/hello", {}).json()["runtime_epoch"]
    rt.op("turn.start", "exec_1", start_payload("[hang]"), secrets_=ZEN)
    time.sleep(0.4)
    pid = rt.daemon.journal.op_get("exec_1")["pid"]
    rt.stop()  # simulate daemon death; child keeps running
    rt.start()
    hello = rt.post("/rt/hello", {}).json()
    assert hello["runtime_epoch"] == epoch and "exec_1" in hello["recovered_operations"]
    status = rt.post("/rt/query", {"kind": "operation.status", "operation_id": "exec_1"}).json()
    assert status["status"] == "lost" and status["result"]["verdict"] == "unknown"
    import os

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    events = rt.events()
    assert types(events).count("execution.started") == 1
    assert events[-1]["payload"]["recovered"] is True
    rt.stop()


def test_fresh_journal_gets_new_epoch(tmp_path) -> None:
    a = DaemonHarness(tmp_path / "a")
    b = DaemonHarness(tmp_path / "b")
    assert (
        a.post("/rt/hello", {}).json()["runtime_epoch"]
        != b.post("/rt/hello", {}).json()["runtime_epoch"]
    )
    a.stop()
    b.stop()


def test_spool_pressure_stops_cli_without_dropping_terminal_evidence(tmp_path) -> None:
    """A15: bounded spool -> CLI stopped, terminal evidence still recorded."""
    rt = DaemonHarness(tmp_path, max_unacked=20)
    rt.op("worktree.restore", "op_restore", {"generation": 0})
    rt.op("turn.start", "exec_1", start_payload("[flood:200]"), secrets_=ZEN)
    rt.wait_op("exec_1")
    events = rt.events()
    terminal = next(e for e in events if e["type"] == "execution.observed_terminal")["payload"]
    assert terminal["verdict"] == "unknown" and terminal["error_code"] == "spool_pressure"
    assert events[-1]["type"] == "execution.stopped"
    refused = rt.op("turn.start", "exec_2", start_payload("x", "exec_2"), secrets_=ZEN)
    assert refused.json()["error"]["code"] == "spool_pressure"
    ack = rt.post(
        "/rt/ack", {"runtime_epoch": rt.daemon.journal.epoch, "through": events[-1]["local_seq"]}
    ).json()
    assert ack["acked"] == events[-1]["local_seq"]
    rt.stop()


def test_files_paths_cannot_escape(rt) -> None:
    (rt.base / "work" / "worktree" / "ok.txt").write_text("fine")
    assert (
        rt.post("/rt/query", {"kind": "files.read", "path": "ok.txt"}).json()["content"] == "fine"
    )
    for bad in ("../state/journal.sqlite", "/etc/passwd", "a/../../x"):
        resp = rt.post("/rt/query", {"kind": "files.read", "path": bad})
        assert resp.status_code in (404, 409) and "error" in resp.json()
    (rt.base / "work" / "worktree" / "link").symlink_to(rt.base / "state")
    assert "error" in rt.post("/rt/query", {"kind": "files.list", "path": "link"}).json()


def test_checkpoint_excludes_credentials_and_restores_native_state(rt, tmp_path) -> None:
    rt.op("turn.start", "exec_1", start_payload("remember BETA [write:b.txt=beta]"), secrets_=ZEN)
    rt.wait_op("exec_1")
    events = rt.events()
    native = next(e for e in events if e["type"] == "execution.native_bound")["payload"][
        "native_id"
    ]
    rt.post(
        "/rt/ack", {"runtime_epoch": rt.daemon.journal.epoch, "through": events[-1]["local_seq"]}
    )
    snap = rt.op("snapshot.prepare", "op_snap", {}).json()["result"]
    import base64
    import io
    import tarfile

    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode(snap["checkpoint_b64"]))).getnames()
    assert not any(n.endswith("auth.json") for n in names)
    assert any(n.startswith("native/sess_test/") for n in names)
    other = DaemonHarness(tmp_path / "second", lease_id="lease_two")
    restored = other.op(
        "worktree.restore", "op_r2", {"generation": 3, "checkpoint_b64": snap["checkpoint_b64"]}
    ).json()
    assert restored["result"]["restored_from"] == "checkpoint"
    assert (tmp_path / "second" / "work" / "worktree" / "b.txt").read_text() == "beta\n"
    other.op(
        "turn.start",
        "exec_9",
        start_payload(
            "recall [recall]", "exec_9", {"provider_id": "opencode", "native_id": native}
        ),
        secrets_=ZEN,
    )
    assert other.wait_op("exec_9")["status"] == "succeeded"
    text = [e for e in other.events() if e["type"] == "message.part_updated"][-1]["payload"][
        "content"
    ]
    assert "remember BETA" in text
    other.stop()
