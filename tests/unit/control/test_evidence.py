"""SOR-131: validation evidence artifacts — create/list/get/download, secrets,
size bounds, workspace/run binding, and deterministic binary/text payloads.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from control.artifacts import sha256_hex
from control.evidence import (
    CONTENT_MEMBER,
    EvidenceCorruptError,
    EvidenceError,
    EvidenceNotFoundError,
    EvidenceSecretError,
    EvidenceStore,
    FileEvidenceStore,
    InMemoryEvidenceStore,
    create_evidence,
    download_evidence,
    evidence_dumps,
    evidence_from_dict,
    get_evidence,
    list_evidence,
)
from control.run_store import InMemoryRunStore, RunLedger
from control.workspace import WorkspaceRecord

CANARY = b"CANARY-SBX-TOKEN-0123456789abcdef"


def _clock(start: datetime | None = None):
    base = start or datetime(2026, 9, 15, tzinfo=UTC)
    ticks = iter(range(10_000))
    return lambda: base + timedelta(seconds=next(ticks))


def _stores(tmp_path: Path):
    return [InMemoryEvidenceStore(), FileEvidenceStore(tmp_path / "evidence")]


def _workspace_record(head_sha: str = "h" * 40) -> WorkspaceRecord:
    return WorkspaceRecord(
        agent_id="agent-a",
        repo="https://github.com/soren-labs/sbx-browser",
        base_ref="main",
        base_sha="b" * 40,
        workdir="repo",
        checkout_sha="c" * 40,
        head_sha=head_sha,
    )


def _make(
    store: EvidenceStore,
    *,
    content: bytes | str = b"log line\n",
    logical_type: str = "log",
    media_type: str | None = None,
    evidence_id: str = "ev-test1",
    run_id: str | None = "run-1",
    **kwargs,
):
    defaults = {
        "store": store,
        "logical_type": logical_type,
        "producer_agent_id": "agent-a",
        "producer_run_id": run_id,
        "content": content,
        "evidence_id": evidence_id,
        "clock": _clock(),
    }
    if media_type is not None:
        defaults["media_type"] = media_type
    defaults.update(kwargs)
    return create_evidence(**defaults)


class TestCreate:
    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_text_log_roundtrip(self, tmp_path: Path, kind: str) -> None:
        store = _stores(tmp_path)[kind == "file"]
        evidence = _make(store, content="log line\n", logical_type="log")
        assert evidence.logical_type == "log"
        assert evidence.media_type == "text/plain"
        assert evidence.size == 9
        assert evidence.sha256 == sha256_hex(b"log line\n")
        assert download_evidence(store, evidence.evidence_id) == b"log line\n"
        got = get_evidence(store, evidence.evidence_id)
        assert got.producer_agent_id == "agent-a"
        assert got.producer_run_id == "run-1"

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_binary_screenshot_roundtrip(self, tmp_path: Path, kind: str) -> None:
        store = _stores(tmp_path)[kind == "file"]
        payload = bytes(range(256)) + b"\x89PNG\r\n\x1a\n"
        evidence = _make(
            store,
            content=payload,
            logical_type="screenshot",
            media_type="image/png",
        )
        assert evidence.logical_type == "screenshot"
        assert evidence.media_type == "image/png"
        assert evidence.size == len(payload)
        assert download_evidence(store, evidence.evidence_id) == payload

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_deterministic_manifest_bytes(self, tmp_path: Path, kind: str) -> None:
        store = _stores(tmp_path)[kind == "file"]
        e1 = _make(store, content=b"same\n", logical_type="report")
        e2 = _make(store, content=b"same\n", logical_type="report")
        assert evidence_dumps(e1) == evidence_dumps(e2)

    def test_workspace_and_run_binding(self, tmp_path: Path) -> None:
        store = InMemoryEvidenceStore()
        ledger = RunLedger(InMemoryRunStore())
        ledger.begin(agent_id="agent-a", n=1)
        ws = _workspace_record()
        evidence = _make(
            store,
            content=b"report json",
            logical_type="report",
            workspace=ws,
            ledger=ledger,
            run_n=1,
        )
        assert evidence.workspace_id == "agent-a"
        assert evidence.workspace_repo == ws.repo
        assert evidence.workspace_base_sha == ws.base_sha
        assert evidence.workspace_head_sha == ws.head_sha
        record = ledger.get("agent-a", 1)
        assert record is not None
        assert f"evidence://{evidence.evidence_id}" in record.artifact_refs

    def test_default_media_type_per_logical_type(self, tmp_path: Path) -> None:
        for logical, expected in (
            ("log", "text/plain"),
            ("screenshot", "image/png"),
            ("video", "video/mp4"),
            ("report", "application/json"),
        ):
            ev = _make(
                InMemoryEvidenceStore(),
                logical_type=logical,
                content=b"x",
                evidence_id=f"ev-{logical}",
            )
            assert ev.media_type == expected, logical

    def test_explicit_media_type_overrides_default(self, tmp_path: Path) -> None:
        ev = _make(
            InMemoryEvidenceStore(),
            content=b"x",
            logical_type="log",
            media_type="application/x-ndjson",
            evidence_id="ev-explicit",
        )
        assert ev.media_type == "application/x-ndjson"


class TestSafety:
    def test_secret_value_fails_closed(self, tmp_path: Path) -> None:
        store = InMemoryEvidenceStore()
        with pytest.raises(EvidenceSecretError) as exc:
            _make(
                store,
                content=b"prefix " + CANARY + b" suffix",
                forbidden_values=[CANARY],
                evidence_id="ev-secret",
            )
        assert exc.value.paths == ["content"]

    def test_secret_in_source_path_report(self, tmp_path: Path) -> None:
        store = InMemoryEvidenceStore()
        with pytest.raises(EvidenceSecretError) as exc:
            _make(
                store,
                content=b"leaked " + CANARY,
                source_path="logs/run.log",
                forbidden_values=[CANARY],
                evidence_id="ev-secret-path",
            )
        assert exc.value.paths == ["logs/run.log"]

    def test_denied_source_path_rejected(self, tmp_path: Path) -> None:
        store = InMemoryEvidenceStore()
        with pytest.raises(EvidenceError) as exc:
            _make(
                store,
                content=b"safe",
                source_path=".env",
                evidence_id="ev-denied",
            )
        assert "denied" in str(exc.value).lower()

    def test_size_bound_rejected(self, tmp_path: Path) -> None:
        store = InMemoryEvidenceStore()
        with pytest.raises(EvidenceError) as exc:
            _make(
                store,
                content=b"0123456789",
                max_size=5,
                evidence_id="ev-too-big",
            )
        assert "size" in str(exc.value).lower()

    def test_invalid_logical_type(self, tmp_path: Path) -> None:
        with pytest.raises(EvidenceError):
            _make(InMemoryEvidenceStore(), logical_type="blob", evidence_id="ev-bad-type")

    def test_invalid_media_type(self, tmp_path: Path) -> None:
        with pytest.raises(EvidenceError):
            _make(
                InMemoryEvidenceStore(),
                logical_type="log",
                media_type="not-a-mime",
                evidence_id="ev-bad-media",
            )

    def test_invalid_evidence_id_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(EvidenceError):
            _make(InMemoryEvidenceStore(), evidence_id="../escape")


class TestStores:
    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_missing_evidence(self, tmp_path: Path, kind: str) -> None:
        store = _stores(tmp_path)[kind == "file"]
        with pytest.raises(EvidenceNotFoundError):
            get_evidence(store, "ev-nope")
        with pytest.raises(EvidenceNotFoundError):
            download_evidence(store, "ev-nope")

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_corrupt_manifest(self, tmp_path: Path, kind: str) -> None:
        store = _stores(tmp_path)[kind == "file"]
        _make(store, evidence_id="ev-corrupt")
        if kind == "file":
            path = tmp_path / "evidence" / "ev-corrupt" / "evidence.json"
            path.write_bytes(b"{not json")
        else:
            raw, content = store._items["ev-corrupt"]
            store._items["ev-corrupt"] = (b"{not json", content)
        with pytest.raises(EvidenceCorruptError):
            get_evidence(store, "ev-corrupt")

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_corrupt_content_checksum(self, tmp_path: Path, kind: str) -> None:
        store = _stores(tmp_path)[kind == "file"]
        _make(store, content=b"original", evidence_id="ev-tamper")
        if kind == "file":
            path = tmp_path / "evidence" / "ev-tamper" / CONTENT_MEMBER
            path.write_bytes(b"tampered")
        else:
            raw, content = store._items["ev-tamper"]
            store._items["ev-tamper"] = (raw, b"tampered")
        with pytest.raises(EvidenceCorruptError):
            download_evidence(store, "ev-tamper")

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_list_filters_and_skips_corrupt(self, tmp_path: Path, kind: str) -> None:
        store = _stores(tmp_path)[kind == "file"]
        _make(store, content=b"a", evidence_id="ev-a1-r1", producer_run_id="run-1")
        _make(store, content=b"b", evidence_id="ev-a1-r2", producer_run_id="run-2")
        _make(
            store,
            content=b"c",
            evidence_id="ev-a2-r1",
            producer_agent_id="agent-b",
            producer_run_id="run-1",
        )
        all_ids = {e.evidence_id for e in list_evidence(store)}
        assert all_ids == {"ev-a1-r1", "ev-a1-r2", "ev-a2-r1"}
        assert [e.evidence_id for e in list_evidence(store, agent_id="agent-a")] == [
            "ev-a1-r1",
            "ev-a1-r2",
        ]
        assert [e.evidence_id for e in list_evidence(store, run_id="run-2")] == ["ev-a1-r2"]
        if kind == "file":
            (tmp_path / "evidence" / "ev-a1-r2" / "evidence.json").write_bytes(b"junk")
        else:
            raw, content = store._items["ev-a1-r2"]
            store._items["ev-a1-r2"] = (b"junk", content)
        assert [e.evidence_id for e in list_evidence(store, agent_id="agent-a")] == ["ev-a1-r1"]

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_delete(self, tmp_path: Path, kind: str) -> None:
        store = _stores(tmp_path)[kind == "file"]
        _make(store, evidence_id="ev-del")
        store.delete("ev-del")
        with pytest.raises(EvidenceNotFoundError):
            get_evidence(store, "ev-del")
        store.delete("ev-del")  # idempotent


class TestManifestDecode:
    def test_rejects_non_dict(self) -> None:
        with pytest.raises(EvidenceCorruptError):
            evidence_from_dict([1, 2])

    def test_rejects_bad_fields(self, tmp_path: Path) -> None:
        good = json.loads(evidence_dumps(_make(InMemoryEvidenceStore(), evidence_id="ev-decode")))
        for patch in (
            {"schema_version": 2},
            {"evidence_id": "../x"},
            {"logical_type": "audio"},
            {"media_type": "bad"},
            {"size": -1},
            {"sha256": "zzz"},
            {"producer": {"run_id": "r"}},  # missing agent_id
            {"workspace": "not-a-dict"},
        ):
            bad = dict(good)
            bad.update(patch)
            with pytest.raises(EvidenceError):
                evidence_from_dict(bad)

    def test_roundtrip(self, tmp_path: Path) -> None:
        original = _make(
            InMemoryEvidenceStore(),
            content=b"roundtrip",
            logical_type="video",
            workspace=_workspace_record(),
            evidence_id="ev-round",
        )
        decoded = evidence_from_dict(json.loads(evidence_dumps(original)))
        assert evidence_dumps(decoded) == evidence_dumps(original)
