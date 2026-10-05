import base64
import json
import subprocess
from types import SimpleNamespace

import pytest
from control.domain.changes import subject_digest
from control.domain.delegation import result_contract, validate_result
from control.domain.delivery import DEFAULT_POLICY, merge_reasons
from control.domain.errors import DomainError
from protocol.runtime import ProtocolError
from runtime.daemon.changes import apply, capture


def run_git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args]).decode().strip()


def test_capture_binary_deleted_untracked_modes_and_atomic_apply(tmp_path):
    root = tmp_path / "runtime"
    work = root / "worktree"
    work.mkdir(parents=True)
    run_git(work, "init", "-q")
    (work / "deleted").write_text("before")
    (work / "binary").write_bytes(b"\x00before")
    run_git(work, "add", ".")
    run_git(
        work, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "base"
    )
    base = run_git(work, "rev-parse", "HEAD")
    (work / "deleted").unlink()
    (work / "binary").write_bytes(b"\x00after")
    (work / "untracked").write_text("new")
    (work / "untracked").chmod(0o755)
    (work / "link").symlink_to("binary")
    metadata = {"generation": "1"}

    class Journal:
        def metadata(self, key, value=None):
            if value is not None:
                metadata[key] = str(value)
            return metadata[key]

    runtime = SimpleNamespace(
        root=root,
        worktree=work,
        session_id="ses_test",
        journal=Journal(),
        supervisor=SimpleNamespace(known_secrets=set()),
    )
    sealed = capture(
        runtime, {"generation": 1, "base_sha": base, "repository": "https://github.com/a/b"}
    )
    assert sealed["subject_digest"] == subject_digest(sealed["subject"])
    entries = {e["path"]: e for e in sealed["subject"]["files"]}
    assert entries["deleted"]["type"] == "deleted"
    assert entries["link"]["mode"] == "120000"
    assert entries["untracked"]["mode"] == "100755"
    assert base64.b64decode(sealed["contents"]["binary"]) == b"\x00after"
    # Failure validating any content leaves the original Worktree unchanged.
    bad = {
        **sealed,
        "contents": {**sealed["contents"], "binary": base64.b64encode(b"bad").decode()},
    }
    with pytest.raises(ProtocolError):
        apply(runtime, bad)
    assert (work / "binary").read_bytes() == b"\x00after"
    run_git(work, "reset", "--hard", "-q", base)
    run_git(work, "clean", "-fdq")
    assert apply(runtime, sealed)["generation"] == 2
    assert not (work / "deleted").exists()
    assert (work / "link").readlink().as_posix() == "binary"
    with pytest.raises(ProtocolError, match="version_conflict"):
        apply(runtime, sealed)


def test_canonical_digest_and_output_contract_exact_subject():
    subject = {"namespace": "ses_vector", "files": [], "base_sha": None}
    assert (
        subject_digest(subject)
        == "3e03278a6e1493cd4a0117d8e7501ef6241abcac7c66f53cb26fec3610fafca7"
    )
    contract = result_contract("review", "digest", None)
    result = {
        "kind": "ReviewAssessment",
        "subject_digest": "digest",
        "head_sha": None,
        "verdict": "approve",
        "findings": [],
        "checks": [],
    }
    assert validate_result(json.dumps(result), contract) == result
    with pytest.raises(DomainError, match="stale_subject"):
        validate_result(json.dumps({**result, "subject_digest": "other"}), contract)
    with pytest.raises(DomainError, match="output_contract_invalid"):
        validate_result("looks good", contract)


def test_merge_requires_independent_exact_subject_and_remote_head():
    cs = {"state": "ready", "subject_digest": "digest", "head_sha": None}
    delivery = {
        "subject_digest": "digest",
        "mapped_head": "head",
        "remote_head": "head",
        "policy": DEFAULT_POLICY,
    }
    remote = {"head": "head", "draft": False, "mergeable": True, "checks": {}}
    approval = {
        "validated": True,
        "subject_digest": "digest",
        "head_sha": None,
        "kind": "ReviewAssessment",
        "verdict": "approve",
        "independent": True,
        "child_session_id": "child",
    }
    assert merge_reasons(delivery, cs, [approval], remote) == []
    assert "remote_head_changed" in merge_reasons(
        delivery, cs, [approval], {**remote, "head": "new"}
    )
    assert "missing_review" in merge_reasons(
        delivery, cs, [{**approval, "independent": False}], remote
    )
    assert "missing_review" in merge_reasons(
        delivery, cs, [{**approval, "subject_digest": "old"}], remote
    )
    assert "requested_changes" in merge_reasons(
        delivery, cs, [{**approval, "verdict": "request_changes"}], remote
    )
