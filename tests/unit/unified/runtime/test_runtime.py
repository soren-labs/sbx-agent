import sys
import time

import pytest
from fastapi.testclient import TestClient
from protocol.runtime import OperationFrame, ProtocolError, digest
from runtime.daemon.app import Runtime, create_runtime_app
from runtime.daemon.journal import Journal
from runtime.harnesses.opencode import OpenCodeHarness
from runtime.security.paths import confined
from runtime.security.redaction import Redactor


@pytest.fixture
def runtime(tmp_path):
    fake = tmp_path / "official_cli.py"
    fake.write_text("""import json,sys,pathlib,os,time
args=sys.argv
home=pathlib.Path(os.environ['HOME'])/'.local/share/opencode'
home.mkdir(parents=True,exist_ok=True)
(home/'opencode.db').write_text('native fixture')
sid=args[args.index('--session')+1] if '--session' in args else 'native_one'
if 'hang' in args: time.sleep(10)
if 'mismatch' in args: sid='other'
for obj in [dict(type='step_start',sessionID=sid),
            dict(type='text',sessionID=sid,part=dict(id='p1',text='Hello')),
            dict(type='text',sessionID=sid,part=dict(id='p1',text='Hello world')),
            dict(type='step_finish',sessionID=sid,part=dict(reason='stop'))]:
 print(json.dumps(obj),flush=True)
""")

    def harness(_):
        return OpenCodeHarness(binary=(sys.executable, str(fake)))

    return Runtime(tmp_path / "lease", "sess_one", "lease_one", 1, "REDACTED", harness)


def frame(operation="op1", prompt="hi", native=None, **changes):
    payload = {
        "turn_id": "turn_one",
        "execution_id": "exec_one",
        "prompt": prompt,
        "native_id": native,
        "provider_id": "opencode",
        "credential_bundle": {"api_key": "REDACTED"},
    }
    return OperationFrame(
        operation_id=operation,
        operation_kind="turn.resume" if native else "turn.start",
        session_id="sess_one",
        lease_id="lease_one",
        lease_generation=1,
        resource_fence=changes.pop("resource_fence", 1),
        grant_id="grant_one",
        grant_expires_at=time.time() + 30,
        request_digest=digest(payload),
        payload=payload,
        **changes,
    )


def wait(runtime, operation):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        row = runtime.journal.get(operation)
        if row["state"] == "terminal":
            return row["result"]
        time.sleep(0.01)
    raise AssertionError("operation did not stop")


def test_native_resume_and_snapshot_parts(runtime):
    first = frame()
    runtime.accept(first)
    result = wait(runtime, "op1")
    assert result["outcome"] == "success"
    assert result["native_id"] == "native_one"
    runtime.accept(first)
    count = runtime.journal.watermark
    runtime.accept(frame("op2", native=result["native_id"]))
    second = wait(runtime, "op2")
    assert second["outcome"] == "success" and second["native_id"] == result["native_id"]
    events = runtime.journal.events()
    updates = [x for x in events if x["type"] == "message.part_updated"]
    assert [x["payload"]["text"] for x in updates[:2]] == ["Hello", "Hello world"]
    assert count < runtime.journal.watermark
    runtime.accept(frame("op3", prompt="mismatch", native=result["native_id"]))
    assert wait(runtime, "op3")["error"] == "context_mismatch"


def test_runtime_crash_does_not_relaunch_and_fences(tmp_path):
    journal = Journal(tmp_path / "state")
    intent = frame()
    assert journal.accept(intent)
    journal.update("op1", "starting")
    epoch = journal.epoch
    rebuilt = Journal(tmp_path / "state")
    assert rebuilt.epoch == epoch
    assert not rebuilt.accept(intent)
    assert rebuilt.get("op1")["state"] == "starting"
    with pytest.raises(ProtocolError, match="idempotency_conflict"):
        rebuilt.accept(frame(prompt="changed"))
    with pytest.raises(ProtocolError, match="version_conflict"):
        rebuilt.accept(frame("old", resource_fence=0))
    with pytest.raises(ProtocolError, match="runtime_incompatible"):
        rebuilt.accept(frame("incompatible", protocol_major=2))


def test_cancel_stops_and_secret_spool(runtime):
    runtime.accept(frame(prompt="hang"))
    time.sleep(0.1)
    runtime.supervisor.stop("op1")
    result = wait(runtime, "op1")
    assert result["outcome"] == "cancelled" and result["stopped"]
    assert runtime.supervisor.process is None
    sample = Redactor(["hidden-value"]).clean(
        {"api_key": "hidden-value", "text": "echo hidden-value"}
    )
    assert sample == {"api_key": "REDACTED", "text": "echo REDACTED"}


