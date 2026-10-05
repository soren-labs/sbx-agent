"""ChangeSet canonical manifest + subject_digest (RFC 167 §05).

The digest covers the canonical manifest only: repository, baseline/head
commits and the sorted file entries — never timestamps, URLs or hosts, so
the same subject digests identically on any replica.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from control.domain.changes import (
    CaptureOrigin,
    ChangeSetFile,
    automatic_eligible,
    canonical_manifest,
    subject_digest,
)
from control.domain.errors import DomainError


def _files():
    return [
        ChangeSetFile(
            path="src/b.py",
            file_type="file",
            mode=0o644,
            content_digest="sha256:b",
        ),
        ChangeSetFile(
            path="src/a.py",
            file_type="file",
            mode=0o644,
            content_digest="sha256:a",
        ),
        ChangeSetFile(path="old/c.py", file_type="deleted", mode=0, content_digest=None),
    ]


def _manifest(**over):
    kw = dict(
        repository="soren-labs/sbx-e2e-test",
        projectless_namespace=None,
        base_sha="a" * 40,
        baseline_digest=None,
        head_sha="b" * 40,
        tree_sha="c" * 40,
        files=_files(),
    )
    return canonical_manifest(**{**kw, **over})


def test_canonical_manifest_sorted():
    m = _manifest()
    assert [f["path"] for f in m["files"]] == ["old/c.py", "src/a.py", "src/b.py"]
    assert m["manifest_version"] == 1


def test_subject_digest_stable_and_exact():
    m1 = _manifest()
    assert subject_digest(m1) == subject_digest(_manifest())
    assert subject_digest(m1).startswith("sha256:")
    assert subject_digest(_manifest(head_sha="d" * 40)) != subject_digest(m1)
    assert subject_digest(_manifest(files=_files()[:2])) != subject_digest(m1)


def test_subject_digest_is_canonical_json():
    m = _manifest()
    expect = (
        "sha256:"
        + hashlib.sha256(json.dumps(m, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    assert subject_digest(m) == expect


def test_path_validation():
    for bad in ("/abs.py", "../up.py", "a\\b.py", "tail/", "", ".."):
        with pytest.raises(DomainError):
            ChangeSetFile(path=bad, file_type="file", mode=0o644, content_digest=None)


def test_capture_origin_automatic_eligible():
    assert automatic_eligible(CaptureOrigin.AUTOMATIC, turn_succeeded=True)
    assert automatic_eligible(CaptureOrigin.EXPLICIT, turn_succeeded=True)
    assert not automatic_eligible(CaptureOrigin.SALVAGE, turn_succeeded=True)
    assert not automatic_eligible(CaptureOrigin.AUTOMATIC, turn_succeeded=False)
