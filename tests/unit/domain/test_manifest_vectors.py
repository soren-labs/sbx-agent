"""Golden subject-digest vectors (RFC 05 canonical manifest)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from protocol.manifests import canonical_manifest, subject_digest

VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "docs/specs/unified/manifests/vectors.json").read_text()
)


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_golden_vectors(vector) -> None:
    assert subject_digest(canonical_manifest(**vector["input"])) == vector["subject_digest"]


def test_order_independent_and_commit_rewrite_changes_identity() -> None:
    base = VECTORS[1]["input"]
    shuffled = {**base, "files": list(reversed(base["files"]))}
    assert subject_digest(canonical_manifest(**shuffled)) == VECTORS[1]["subject_digest"]
    rewritten = {**base, "head_sha": "f" * 40}
    assert subject_digest(canonical_manifest(**rewritten)) != VECTORS[1]["subject_digest"]
    with pytest.raises(ValueError):
        canonical_manifest(
            **{**base, "files": [{"path": "../x", "type": "file", "mode": "100644", "digest": "d"}]}
        )
