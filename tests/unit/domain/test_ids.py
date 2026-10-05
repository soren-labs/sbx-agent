"""ID vocabulary: prefix discipline + UUIDv7 ordering (RFC 167 §02)."""

from __future__ import annotations

import pytest
from control.domain import ids


@pytest.mark.parametrize(
    "kind,prefix",
    [
        ("user", "usr_"),
        ("workspace", "wsp_"),
        ("project", "prj_"),
        ("project_version", "pver_"),
        ("connection", "con_"),
        ("credential_version", "cred_"),
        ("session", "sess_"),
        ("message", "msg_"),
        ("turn", "turn_"),
        ("execution", "exec_"),
        ("executor_lease", "lease_"),
        ("worktree", "wt_"),
        ("snapshot", "snap_"),
        ("changeset", "cs_"),
        ("delivery", "dlv_"),
        ("delegation", "del_"),
        ("delegation_result", "res_"),
        ("service_instance", "svc_"),
        ("job", "job_"),
        ("event", "evt_"),
    ],
)
def test_prefixes(kind, prefix):
    ident = ids.new_id(kind)
    assert ident.startswith(prefix)
    ids.check_prefix(kind, ident)


def test_wrong_kind_rejected():
    ident = ids.new_id("session")
    with pytest.raises(ValueError):
        ids.check_prefix("turn", ident)


def test_uuid7_orderable():
    import time

    a = ids.new_id("session")
    time.sleep(0.002)  # cross a millisecond boundary
    b = ids.new_id("session")
    assert a < b  # time-ordered across the ms boundary
    assert a.split("_", 1)[1][12] == "7"  # version nibble


def test_unique():
    seen = {ids.new_id("session") for _ in range(1000)}
    assert len(seen) == 1000
