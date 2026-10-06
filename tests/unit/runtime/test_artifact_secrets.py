"""One artifact-secret policy for capture, checkpoint, restore and reads (real daemon)."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import tarfile
import time
from pathlib import Path

import pytest
from runtime.security.artifacts import collect_known, is_secret_path, secret_reason
from tests.support.runtime import DaemonHarness

KEY = "zen-selected-artifact-key-7f3a9c"  # fake selected credential
ZEN = {"opencode_zen": {"api_key": KEY}}
GH_LIKE = "ghp_" + "Q" * 36
PRIVATE_KEY = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


def start_payload(prompt: str, execution_id: str = "exec_1") -> dict:
    return {
        "provider_id": "opencode",
        "turn_id": "turn_1",
        "execution_id": execution_id,
        "prompt": prompt,
        "model": "opencode/big-pickle",
        "native_binding": None,
        "deadline_seconds": 30,
    }


@pytest.fixture
def rt(tmp_path):
    harness = DaemonHarness(tmp_path / "a")
    restored = harness.op("worktree.restore", "op_restore", {"generation": 0}).json()
    assert restored["status"] == "succeeded"
    harness.base_sha = restored["result"]["base_sha"]
    yield harness
    harness.stop()


def wt(rt) -> Path:
    return rt.base / "work" / "worktree"


def put(rt, rel: str, text: str) -> None:
    target = wt(rt) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def capture(rt, op_id: str, secrets_: dict | None = None) -> dict:
    return rt.op(
        "changes.capture", op_id, {"base_sha": rt.base_sha, "repository": None}, secrets_=secrets_
    ).json()


def checkpoint(rt, op_id: str = "op_snap", secrets_: dict | None = None) -> dict:
    events = rt.events()
    if events:
        rt.post(
            "/rt/ack",
            {"runtime_epoch": rt.daemon.journal.epoch, "through": events[-1]["local_seq"]},
        )
    response = rt.op("snapshot.prepare", op_id, {}, secrets_=secrets_).json()
    assert response["status"] == "succeeded", response
    return response["result"]


def members(snap: dict) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(snap["checkpoint_b64"]))) as tar:
        return {m.name: tar.extractfile(m).read() if m.isfile() else b"" for m in tar.getmembers()}


def run_turn_writing(rt, rel: str, text: str) -> None:
    rt.op("turn.start", "exec_1", start_payload(f"copy [write:{rel}={text}]"), secrets_=ZEN)
    assert rt.wait_op("exec_1")["status"] == "succeeded"


# ------------------------------------------------------------------------ policy unit
@pytest.mark.parametrize(
    "path",
    [".env", "app/.env.local", "auth.json", "keys/server.pem", "deploy/id_ed25519", ".ssh/config"],
)
def test_credential_paths(path) -> None:
    assert is_secret_path(path)


@pytest.mark.parametrize("path", [".env.example", "app/.env.sample", "src/env.py", "README.md"])
def test_templates_and_ordinary_paths(path) -> None:
    assert not is_secret_path(path)


def test_secret_reason_never_returns_the_secret() -> None:
    assert secret_reason(f"x={KEY}".encode(), [KEY]) == "known_credential"
    assert secret_reason(GH_LIKE.encode()) == "secret_pattern"
    assert secret_reason(GH_LIKE.encode(), patterns=False) is None
    assert secret_reason(b"just code") is None


def test_uploaded_auth_json_registers_its_token_values() -> None:
    assert KEY in collect_known({"codex": {"auth_json": json.dumps({"access_token": KEY})}})


def test_git_username_is_not_treated_as_a_selected_secret(rt) -> None:
    from control.application.artifact_secrets import runtime_visible

    credential = {"username": "x-access-token", "password": KEY}
    broker = type(
        "Broker", (), {"inference": lambda _, s: ({}, {}), "source": lambda _, s: credential}
    )()
    assert runtime_visible(broker, {}) == {"redact": [KEY]}
    assert collect_known({"git": credential}) == {KEY}
    put(rt, "example.txt", "Git helpers use x-access-token as the username\n")
    subprocess.run(["git", "add", "example.txt"], cwd=wt(rt), check=True)
    snap = checkpoint(rt, secrets_={"git": credential})
    assert "worktree/example.txt" in members(snap)


@pytest.mark.parametrize("slot", ["inference", "source"])
@pytest.mark.parametrize("change", ["replace", "disconnect"])
def test_historical_credential_is_filtered_after_runtime_restart(rt, db, tmp_path, slot, change):
    from control.application.artifact_secrets import runtime_visible
    from control.domain.ids import new_id
    from tests.support.api import ApiStack, User

    stack = ApiStack(db, tmp_path / "control")
    user = User(stack)
    old, new, unrelated = (
        "REDACTED_A_HISTORY_000",
        "REDACTED_B_HISTORY_000",
        "REDACTED_UNRELATED_000",
    )
    kind, field = ("opencode_zen", "api_key") if slot == "inference" else ("github", "token")
    zen = user.connect("opencode_zen", {"api_key": old if slot == "inference" else "REDACTED_ZEN"})
    selected = zen if slot == "inference" else user.connect("github", {"token": old})
    user.connect(kind, {field: unrelated})
    other = User(stack)
    other.connect(kind, {field: "REDACTED_OTHER_OWNER"})
    stack.drain()
    body = {
        "harness": {"provider_id": "opencode"},
        "executor": {"backend": "local"},
        "connections": {"inference": zen["id"], slot: selected["id"]},
    }
    if slot == "source":
        body["repository"] = {"full_name": "example/repo"}
    created = user.post(f"/api/workspaces/{user.workspace_id}/sessions", body)
    assert created.status_code == 201, created.text
    sid = created.json()["session"]["id"]
    session = db.read(lambda u: u.get("sessions", sid))
    broker = stack.services.execution.credentials
    try:
        if slot == "inference":
            # A real Turn materializes the selected value in an ordinary file.
            rt.op(
                "turn.start",
                "history-turn",
                start_payload(f"[write:ordinary.txt={old}]"),
                secrets_=broker.inference(session)[0],
            )
            assert rt.wait_op("history-turn")["status"] == "succeeded"
        else:
            # Source material is handed to worktree restore, not to the CLI.
            source = broker.source(session)
            assert source["password"] == old
            put(rt, "ordinary.txt", source["password"])
            rt.daemon.known_secrets |= collect_known({"git": source})
        if change == "replace":
            replaced = user.post(
                f"/api/connections/{selected['id']}/credential-versions",
                {"credential": {field: new}, "expected_version": selected["version"]},
            )
            assert replaced.status_code == 201, replaced.text
        else:
            revoked = user.delete(f"/api/connections/{selected['id']}")
            assert revoked.status_code == 200, revoked.text
        rt.stop()
        # Bind the existing daemon to the API's real lease-scoped connector.
        lease = {
            "id": new_id("lease"),
            "workspace_id": user.workspace_id,
            "session_id": sid,
            "backend": "local",
            "allocation_operation_id": new_id("operation"),
            "generation": rt.generation,
            "state": "ready",
            "image_digest": "sha256:test",
            "resource_class": "standard",
        }
        rt.lease_id = lease["id"]
        rt.key = bytes.fromhex(stack.services.live.connector.enrollment_key(lease))
        rt.start()
        db.run(lambda u: u.insert("executor_leases", {**lease, "handle": {"endpoint": rt.url}}))
        assert not rt.daemon.known_secrets
        path = f"/api/sessions/{sid}/files/content?path=ordinary.txt"
        assert other.get(path).status_code == 404
        assert not rt.daemon.known_secrets, "another owner must not reach the runtime"
        # The first authorized read must filter history before any capture/checkpoint.
        read = user.get(path)
        assert read.status_code == 200, read.text
        assert read.json()["redacted"] is True
        assert "REDACTED" in read.json()["content"]
        assert old not in read.text
        bundle = runtime_visible(broker, session)
        assert rt.daemon.known_secrets == set(bundle["redact"])
        assert old in rt.daemon.known_secrets
        assert unrelated not in rt.daemon.known_secrets
        assert "REDACTED_OTHER_OWNER" not in rt.daemon.known_secrets
        # Even an older, unfiltered checkpoint is sanitized on restore.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(wt(rt), arcname="worktree")
        restored = DaemonHarness(tmp_path / "legacy-restore")
        try:
            result = restored.op(
                "worktree.restore",
                "restore-history",
                {"checkpoint_b64": base64.b64encode(buf.getvalue()).decode()},
                secrets_=bundle,
            ).json()
            assert result["status"] == "succeeded", result
            assert not (wt(restored) / "ordinary.txt").exists()
        finally:
            restored.stop()
        snap = checkpoint(rt, "history-checkpoint", secrets_=bundle)
        assert "worktree/ordinary.txt" not in members(snap)
        assert all(old.encode() not in data for data in members(snap).values())
        rt.stop()
        rt.start()
        result = capture(rt, "history-capture", secrets_=bundle)
        assert result["status"] == "failed", "historical material must not escape after restart"
        assert result["result"]["error"]["code"] == "capture_failed"
        assert old in rt.daemon.known_secrets
        assert unrelated not in rt.daemon.known_secrets
        assert "REDACTED_OTHER_OWNER" not in rt.daemon.known_secrets
    finally:
        stack.shutdown()


# ---------------------------------------------------------------------------- capture
def test_capture_excludes_credential_files_but_keeps_templates(rt) -> None:
    put(rt, ".env", "API_KEY=local-dev-value\n")
    put(rt, "svc/.env.production", "DB=postgres://u:p@h/db\n")
    put(rt, "auth.json", '{"k": "v"}\n')
    put(rt, "keys/server.pem", "not even a key\n")
    put(rt, ".env.example", "API_KEY=\n")
    put(rt, "app.py", "print('hi')\n")
    result = capture(rt, "op_cap")
    assert result["status"] == "succeeded", result
    paths = {f["path"] for f in result["result"]["manifest"]["files"]}
    assert paths == {".env.example", "app.py"}
    patch = base64.b64decode(result["result"]["patch_b64"])
    assert b"local-dev-value" not in patch and b"postgres://" not in patch


def test_capture_refuses_selected_credential_copied_into_worktree(rt) -> None:
    run_turn_writing(rt, "notes.txt", KEY)
    result = capture(rt, "op_cap")
    assert result["status"] == "failed"
    assert result["result"]["error"]["code"] == "capture_failed"
    assert "known_credential" in result["result"]["error"]["message"]
    assert KEY not in str(result)


def test_capture_refuses_known_values_supplied_by_control_after_restart(rt, tmp_path) -> None:
    put(rt, "copied.txt", f"token={KEY}\n")
    rt.daemon.known_secrets.clear()  # a restarted runtime has no in-memory set
    assert capture(rt, "op_cap1")["status"] == "succeeded", "nothing to compare against"
    result = capture(rt, "op_cap2", secrets_={"redact": [KEY]})
    assert result["status"] == "failed" and KEY not in str(result)


@pytest.mark.parametrize("content", [GH_LIKE, PRIVATE_KEY])
def test_capture_refuses_token_and_private_key_patterns(rt, content) -> None:
    put(rt, "src/config.txt", f"value = {content}\n")
    result = capture(rt, "op_cap")
    assert result["status"] == "failed"
    assert "secret_pattern" in result["result"]["error"]["message"]


def test_error_messages_never_echo_known_secrets(rt) -> None:
    rt.daemon.known_secrets.add(KEY)
    put(rt, f"notes-{KEY}.txt", "plain\n")
    result = capture(rt, "op_cap")
    assert result["status"] == "failed" and KEY not in str(result)
    status = rt.post("/rt/query", {"kind": "operation.status", "operation_id": "op_cap"}).json()
    assert KEY not in str(status), "the journaled failure is redacted too"


# ------------------------------------------------------------------------- checkpoint
def test_checkpoint_excludes_credential_paths_and_values(rt) -> None:
    run_turn_writing(rt, "notes.txt", KEY)
    put(rt, ".env", "API_KEY=local-dev-value\n")
    put(rt, "nested/auth.json", "{}\n")
    put(rt, ".env.example", "API_KEY=\n")
    put(rt, "token.txt", f"{GH_LIKE}\n")
    put(rt, "app.py", "print('hi')\n")
    snap = checkpoint(rt)
    names = members(snap)
    assert "worktree/app.py" in names and "worktree/.env.example" in names
    for withheld in (".env", "nested/auth.json", "notes.txt", "token.txt"):
        assert f"worktree/{withheld}" not in names, withheld
    assert set(snap["excluded"]) == {".env", "nested/auth.json", "notes.txt", "token.txt"}
    blob = b"".join(names.values())
    assert KEY.encode() not in blob and b"local-dev-value" not in blob
    assert GH_LIKE.encode() not in blob
    assert not any(n.endswith("auth.json") for n in names)


def test_checkpoint_scans_large_files_and_native_state(rt) -> None:
    put(rt, "large.txt", "x" * (8 * 1024 * 1024) + KEY)
    native = rt.base / "state" / "homes" / "sess_test" / ".local/share/opencode/opencode.db"
    native.parent.mkdir(parents=True)
    native.write_text(KEY)
    snap = checkpoint(rt, secrets_={"redact": [KEY]})
    names = members(snap)
    assert "worktree/large.txt" not in names
    assert not any(n.endswith("opencode.db") for n in names)


@pytest.mark.parametrize("packed", [False, True])
def test_checkpoint_refuses_selected_credentials_in_git_objects(rt, packed) -> None:
    put(rt, "copied.txt", KEY)
    assert capture(rt, "op_cap", {"redact": [KEY]})["status"] == "failed"
    put(rt, "copied.txt", "safe now")
    if packed:
        put(rt, "copied.txt", KEY)
        subprocess.run(["git", "add", "copied.txt"], cwd=wt(rt), check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "safe"],
            cwd=wt(rt),
            check=True,
        )
        subprocess.run(["git", "gc", "--prune=never"], cwd=wt(rt), check=True)
        assert list((wt(rt) / ".git/objects/pack").glob("*.pack"))
    result = rt.op("snapshot.prepare", "op_snap", {}).json()
    assert result["status"] == "failed" and KEY not in str(result)
    assert result["result"]["error"]["code"] == "capture_failed"


def test_restore_filters_selected_values_from_legacy_archive(rt, tmp_path) -> None:
    put(rt, "copied.txt", KEY)
    put(rt, ".env.example", "API_KEY=\n")
    native = tmp_path / "native.txt"
    native.write_text(KEY)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(wt(rt), arcname="worktree")
        tar.add(native, arcname="native/sess_test/native.txt")
    other = DaemonHarness(tmp_path / "legacy", lease_id="lease_legacy")
    try:
        result = other.op(
            "worktree.restore",
            "op_r",
            {"checkpoint_b64": base64.b64encode(buf.getvalue()).decode()},
            secrets_={"redact": [KEY]},
        ).json()
        assert result["status"] == "succeeded", result
        assert not (wt(other) / "copied.txt").exists()
        assert (wt(other) / ".env.example").exists()
        assert KEY not in "".join(
            p.read_text() for p in (other.base / "work/.restore").rglob("*.txt")
        )
    finally:
        other.stop()


def test_restore_refuses_git_credentials_before_realizing_worktree(rt, tmp_path) -> None:
    put(rt, "copied.txt", KEY)
    subprocess.run(["git", "add", "copied.txt"], cwd=wt(rt), check=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(wt(rt), arcname="worktree")
    other = DaemonHarness(tmp_path / "refused", lease_id="lease_refused")
    try:
        result = other.op(
            "worktree.restore",
            "op_r",
            {"checkpoint_b64": base64.b64encode(buf.getvalue()).decode()},
            secrets_={"redact": [KEY]},
        ).json()
        assert result["status"] == "failed" and KEY not in str(result)
        assert not wt(other).exists()
        assert not (other.base / "work/.restore").exists()
    finally:
        other.stop()


def test_prepare_fault_and_prior_selected_values_are_redacted(rt, monkeypatch) -> None:
    from runtime.harnesses.protocol import HarnessError

    harness = rt.daemon.harnesses["opencode"]

    def fail(*_):
        raise HarnessError("credential_invalid", f"rejected {KEY}")

    with monkeypatch.context() as patch:
        patch.setattr(harness, "prepare", fail)
        rt.op("turn.start", "bad_prepare", start_payload("hi"), secrets_=ZEN)
    assert KEY not in str(rt.events())
    rt.op(
        "turn.start",
        "next_turn",
        start_payload(KEY, "next_exec"),
        secrets_={"opencode_zen": {"api_key": "REDACTED"}},
    )
    assert rt.wait_op("next_turn")["status"] == "succeeded"
    assert KEY not in str(rt.events())


def test_restore_never_materializes_credential_members(rt, tmp_path) -> None:
    src = tmp_path / "src" / "worktree"
    src.mkdir(parents=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", *a], cwd=src, check=True, capture_output=True
    )
    run("init", "-q")
    (src / "ok.txt").write_text("ok\n")
    run("add", "ok.txt")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    (src / ".env").write_text("API_KEY=legacy\n")
    (src / "sub").mkdir()
    (src / "sub" / "auth.json").write_text("{}\n")
    (src / ".env.example").write_text("API_KEY=\n")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:  # e.g. an archive from before the policy
        tar.add(src, arcname="worktree")
        native = tmp_path / "auth.json"
        native.write_text("{}")
        tar.add(native, arcname="native/sess_test/auth.json")
    other = DaemonHarness(tmp_path / "b", lease_id="lease_two")
    try:
        payload = {"generation": 2, "checkpoint_b64": base64.b64encode(buf.getvalue()).decode()}
        restored = other.op("worktree.restore", "op_r", payload).json()
        assert restored["status"] == "succeeded", restored
        root = tmp_path / "b" / "work" / "worktree"
        assert (root / "ok.txt").exists() and (root / ".env.example").exists()
        assert not (root / ".env").exists() and not (root / "sub" / "auth.json").exists()
        assert not list((tmp_path / "b").rglob("auth.json"))
    finally:
        other.stop()


def test_restore_brings_back_tracked_files_withheld_from_checkpoint(rt, tmp_path) -> None:
    root = wt(rt)
    put(rt, "fixtures/sample.txt", f"{GH_LIKE}\n")
    subprocess.run(["git", "add", "fixtures"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fx"],
        cwd=root,
        check=True,
    )
    snap = checkpoint(rt)
    assert "fixtures/sample.txt" in snap["excluded"]
    other = DaemonHarness(tmp_path / "c", lease_id="lease_three")
    try:
        payload = {"generation": 1, "checkpoint_b64": snap["checkpoint_b64"]}
        assert other.op("worktree.restore", "op_r", payload).json()["status"] == "succeeded"
        restored = tmp_path / "c" / "work" / "worktree" / "fixtures" / "sample.txt"
        assert restored.exists(), "tracked file restored from Git, no phantom deletion"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=restored.parents[1],
            capture_output=True,
            text=True,
        )
        assert status.stdout.strip() == ""
    finally:
        other.stop()


# ------------------------------------------------------------------------------ reads
def test_reads_refuse_credential_paths_and_redact_known_values(rt) -> None:
    run_turn_writing(rt, "notes.txt", KEY)
    put(rt, ".env", "API_KEY=local-dev-value\n")
    put(rt, ".env.example", "API_KEY=\n")
    refused = rt.post("/rt/query", {"kind": "files.read", "path": ".env"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "forbidden"
    assert "local-dev-value" not in refused.text
    notes = rt.post("/rt/query", {"kind": "files.read", "path": "notes.txt"}).json()
    assert notes["redacted"] is True and KEY not in notes["content"]
    assert "REDACTED" in notes["content"]
    template = rt.post("/rt/query", {"kind": "files.read", "path": ".env.example"}).json()
    assert template["content"] == "API_KEY=\n" and template["redacted"] is False


def test_reads_redact_before_truncation(rt) -> None:
    put(rt, "large.txt", "x" * (1_000_000 - 4) + KEY)
    rt.daemon.known_secrets.add(KEY)
    result = rt.post("/rt/query", {"kind": "files.read", "path": "large.txt"}).json()
    assert result["redacted"] and result["truncated"]
    assert not result["content"].endswith(KEY[:4])


def test_terminal_output_redacts_known_values(rt) -> None:
    put(rt, "notes.txt", f"{KEY}\n")
    rt.daemon.known_secrets.add(KEY)
    term = rt.op("terminal.create", "op_t", {}).json()["result"]["terminal_id"]
    rt.op("terminal.input", "op_ti", {"terminal_id": term, "data": "cat notes.txt\n"})
    deadline = time.time() + 10
    seen = ""
    while time.time() < deadline and "REDACTED" not in seen:
        seen = rt.post("/rt/query", {"kind": "terminal.read", "terminal_id": term}).json()["data"]
        time.sleep(0.05)
    assert "REDACTED" in seen and KEY not in seen
    rt.op("terminal.close", "op_tc", {"terminal_id": term})
