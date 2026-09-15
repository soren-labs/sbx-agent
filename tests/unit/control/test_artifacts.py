"""Artifact package/store core: checksums, corrupt/missing, teardown read, leak."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from control.artifacts import (
    ArtifactCorruptError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactSecretError,
    FileArtifactStore,
    InMemoryArtifactStore,
    TestResult,
    WorkspacePolicy,
    build_artifact,
    manifest_dumps,
    manifest_from_dict,
    sha256_hex,
)

CANARY = b"CANARY-SBX-TOKEN-0123456789abcdef"


def _clock(start: datetime | None = None):
    base = start or datetime(2026, 9, 15, tzinfo=UTC)
    ticks = iter(range(10_000))
    return lambda: base + timedelta(seconds=next(ticks))


def _write(root: Path, relpath: str, data: bytes | str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    _write(ws, "src/app.py", "print('hi')\n")
    _write(ws, "src/util/helper.py", "X = 1\n")
    _write(ws, "README.md", "# demo\n")
    # Denied: provider HOME / auth stores / key material / runner bookkeeping.
    _write(ws, "home/.codex/auth.json", CANARY)
    _write(ws, ".env", CANARY)
    _write(ws, ".git/config", b"[core]")
    _write(ws, "src/keys/id_rsa", CANARY)
    _write(ws, ".config/devin/credentials.json", CANARY)
    _write(ws, "session.json", b"{}")
    _write(ws, "events.jsonl", CANARY)
    return ws


def _stores(tmp_path: Path):
    return [InMemoryArtifactStore(), FileArtifactStore(tmp_path / "store")]


def _build(store, ws: Path, **kwargs):
    defaults = {
        "store": store,
        "agent_id": "agent-a",
        "run_id": "run-1",
        "base_sha": "b" * 40,
        "head_sha": "h" * 40,
        "artifact_id": "art-test1",
        "clock": _clock(),
    }
    defaults.update(kwargs)
    return build_artifact(ws, **defaults)


class TestBuild:
    def test_collects_allowed_files_sorted_with_checksums(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        manifest = _build(InMemoryArtifactStore(), ws)
        paths = [f.path for f in manifest.files]
        assert paths == sorted(paths)
        assert set(paths) == {"README.md", "src/app.py", "src/util/helper.py"}
        for entry in manifest.files:
            assert entry.sha256 == sha256_hex((ws / entry.path).read_bytes())
            assert entry.size == (ws / entry.path).stat().st_size

    def test_manifest_fields(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        store = InMemoryArtifactStore()
        manifest = _build(
            store,
            ws,
            tests=[TestResult("pytest -q", 0), ("make lint", 1)],
        )
        assert manifest.format == "patch"
        assert manifest.base_sha == "b" * 40
        assert manifest.head_sha == "h" * 40
        assert manifest.producer_agent_id == "agent-a"
        assert manifest.producer_run_id == "run-1"
        assert manifest.created_at == "2026-09-15T00:00:00+00:00"
        assert manifest.tests == [
            TestResult("pytest -q", 0),
            TestResult("make lint", 1),
        ]
        raw = json.loads(store.read("art-test1", "manifest.json"))
        assert raw["producer"] == {"agent_id": "agent-a", "run_id": "run-1"}

    def test_secret_paths_never_collected(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        manifest = _build(InMemoryArtifactStore(), ws)
        paths = {f.path for f in manifest.files}
        assert not any(p.startswith(("home/", ".git/", ".config/")) for p in paths)
        assert ".env" not in paths and "session.json" not in paths
        assert "src/keys/id_rsa" not in paths
        assert "events.jsonl" not in paths

    def test_include_allowlist_narrows_collection(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        manifest = _build(InMemoryArtifactStore(), ws, include=["src/*.py"])
        assert {f.path for f in manifest.files} == {"src/app.py", "src/util/helper.py"}

    def test_symlink_never_followed(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        (ws / "src" / "leak.py").symlink_to(ws / "home" / ".codex" / "auth.json")
        manifest = _build(InMemoryArtifactStore(), ws)
        assert "src/leak.py" not in {f.path for f in manifest.files}
        assert any("symlink" in w for w in manifest.warnings)

    def test_forbidden_value_fails_closed(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        _write(ws, "src/token.txt", CANARY)
        store = InMemoryArtifactStore()
        with pytest.raises(ArtifactSecretError) as exc:
            _build(store, ws, forbidden_values=[CANARY])
        assert exc.value.paths == ["src/token.txt"]
        with pytest.raises(ArtifactNotFoundError):
            store.manifest("art-test1")

    def test_forbidden_value_in_payload_fails(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        with pytest.raises(ArtifactSecretError):
            _build(
                InMemoryArtifactStore(),
                ws,
                payloads={"patch.diff": CANARY},
                forbidden_values=[CANARY],
            )

    def test_payloads_recorded_with_checksum(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        store = InMemoryArtifactStore()
        manifest = _build(
            store,
            ws,
            payloads={"patch.diff": b"diff --git a/x b/x\n", "tests/junit.xml": "<x/>"},
        )
        assert manifest.payloads["patch.diff"] == sha256_hex(b"diff --git a/x b/x\n")
        assert store.read("art-test1", "tests/junit.xml") == b"<x/>"

    @pytest.mark.parametrize("bad", ["files/x", "manifest.json", "../x", "/abs", "a\\b", "a:b", ""])
    def test_payload_name_rejected(self, tmp_path, bad) -> None:
        ws = _workspace(tmp_path)
        with pytest.raises(ArtifactError):
            _build(InMemoryArtifactStore(), ws, payloads={bad: b"x"})

    def test_deterministic_manifest_bytes(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        m1 = _build(InMemoryArtifactStore(), ws)
        m2 = _build(InMemoryArtifactStore(), ws)
        assert manifest_dumps(m1) == manifest_dumps(m2)

    def test_workspace_must_be_dir(self, tmp_path) -> None:
        with pytest.raises(ArtifactError):
            _build(InMemoryArtifactStore(), tmp_path / "nope")


class TestStores:
    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_roundtrip_open_verifies(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        _build(store, ws, payloads={"patch.diff": "p"})
        pkg = store.open("art-test1")
        assert pkg.manifest.artifact_id == "art-test1"
        assert pkg.file_bytes("src/app.py") == b"print('hi')\n"
        assert pkg.member("patch.diff") == b"p"
        assert pkg.member("manifest.json") == manifest_dumps(pkg.manifest)

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_missing(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        with pytest.raises(ArtifactNotFoundError):
            store.manifest("art-nope")
        with pytest.raises(ArtifactNotFoundError):
            store.open("art-nope")
        ws = _workspace(tmp_path)
        _build(store, ws)
        with pytest.raises(ArtifactNotFoundError):
            store.read("art-test1", "files/nope.py")
        with pytest.raises(ArtifactNotFoundError):
            store.read("art-test1", "patch.diff")

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_corrupt_manifest(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        _build(store, ws)
        if kind == "file":
            path = tmp_path / "store" / "art-test1" / "manifest.json"
            path.write_bytes(b"{not json")
        else:
            raw, members = store._items["art-test1"]
            store._items["art-test1"] = (b"{not json", members)
        with pytest.raises(ArtifactCorruptError):
            store.manifest("art-test1")
        with pytest.raises(ArtifactCorruptError):
            store.open("art-test1")

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_corrupt_member_checksum(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        _build(store, ws)
        if kind == "file":
            member = tmp_path / "store" / "art-test1" / "members" / "files" / "src" / "app.py"
            member.write_bytes(b"tampered\n")
        else:
            raw, members = store._items["art-test1"]
            members["files/src/app.py"] = b"tampered\n"
        with pytest.raises(ArtifactCorruptError):
            store.open("art-test1")
        with pytest.raises(ArtifactCorruptError):
            store.read("art-test1", "files/src/app.py")

    def test_missing_member_file(self, tmp_path) -> None:
        store = FileArtifactStore(tmp_path / "store")
        ws = _workspace(tmp_path)
        _build(store, ws)
        member = tmp_path / "store" / "art-test1" / "members" / "files" / "README.md"
        member.unlink()
        with pytest.raises(ArtifactCorruptError):
            store.open("art-test1")
        with pytest.raises(ArtifactCorruptError):
            store.read("art-test1", "files/README.md")

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_teardown_independent_read(self, tmp_path, kind) -> None:
        """Artifact survives removal of the source workspace (sandbox teardown)."""
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        manifest = _build(store, ws)
        shutil.rmtree(ws)
        pkg = store.open("art-test1")
        assert pkg.file_bytes("src/app.py") == b"print('hi')\n"
        assert store.read("art-test1", "manifest.json") == manifest_dumps(manifest)

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_credential_never_in_package(self, tmp_path, kind) -> None:
        """No member or manifest byte contains credential canary material."""
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        _build(store, ws)
        pkg = store.open("art-test1")
        blob = manifest_dumps(pkg.manifest) + b"".join(pkg.members.values())
        assert CANARY not in blob

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_write_to_and_verify_dir(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        _build(store, ws)
        pkg = store.open("art-test1")
        dest = tmp_path / "applied"
        written = pkg.write_to(dest)
        assert set(written) == {"README.md", "src/app.py", "src/util/helper.py"}
        assert pkg.verify_dir(dest) == []
        (dest / "src" / "app.py").write_bytes(b"drifted\n")
        (dest / "README.md").unlink()
        assert pkg.verify_dir(dest) == ["README.md", "src/app.py"]

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_put_rejects_mismatched_members(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        manifest = _build(InMemoryArtifactStore(), ws)
        members = {f"files/{f.path}": (ws / f.path).read_bytes() for f in manifest.files}
        members["files/src/app.py"] = b"wrong"
        with pytest.raises(ArtifactCorruptError):
            store.put(manifest, members)
        members["files/src/app.py"] = (ws / "src" / "app.py").read_bytes()
        members["extra.bin"] = b"x"
        with pytest.raises(ArtifactError):
            store.put(manifest, members)

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_list_filters_by_agent_and_skips_corrupt(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        _build(store, ws)
        _build(store, ws, artifact_id="art-other", agent_id="agent-b")
        # Same created_at on both -> deterministic artifact_id ordering.
        assert [m.artifact_id for m in store.list()] == ["art-other", "art-test1"]
        assert [m.artifact_id for m in store.list(agent_id="agent-b")] == ["art-other"]
        if kind == "file":
            (tmp_path / "store" / "art-other" / "manifest.json").write_bytes(b"junk")
        else:
            raw, members = store._items["art-other"]
            store._items["art-other"] = (b"junk", members)
        assert [m.artifact_id for m in store.list()] == ["art-test1"]

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_delete(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        _build(store, ws)
        store.delete("art-test1")
        with pytest.raises(ArtifactNotFoundError):
            store.manifest("art-test1")
        store.delete("art-test1")  # idempotent

    @pytest.mark.parametrize("bad_id", ["..", "a/b", ".hidden", "x y"])
    def test_artifact_id_validation(self, tmp_path, bad_id) -> None:
        ws = _workspace(tmp_path)
        with pytest.raises(ArtifactError):
            _build(InMemoryArtifactStore(), ws, artifact_id=bad_id)


class TestManifestDecode:
    def test_rejects_non_dict(self) -> None:
        with pytest.raises(ArtifactCorruptError):
            manifest_from_dict([1, 2])

    def test_rejects_bad_fields(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        good = json.loads(manifest_dumps(_build(InMemoryArtifactStore(), ws)))
        for patch in (
            {"schema_version": 2},
            {"format": "bundle"},
            {"artifact_id": ".."},
            {"files": [{"path": "a", "sha256": "zzz", "size": 1}]},
            {"files": [{"path": "a/../b", "sha256": "0" * 64, "size": 1}]},
            {"tests": [{"command": "x", "exit_code": "zero"}]},
            {"payloads": {"files/x": "0" * 64}},
            {"producer": {"run_id": "r"}},  # missing agent_id
        ):
            bad = dict(good)
            bad.update(patch)
            with pytest.raises(ArtifactCorruptError):
                manifest_from_dict(bad)

    def test_roundtrip(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        manifest = _build(
            InMemoryArtifactStore(), ws, tests=[{"command": "pytest", "exit_code": 0}]
        )
        decoded = manifest_from_dict(json.loads(manifest_dumps(manifest)))
        assert manifest_dumps(decoded) == manifest_dumps(manifest)


class TestPolicy:
    def test_denied_paths(self) -> None:
        policy = WorkspacePolicy()
        for denied in (
            "home/x",
            ".git/config",
            "a/.git/HEAD",
            ".env",
            "sub/.env.local",
            ".codex/auth.json",
            "deep/.ssh/id_rsa",
            "x.pem",
            "session.json",
            "turns/1.json",
        ):
            assert policy.denial(denied) is not None, denied
        # Strict boundary: even commonly-safe templates stay out (`.env.*`).
        assert policy.denial(".env.example") is not None
        for allowed in ("src/app.py", "src/home/x.py", "docs/env.md"):
            assert policy.is_allowed(allowed), allowed

    def test_include_glob(self) -> None:
        policy = WorkspacePolicy(include=("src/*.py",))
        assert policy.is_allowed("src/app.py")
        assert not policy.is_allowed("README.md")
        assert not policy.is_allowed("src/.env")  # deny beats include
