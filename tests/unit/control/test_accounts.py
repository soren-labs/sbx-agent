"""SOR-63/D1: persistent AccountRegistry over pluggable AccountStore backends."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest
from control.accounts import (
    AccountStore,
    FileAccountStore,
    InMemoryAccountStore,
    PersistentAccountRegistry,
    account_from_dict,
    account_to_dict,
    cooldown_expired,
    parse_iso,
    select_store,
)
from control.accounts import main as accounts_main
from control.ports import Account, AccountRegistry


def _account(id: str, provider: str = "antigravity", **kw: Any) -> Account:
    return Account(id=id, provider=provider, label=id, **kw)


def _registry(store: AccountStore, **kw: Any) -> PersistentAccountRegistry:
    return PersistentAccountRegistry(store, **kw)


@pytest.fixture(params=["memory", "file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> AccountStore:
    if request.param == "memory":
        return InMemoryAccountStore()
    return FileAccountStore(tmp_path / "accounts")


class TestSerialization:
    def test_roundtrip(self) -> None:
        acct = _account(
            "agy-1",
            models=("gemini-3.8-flash-low",),
            max_concurrent=2,
            secret_name="sbx-acct-agy-1",
            created_at="2026-09-15T00:00:00+00:00",
            last_used_at="2026-09-15T01:00:00+00:00",
            cooldown_until="2026-09-15T02:00:00+00:00",
            last_error="rate_limited",
        )
        assert account_from_dict(account_to_dict(acct)) == acct

    def test_missing_fields_rejected(self) -> None:
        with pytest.raises(ValueError):
            account_from_dict({})
        with pytest.raises(ValueError):
            account_from_dict({"id": "a1"})
        with pytest.raises(ValueError):
            account_from_dict({"id": "a1", "provider": "grok", "status": "weird"})

    def test_parse_iso(self) -> None:
        assert parse_iso(None) is None
        assert parse_iso("garbage") is None
        assert parse_iso("2026-09-15T00:00:00Z") is not None
        naive = parse_iso("2026-09-15T00:00:00")
        assert naive is not None and naive.tzinfo is not None

    def test_cooldown_expired(self) -> None:
        now = parse_iso("2026-09-15T12:00:00Z")
        assert now is not None
        cooling = _account("a1", status="cooling", cooldown_until="2026-09-15T11:00:00Z")
        assert cooldown_expired(cooling, now)
        future = _account("a1", status="cooling", cooldown_until="2026-09-15T13:00:00Z")
        assert not cooldown_expired(future, now)
        assert not cooldown_expired(_account("a1"), now)
        # cooling without a timestamp never auto-expires (operator intent).
        assert not cooldown_expired(_account("a1", status="cooling"), now)


class TestRegistryOverStore:
    def test_protocol_conformance(self, store: AccountStore) -> None:
        assert isinstance(_registry(store), AccountRegistry)
        assert isinstance(store, AccountStore)

    def test_crud_and_status(self, store: AccountStore) -> None:
        reg = _registry(store)
        reg.put(_account("a1"))
        reg.put(_account("g1", provider="grok"))
        assert reg.get("a1") is not None
        assert reg.get("a1").status == "active"  # type: ignore[union-attr]
        assert [a.id for a in reg.list()] == ["a1", "g1"]
        assert [a.id for a in reg.list("grok")] == ["g1"]

        updated = reg.mark_status("a1", "cooling", cooldown_until="2030-01-01T00:00:00Z")
        assert updated.status == "cooling"
        assert reg.get("a1").cooldown_until == "2030-01-01T00:00:00Z"  # type: ignore[union-attr]

        reg.touch("a1", "2026-09-15T05:00:00Z")
        assert reg.get("a1").last_used_at == "2026-09-15T05:00:00Z"  # type: ignore[union-attr]

        reg.remove("a1")
        assert reg.get("a1") is None
        with pytest.raises(KeyError):
            reg.mark_status("a1", "active")
        with pytest.raises(KeyError):
            reg.touch("a1", "2026-09-15T05:00:00Z")

    def test_put_validation(self, store: AccountStore) -> None:
        reg = _registry(store)
        with pytest.raises(ValueError):
            reg.put(_account("bad", status="weird"))
        with pytest.raises(ValueError):
            reg.mark_status("a1", "weird")

    def test_credential_blob_roundtrip(self, store: AccountStore) -> None:
        reg = _registry(store)
        reg.put(_account("a1"))
        blob = {"provider": "antigravity", "files": {".gemini/creds.json": "REDACTED"}}
        reg.put_credential_blob("a1", blob)
        assert reg.get_credential_blob("a1") == blob
        reg.remove("a1")
        assert reg.get_credential_blob("a1") is None

    def test_credential_blob_validation(self, store: AccountStore) -> None:
        reg = _registry(store)
        reg.put(_account("a1"))
        with pytest.raises(ValueError):
            reg.put_credential_blob("a1", {"provider": "antigravity"})  # no files
        with pytest.raises(ValueError):
            reg.put_credential_blob(
                "a1", {"provider": "grok", "files": {"x": "y"}}
            )  # provider mismatch

    def test_corrupt_record_surfaces_as_disabled(self, store: AccountStore) -> None:
        store.put_record("broken", {"id": "broken", "provider": "grok", "status": "bogus"})
        reg = _registry(store)
        acct = reg.get("broken")
        assert acct is not None
        assert acct.status == "disabled"
        assert acct.last_error == "corrupt_record"
        # A corrupt record never passes the provider filter as usable.
        assert all(a.status == "disabled" for a in reg.list("grok"))

    def test_running_count_sources(self, store: AccountStore) -> None:
        reg = _registry(store)
        reg.put(_account("a1"))
        assert reg.running_count("a1") == 0
        reg.set_running("a1", 2)
        assert reg.running_count("a1") == 2
        reg.adjust_running("a1", -1)
        assert reg.running_count("a1") == 1
        reg.bind_running(lambda _id: 7)
        assert reg.running_count("a1") == 7


class TestFileAccountStore:
    def test_persists_across_instances(self, tmp_path: Path) -> None:
        root = tmp_path / "accounts"
        reg = _registry(FileAccountStore(root))
        reg.put(_account("a1"))
        reg.put_credential_blob("a1", {"provider": "antigravity", "files": {"creds": "REDACTED"}})

        reopened = _registry(FileAccountStore(root))
        acct = reopened.get("a1")
        assert acct is not None and acct.provider == "antigravity"
        assert reopened.get_credential_blob("a1") == {
            "provider": "antigravity",
            "files": {"creds": "REDACTED"},
        }

    def test_credential_file_mode_600(self, tmp_path: Path) -> None:
        store = FileAccountStore(tmp_path / "accounts")
        store.put_blob("a1", {"provider": "antigravity", "files": {"creds": "REDACTED"}})
        path = tmp_path / "accounts" / "credentials" / "a1.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_corrupt_file_yields_disabled_record(self, tmp_path: Path) -> None:
        root = tmp_path / "accounts"
        (root / "accounts").mkdir(parents=True)
        (root / "accounts" / "bad1.json").write_text("{not json", encoding="utf-8")
        reg = _registry(FileAccountStore(root))
        # get() treats an unreadable record as absent; list() surfaces it disabled.
        assert reg.get("bad1") is None
        listed = reg.list()
        assert [a.id for a in listed] == ["bad1"]
        assert listed[0].status == "disabled"


class TestSelectStore:
    def test_local_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SBX_BACKEND", "local")
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        store = select_store()
        assert isinstance(store, FileAccountStore)
        assert store.root == tmp_path / "sbx-browser" / "accounts"

    def test_store_dir_override(self, tmp_path: Path) -> None:
        store = select_store(store_dir=tmp_path / "custom")
        assert isinstance(store, FileAccountStore)
        assert store.root == tmp_path / "custom"


class TestCli:
    def test_import_list_disable_remove(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        creds = tmp_path / "creds.json"
        creds.write_text('{"token": "REDACTED"}', encoding="utf-8")
        store_dir = tmp_path / "store"

        rc = accounts_main(
            [
                "--store-dir",
                str(store_dir),
                "import",
                "--provider",
                "grok",
                "--label",
                "grok-01",
                "--from",
                str(creds),
                "--slots",
                "2",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "imported" in out and "REDACTED" not in out

        rc = accounts_main(["--store-dir", str(store_dir), "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "grok-01" in out and "REDACTED" not in out

        reg = _registry(FileAccountStore(store_dir))
        (account,) = reg.list("grok")
        assert account.max_concurrent == 2
        # Single-file --from maps onto the adapter's declared relpath.
        assert reg.get_credential_blob(account.id) == {
            "provider": "grok",
            "files": {".grok/auth.json": '{"token": "REDACTED"}'},
        }

        rc = accounts_main(["--store-dir", str(store_dir), "disable", account.id])
        assert rc == 0
        assert reg.get(account.id).status == "disabled"  # type: ignore[union-attr]
        rc = accounts_main(["--store-dir", str(store_dir), "enable", account.id])
        assert rc == 0
        assert reg.get(account.id).status == "active"  # type: ignore[union-attr]

        rc = accounts_main(["--store-dir", str(store_dir), "remove", account.id])
        assert rc == 0
        assert reg.get(account.id) is None
        assert reg.get_credential_blob(account.id) is None

    def test_import_directory_uses_adapter_files(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        codex_dir = tmp_path / "dotcodex"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text("{}", encoding="utf-8")
        store_dir = tmp_path / "store"
        rc = accounts_main(
            [
                "--store-dir",
                str(store_dir),
                "import",
                "--provider",
                "codex",
                "--from",
                str(codex_dir),
            ]
        )
        assert rc == 0
        capsys.readouterr()
        reg = _registry(FileAccountStore(store_dir))
        (account,) = reg.list("codex")
        assert reg.get_credential_blob(account.id) == {
            "provider": "codex",
            "files": {".codex/auth.json": "{}"},
        }