def test_pressure_paths_and_auth(runtime, tmp_path):
    client = TestClient(create_runtime_app(runtime))
    assert client.get("/hello").status_code == 403
    assert client.get("/hello", headers={"Authorization": "Bearer REDACTED"}).status_code == 200
    (runtime.worktree / "link").symlink_to(tmp_path)
    with pytest.raises(ProtocolError, match="forbidden"):
        confined(runtime.worktree, "link/file")
    for bad in ("../secret", "/etc/passwd"):
        with pytest.raises(ProtocolError):
            confined(runtime.worktree, bad)
    runtime.journal.max_spool_bytes = 10
    with pytest.raises(ProtocolError, match="spool_pressure"):
        runtime.journal.append("op1", "diagnostic.reported", {"message": "large enough"})
    assert runtime.journal.watermark == 0

    manifest = OpenCodeHarness().describe()
    assert manifest.capabilities["steer"].status == "unsupported"
    assert OpenCodeHarness(cli_version="unknown").describe().support_tier == "disabled"


def test_invalid_runtime_requests_do_not_echo_credentials(runtime):
    client = TestClient(create_runtime_app(runtime))
    body = frame().model_dump()
    body["lease_generation"] = {"credential_bundle": {"api_key": "REDACTED"}}
    response = client.post("/operations", json=body, headers={"Authorization": "Bearer REDACTED"})
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request"}
    assert "credential_bundle" not in response.text


def test_checkpoint_restore_preserves_worktree_native_identity(runtime, tmp_path):
    from runtime.daemon.snapshots import capture, restore

    runtime.accept(frame())
    result = wait(runtime, "op1")
    (runtime.worktree / "saved.txt").write_text("saved marker")
    sealed = capture(runtime)
    replacement = Runtime(
        tmp_path / "replacement",
        "sess_one",
        "lease_two",
        2,
        "REDACTED",
        runtime.supervisor.harness_factory,
    )
    restore(replacement, sealed["manifest"])
    assert (replacement.worktree / "saved.txt").read_text() == "saved marker"
    assert replacement.journal.epoch != runtime.journal.epoch
    OpenCodeHarness().validate_native_state(replacement.root / "native-home", result["native_id"])
    sealed["manifest"]["files"][0]["digest"] = "tampered"
    with pytest.raises(ProtocolError, match="capture_failed"):
        restore(replacement, sealed["manifest"])
    assert (replacement.worktree / "saved.txt").read_text() == "saved marker"


def test_cancel_before_spawn_retains_final_stop_evidence(runtime):
    accepted = frame("before-spawn")
    runtime.journal.accept(accepted)
    runtime.supervisor.stop(accepted.operation_id)
    row = runtime.journal.get(accepted.operation_id)
    assert row["state"] == "terminal"
    assert row["result"]["final_watermark"] == 1
    assert runtime.journal.events()[0]["type"] == "execution.stopped"
    assert runtime.journal.metadata("generation") == "1"


def test_pressure_reserves_stop_lane_and_bounds_operation_results(tmp_path):
    journal = Journal(tmp_path / "bounded", max_spool_bytes=10, max_journal_bytes=1000)
    with pytest.raises(ProtocolError, match="spool_pressure"):
        journal.append("op", "message.part_updated", {"text": "long output"})
    seq = journal.append("op", "execution.stopped", {"stopped": True})
    assert seq == 1
    with pytest.raises(ProtocolError, match="invalid_cursor"):
        journal.ack(seq + 1)
    journal.accept(frame())
    with pytest.raises(ProtocolError, match="quota_exhausted"):
        journal.update("op1", "terminal", result={"text": "x" * 2000})
    assert journal.get("op1")["state"] == "accepted"


def test_checkpoint_internal_symlink_integrity_and_escape(runtime, tmp_path):
    from runtime.daemon.snapshots import capture, restore

    (runtime.worktree / "target").write_text("preserved")
    (runtime.worktree / "link").symlink_to("target")
    sealed = capture(runtime)
    replacement = Runtime(tmp_path / "restore", "sess_one", "lease_two", 2, "REDACTED")
    restore(replacement, sealed["manifest"])
    assert (replacement.worktree / "link").is_symlink()
    assert (replacement.worktree / "link").read_text() == "preserved"
    (runtime.worktree / "outside").symlink_to(tmp_path / "outside")
    with pytest.raises(ProtocolError, match="forbidden"):
        capture(runtime)
