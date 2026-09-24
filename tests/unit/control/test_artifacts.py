"""Artifact package/store core: checksums, corrupt/missing, teardown read, leak."""

from __future__ import annotations

import json
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from control.artifacts import (
    ArtifactCorruptError,
    ArtifactError,
    ArtifactManifest,
    ArtifactNotFoundError,
    ArtifactSecretError,
    FileArtifactStore,
    InMemoryArtifactStore,
    ModalDictArtifactStore,
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


class _FakeModalDict:
    """``modal.Dict``-shaped fake: string keys, arbitrary pickled values.

    ``get``/``put``/``pop``/``keys``/``items`` mirror the live client; call
    counters let tests prove which RPC surface a store method used.
    """

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.gets: list[str] = []
        self.items_calls = 0
        self.keys_calls = 0

    def get(self, key, default=None):
        self.gets.append(key)
        return self.data.get(key, default)

    def put(self, key, value):
        self.data[key] = value

    def pop(self, key):
        return self.data.pop(key)

    def keys(self):
        self.keys_calls += 1
        return iter(list(self.data))

    def items(self):
        self.items_calls += 1
        return iter(list(self.data.items()))


def _modal_store() -> tuple[ModalDictArtifactStore, _FakeModalDict]:
    store = ModalDictArtifactStore("test-artifacts")
    fake = _FakeModalDict()
    store._dict = fake  # skip the lazy modal import; tests never touch Modal
    return store, fake


class TestModalDictStore:
    def test_put_get_read_roundtrip(self, tmp_path) -> None:
        """Direct create/GET/member paths are unchanged on the Dict store."""
        store, _ = _modal_store()
        ws = _workspace(tmp_path)
        manifest = _build(store, ws, payloads={"patch.diff": "p"})
        assert store.manifest("art-test1").artifact_id == manifest.artifact_id
        pkg = store.open("art-test1")
        assert pkg.file_bytes("src/app.py") == b"print('hi')\n"
        assert store.read("art-test1", "patch.diff") == b"p"
        assert store.read("art-test1", "manifest.json") == manifest_dumps(manifest)

    def test_list_never_pulls_member_bytes(self, tmp_path) -> None:
        """list/query enumerates keys then fetches manifests only — the Dict
        ``items()`` scan that streamed every member blob is gone.

        SOR-201: the filtered listing additionally stops enumerating keys
        once the per-agent index exists — only the one lazy build scans.
        """
        store, fake = _modal_store()
        ws = _workspace(tmp_path)
        _build(store, ws, payloads={"patch.diff": "p"})
        _build(store, ws, artifact_id="art-other", agent_id="agent-b")
        fake.gets.clear()
        fake.keys_calls = 0
        assert [m.artifact_id for m in store.list()] == ["art-other", "art-test1"]
        assert [m.artifact_id for m in store.list(agent_id="agent-b")] == ["art-other"]
        assert fake.items_calls == 0
        # One keys() for the unfiltered list + the one-time index build;
        # steady-state filtered queries enumerate nothing.
        assert fake.keys_calls >= 1
        built = fake.keys_calls
        fake.gets.clear()
        assert [m.artifact_id for m in store.list(agent_id="agent-b")] == ["art-other"]
        assert fake.keys_calls == built
        assert set(fake.gets) <= {"art-other/manifest", store._idx_key("agent-b")}
        assert not any(k.endswith("/member/patch.diff") for k in fake.gets)

    def test_list_skips_corrupt_and_member_named_manifest(self, tmp_path) -> None:
        """A payload literally named ``manifest`` produces a
        ``<id>/member/manifest`` key — it must not shadow the real manifest
        id or feed member bytes into manifest decode."""
        store, fake = _modal_store()
        ws = _workspace(tmp_path)
        _build(store, ws, payloads={"manifest": b"decoy", "patch.diff": "p"})
        _build(store, ws, artifact_id="art-junk")
        fake.data["art-junk/manifest"] = b"junk"
        fake.gets.clear()
        assert [m.artifact_id for m in store.list()] == ["art-test1"]
        assert "art-test1/member/manifest" not in fake.gets
        assert "art-test1/member/patch.diff" not in fake.gets

    def test_delete_removes_all_keys(self, tmp_path) -> None:
        store, fake = _modal_store()
        ws = _workspace(tmp_path)
        _build(store, ws, payloads={"patch.diff": "p"})
        store.delete("art-test1")
        assert not any(k.startswith("art-test1/") for k in fake.data)
        with pytest.raises(ArtifactNotFoundError):
            store.manifest("art-test1")
        store.delete("art-test1")  # idempotent


class TestAgentIndex:
    """SOR-201: durable producer-agent -> artifact index + keyset pages."""

    def _seed(self, store, ws: Path, specs: list[tuple[str, str, int]]) -> None:
        """``(artifact_id, agent_id, tick)`` rows; deterministic created_at."""
        for artifact_id, agent_id, tick in specs:
            base = datetime(2026, 9, 15, tzinfo=UTC) + timedelta(seconds=tick)
            _build(
                store,
                ws,
                artifact_id=artifact_id,
                agent_id=agent_id,
                clock=lambda b=base: b,
            )

    def _file_index_bytes(self, store: FileArtifactStore, agent_id: str) -> bytes:
        return store._index_path(agent_id).read_bytes()

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_index_maintained_on_put_and_delete(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        self._seed(store, ws, [("art-1", "agent-a", 0), ("art-2", "agent-a", 1)])
        assert [m.artifact_id for m in store.list(agent_id="agent-a")] == [
            "art-1",
            "art-2",
        ]
        store.delete("art-1")
        assert [m.artifact_id for m in store.list(agent_id="agent-a")] == ["art-2"]

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_reput_reindexes_producer_change(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        self._seed(store, ws, [("art-1", "agent-a", 0)])
        manifest = store.manifest("art-1")
        manifest.producer_agent_id = "agent-b"
        store.put(manifest, store.open("art-1").members)
        assert [m.artifact_id for m in store.list(agent_id="agent-a")] == []
        assert [m.artifact_id for m in store.list(agent_id="agent-b")] == ["art-1"]

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_lazy_rebuild_covers_pre_index_data(self, tmp_path, kind) -> None:
        """Artifacts written behind the index's back are still listed."""
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        self._seed(store, ws, [("art-1", "agent-a", 0)])
        if kind == "file":
            # Simulate a pre-SOR-201 store: index files removed.
            shutil.rmtree(tmp_path / "store" / ".index", ignore_errors=True)
            store._index_ready = False
        else:
            store._index.clear()
            store._index_ready = False
        assert [m.artifact_id for m in store.list(agent_id="agent-a")] == ["art-1"]
        if kind == "file":
            assert (tmp_path / "store" / ".index" / "_built").exists()

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_stale_index_row_pruned(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        self._seed(store, ws, [("art-1", "agent-a", 0), ("art-2", "agent-a", 1)])
        store.list(agent_id="agent-a")  # build/marker
        # Remove the package behind the index's back.
        if kind == "file":
            shutil.rmtree(tmp_path / "store" / "art-1")
        else:
            store._items.pop("art-1")
        assert [m.artifact_id for m in store.list(agent_id="agent-a")] == ["art-2"]
        # The stale row is repaired, not just hidden.
        if kind == "file":
            assert b"art-1" not in self._file_index_bytes(store, "agent-a")
        else:
            assert "art-1" not in {e.artifact_id for e in store._index["agent-a"]}

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_list_page_walks_keyset(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        self._seed(
            store,
            ws,
            [(f"art-{i}", "agent-a", i) for i in range(5)] + [("art-x", "agent-b", 99)],
        )
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page = store.list_page(agent_id="agent-a", cursor=cursor, limit=2)
            seen += [m.artifact_id for m in page.artifacts]
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == [f"art-{i}" for i in range(5)]
        assert cursor is None
        # Unpaginated filtered listing is unchanged.
        assert [m.artifact_id for m in store.list(agent_id="agent-a")] == [
            f"art-{i}" for i in range(5)
        ]

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_list_page_unfiltered(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        self._seed(store, ws, [("art-1", "agent-a", 0), ("art-2", "agent-b", 1)])
        page = store.list_page(limit=1)
        assert [m.artifact_id for m in page.artifacts] == ["art-1"]
        page = store.list_page(cursor=page.next_cursor)
        assert [m.artifact_id for m in page.artifacts] == ["art-2"]
        assert page.next_cursor is None

    @pytest.mark.parametrize("kind", ["memory", "file"])
    def test_bad_cursor_and_limit(self, tmp_path, kind) -> None:
        store = _stores(tmp_path)[kind == "file"]
        ws = _workspace(tmp_path)
        self._seed(store, ws, [("art-1", "agent-a", 0)])
        with pytest.raises(ArtifactError):
            store.list_page(agent_id="agent-a", cursor="!!!")
        with pytest.raises(ArtifactError):
            store.list_page(agent_id="agent-a", limit=0)

    def test_index_doc_carries_no_member_content(self, tmp_path) -> None:
        store = FileArtifactStore(tmp_path / "store")
        ws = _workspace(tmp_path)
        _build(store, ws, forbidden_values=[CANARY])
        doc = self._file_index_bytes(store, "agent-a")
        assert CANARY not in doc
        body = json.loads(doc)
        assert set(body) == {"v", "entries"}
        (row,) = body["entries"]
        assert set(row) == {"a", "t", "r"}

    def test_modal_index_written_and_cleaned(self, tmp_path) -> None:
        store, fake = _modal_store()
        ws = _workspace(tmp_path)
        self._seed(store, ws, [("art-1", "agent-a", 0)])
        idx_key = store._idx_key("agent-a")
        assert idx_key.startswith("index/agent/")
        assert json.loads(fake.data[idx_key])["entries"][0]["a"] == "art-1"
        store.delete("art-1")
        assert idx_key not in fake.data

    def test_modal_lazy_migration_from_historical_keys(self, tmp_path) -> None:
        """A Dict holding pre-index artifacts still answers filtered
        queries: the first one builds the index, later ones never rescan."""
        store, fake = _modal_store()
        for i in range(4):
            aid = f"art-old{i}"
            m = ArtifactManifest(
                artifact_id=aid,
                created_at=f"2026-09-15T00:00:0{i}+00:00",
                producer_agent_id="agent-old",
            )
            fake.data[f"{aid}/manifest"] = manifest_dumps(m)
            fake.data[f"{aid}/members"] = []
        assert [m.artifact_id for m in store.list(agent_id="agent-old")] == [
            f"art-old{i}" for i in range(4)
        ]
        assert fake.data[ModalDictArtifactStore._IDX_BUILT] == b"1"
        fake.keys_calls = 0
        for _ in range(3):
            store.list(agent_id="agent-old")
        assert fake.keys_calls == 0  # index hit, no global scan


class TestIndexScale:
    """SOR-201 scale gate: 10k+ artifacts, P95 < 1.5s, global-count
    independence proven by store-op counts, not just wall time."""

    SCALE_N = 12_000
    SCALE_AGENTS = 120
    PAGE = 50
    P95_LIMIT_S = 1.5
    SAMPLES = 25

    def _seed_fake_dict(self, fake: _FakeModalDict) -> None:
        for i in range(self.SCALE_N):
            aid = f"art-{i:05d}"
            manifest = ArtifactManifest(
                artifact_id=aid,
                created_at=f"2026-09-15T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+00:00",
                producer_agent_id=f"agent-{i % self.SCALE_AGENTS}",
            )
            fake.data[f"{aid}/manifest"] = manifest_dumps(manifest)
            fake.data[f"{aid}/members"] = []

    def _seed_file_store(self, root: Path) -> FileArtifactStore:
        store = FileArtifactStore(root)
        for i in range(self.SCALE_N):
            aid = f"art-{i:05d}"
            manifest = ArtifactManifest(
                artifact_id=aid,
                created_at=f"2026-09-15T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+00:00",
                producer_agent_id=f"agent-{i % self.SCALE_AGENTS}",
            )
            d = root / aid
            d.mkdir(parents=True)
            (d / "manifest.json").write_bytes(manifest_dumps(manifest))
        return store

    @staticmethod
    def _p95(samples: list[float]) -> float:
        ordered = sorted(samples)
        return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]

    def test_modal_filtered_page_is_global_independent(self, tmp_path) -> None:
        store, fake = _modal_store()
        self._seed_fake_dict(fake)
        assert store.rebuild_index() == self.SCALE_N
        agent = "agent-7"  # 100 artifacts
        latencies: list[float] = []
        for _ in range(self.SAMPLES):
            fake.gets.clear()
            fake.keys_calls = 0
            t0 = time.perf_counter()
            page = store.list_page(agent_id=agent, limit=self.PAGE)
            latencies.append(time.perf_counter() - t0)
            assert len(page.artifacts) == self.PAGE
            # The query surface is the index doc + the page's manifests —
            # O(page), no key enumeration, no other agent's rows.
            assert fake.keys_calls == 0
            assert len(fake.gets) <= self.PAGE + 1
        assert self._p95(latencies) < self.P95_LIMIT_S
        # Keyset walk covers the agent's rows exactly once.
        seen: list[str] = []
        cursor = None
        while True:
            page = store.list_page(agent_id=agent, cursor=cursor, limit=self.PAGE)
            seen += [m.artifact_id for m in page.artifacts]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert len(seen) == self.SCALE_N // self.SCALE_AGENTS
        assert len(set(seen)) == len(seen)

    def test_file_filtered_page_is_global_independent(self, tmp_path) -> None:
        store = self._seed_file_store(tmp_path / "store")
        assert store.rebuild_index() == self.SCALE_N
        agent = "agent-7"
        calls: list[str] = []
        orig_manifest = store.manifest
        store.manifest = lambda aid: (calls.append(aid), orig_manifest(aid))[1]
        try:
            latencies: list[float] = []
            for _ in range(self.SAMPLES):
                calls.clear()
                t0 = time.perf_counter()
                page = store.list_page(agent_id=agent, limit=self.PAGE)
                latencies.append(time.perf_counter() - t0)
                assert len(page.artifacts) == self.PAGE
                # Only the page's manifests are read from disk.
                assert len(calls) == self.PAGE
            assert self._p95(latencies) < self.P95_LIMIT_S
        finally:
            del store.manifest

    def test_memory_filtered_page(self, tmp_path) -> None:
        store = InMemoryArtifactStore()
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write(ws, "a.txt", "x\n")
        for i in range(self.SCALE_N):
            base = datetime(2026, 9, 15, tzinfo=UTC) + timedelta(seconds=i)
            _build(
                store,
                ws,
                artifact_id=f"art-{i:05d}",
                agent_id=f"agent-{i % self.SCALE_AGENTS}",
                clock=lambda: base,
            )
        latencies: list[float] = []
        for _ in range(self.SAMPLES):
            t0 = time.perf_counter()
            page = store.list_page(agent_id="agent-7", limit=self.PAGE)
            latencies.append(time.perf_counter() - t0)
            assert len(page.artifacts) == self.PAGE
        assert self._p95(latencies) < self.P95_LIMIT_S


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
