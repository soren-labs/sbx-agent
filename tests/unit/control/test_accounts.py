"""SOR-63/D1: persistent AccountRegistry over pluggable AccountStore backends."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from control.accounts import (
    AccountStore,
    FileAccountStore,
    InMemoryAccountStore,
    ModalDictAccountStore,
    PersistentAccountRegistry,
    account_from_dict,
    account_to_dict,
    cooldown_expired,
    is_valid_account_id,
    parse_iso,
    select_store,
    validate_account_id,
)
from control.accounts import main as accounts_main
from control.ports import Account, AccountRegistry
from control.scheduler import AccountScheduler, ScheduleRefused


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


class TestAccountIdSafety:
    """SOR-105: every account_id reaching a store/registry path is validated
    fail-closed — traversal, absolute, slash/backslash, empty and overlong
    ids are refused before any filesystem/Secret/Dict access. Legal ids
    (``[A-Za-z0-9._-]``, alnum first, <=128 chars) are unaffected."""

    BAD_IDS = (
        "../x",
        "../escape",
        "..",
        "../../victim",
        "/abs/path",
        "a/b",
        "a\\b",
        "",
        "   ",
        ".hidden",
        "-x",
        "a b",
        "a\x00b",
        "x" * 129,
    )
    GOOD_IDS = ("a", "acct-grok-1", "A9._-x", "9", "x" * 128, "a.b_c-d")

    def test_validate_accepts_legal_ids(self) -> None:
        for good in self.GOOD_IDS:
            assert validate_account_id(good) == good
            assert is_valid_account_id(good)

    def test_validate_rejects_unsafe_and_nonstring_ids(self) -> None:
        for bad in (*self.BAD_IDS, None, 7, b"a1"):
            with pytest.raises(ValueError):
                validate_account_id(bad)
            assert not is_valid_account_id(bad)

    @pytest.mark.parametrize("bad", BAD_IDS)
    def test_file_store_rejects_before_any_io(self, tmp_path: Path, bad: str) -> None:
        store = FileAccountStore(tmp_path / "store")
        calls = (
            lambda: store.get_record(bad),
            lambda: store.put_record(bad, {"id": bad, "provider": "grok"}),
            lambda: store.delete_record(bad),
            lambda: store.get_blob(bad),
            lambda: store.put_blob(bad, {"provider": "grok", "files": {"c": "REDACTED"}}),
            lambda: store.delete_blob(bad),
        )
        for call in calls:
            with pytest.raises(ValueError):
                call()
        # Nothing was created — inside or outside the store root.
        assert not (tmp_path / "store").exists()

    def test_file_store_traversal_cannot_read_or_delete_outside_root(self, tmp_path: Path) -> None:
        """Pre-fix reproducer: ``accounts/<id>.json`` / ``credentials/<id>.json``
        with ``../victim`` resolved to ``<root>/victim.json`` — reads and
        deletes escaped the store. Now refused before any path is formed."""
        root = tmp_path / "store"
        (root / "accounts").mkdir(parents=True)
        (root / "credentials").mkdir(parents=True)
        victim = root / "victim.json"
        victim.write_text(
            json.dumps({"provider": "grok", "files": {"c": "REDACTED"}}), encoding="utf-8"
        )
        store = FileAccountStore(root)
        with pytest.raises(ValueError):
            store.get_blob("../victim")
        with pytest.raises(ValueError):
            store.get_record("../victim")
        with pytest.raises(ValueError):
            store.delete_blob("../victim")
        with pytest.raises(ValueError):
            store.delete_record("../victim")
        assert victim.is_file()

    def test_file_store_traversal_cannot_write_outside_root(self, tmp_path: Path) -> None:
        root = tmp_path / "store"
        store = FileAccountStore(root)
        with pytest.raises(ValueError):
            store.put_blob("../../escape", {"provider": "grok", "files": {"c": "x"}})
        with pytest.raises(ValueError):
            store.put_record("../../escape", {"id": "../../escape", "provider": "grok"})
        assert not (tmp_path / "escape.json").exists()
        assert not root.exists()

    @pytest.mark.parametrize("bad", BAD_IDS)
    def test_registry_rejects_unsafe_ids(self, store: AccountStore, bad: str) -> None:
        reg = _registry(store)
        with pytest.raises(ValueError):
            reg.get(bad)
        with pytest.raises(ValueError):
            reg.put(_account(bad))
        with pytest.raises(ValueError):
            reg.mark_status(bad, "disabled")
        with pytest.raises(ValueError):
            reg.touch(bad, "2026-09-16T00:00:00Z")
        with pytest.raises(ValueError):
            reg.remove(bad)
        with pytest.raises(ValueError):
            reg.get_credential_blob(bad)
        with pytest.raises(ValueError):
            reg.put_credential_blob(bad, {"provider": "antigravity", "files": {"c": "R"}})
        with pytest.raises(ValueError):
            reg.set_running(bad, 1)
        with pytest.raises(ValueError):
            reg.adjust_running(bad, 1)
        with pytest.raises(ValueError):
            reg.running_count(bad)

    def test_rejection_message_carries_no_credential(self, store: AccountStore) -> None:
        reg = _registry(store)
        with pytest.raises(ValueError) as exc:
            reg.put_credential_blob(
                "../x", {"provider": "antigravity", "files": {"c": "S3CR3T-VALUE"}}
            )
        assert "S3CR3T-VALUE" not in str(exc.value)

    def test_modal_store_validates_before_dict_access(self) -> None:
        """A non-conformant id raises ValueError before ``_d()`` — no modal
        import, no Dict key construction. The exploding stub proves order:
        a legal id reaches the Dict and trips it."""

        class _ExplodingDict:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError("modal.Dict was touched")

        store = ModalDictAccountStore()
        store._dict = _ExplodingDict()
        for bad in ("../x", "/abs", "a\\b", "", "x" * 129):
            for call in (
                lambda: store.get_record(bad),
                lambda: store.put_record(bad, {}),
                lambda: store.delete_record(bad),
                lambda: store.get_blob(bad),
                lambda: store.put_blob(bad, {}),
                lambda: store.delete_blob(bad),
            ):
                with pytest.raises(ValueError):
                    call()
        with pytest.raises(AssertionError, match="modal.Dict was touched"):
            store.get_record("legal-1")

    def test_scheduler_named_pick_refuses_unsafe_id(self) -> None:
        sched = AccountScheduler(_registry(InMemoryAccountStore()))
        for bad in ("../x", "/abs", "a\\b", "", "x" * 129):
            decision = sched.decide(provider="grok", account=bad)
            assert decision.account is None
            assert decision.error == "account_unavailable"
            with pytest.raises(ScheduleRefused) as exc:
                sched.acquire(provider="grok", account=bad)
            assert exc.value.error == "account_unavailable"
            # report_failure's contract is KeyError for an unknown account;
            # a non-conformant id can never name one.
            with pytest.raises(KeyError):
                sched.report_failure(bad, "rate_limited")
            with pytest.raises(KeyError):
                sched.report_run_error(bad, None)


class TestCliAccountIdSafety:
    """SOR-105: the legacy ``python -m control.accounts`` CLI rejects unsafe
    ids with a clean error — no traceback, no store/filesystem access, no
    credential material on either stream."""

    def _creds(self, tmp_path: Path) -> Path:
        creds = tmp_path / "creds.json"
        creds.write_text('{"token": "REDACTED"}', encoding="utf-8")
        return creds

    def test_remove_traversal_cannot_delete_outside_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pre-fix, ``remove ../../victim`` unlinked ``<tmp>/victim.json``
        via ``accounts/../../victim.json``."""
        store_dir = tmp_path / "store"
        victim = tmp_path / "victim.json"
        victim.write_text("{}", encoding="utf-8")
        rc = accounts_main(["--store-dir", str(store_dir), "remove", "../../victim"])
        assert rc == 2
        captured = capsys.readouterr()
        assert "invalid account id" in captured.err
        assert "Traceback" not in captured.err
        assert victim.is_file()

    @pytest.mark.parametrize("cmd", ["disable", "enable", "remove"])
    def test_status_and_remove_commands_reject_unsafe_ids(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], cmd: str
    ) -> None:
        store_dir = tmp_path / "store"
        for bad in ("../x", "/abs", "a\\b", "", "x" * 129):
            rc = accounts_main(["--store-dir", str(store_dir), cmd, bad])
            assert rc == 2
            captured = capsys.readouterr()
            assert "invalid account id" in captured.err
            assert "Traceback" not in captured.err
        assert not store_dir.exists()

    def test_import_rejects_unsafe_account_id_before_touching_anything(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_dir = tmp_path / "store"
        creds = self._creds(tmp_path)
        for bad in ("../x", "/abs", "a\\b", "x" * 129):
            rc = accounts_main(
                [
                    "--store-dir",
                    str(store_dir),
                    "import",
                    "--provider",
                    "grok",
                    "--from",
                    str(creds),
                    "--account-id",
                    bad,
                ]
            )
            assert rc == 2
            captured = capsys.readouterr()
            assert "invalid account id" in captured.err
            assert "REDACTED" not in captured.out + captured.err
        # The refused imports created nothing — inside or outside the store.
        assert not store_dir.exists()
        assert not (tmp_path / "x.json").exists()

    def test_cli_legal_ids_still_work(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_dir = tmp_path / "store"
        creds = self._creds(tmp_path)
        for good in TestAccountIdSafety.GOOD_IDS:
            rc = accounts_main(
                [
                    "--store-dir",
                    str(store_dir),
                    "import",
                    "--provider",
                    "grok",
                    "--from",
                    str(creds),
                    "--account-id",
                    good,
                ]
            )
            assert rc == 0, capsys.readouterr().err
            assert (store_dir / "accounts" / f"{good}.json").is_file()

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


class TestStoredRecordIdIntegrity:
    """SOR-105 review: the store key is authoritative. A stored record whose
    body id is unsafe — or simply differs from the key it sits under —
    decodes as ``corrupt_account`` so no smuggled id can reach a lane that
    re-uses ``account.id`` (``running_count``, ``mark_status``, ``touch``,
    scheduler picks, Secret-name construction)."""

    def test_unsafe_body_id_decodes_corrupt(self, store: AccountStore) -> None:
        store.put_record("good-1", {"id": "../victim", "provider": "grok", "status": "active"})
        reg = _registry(store)
        acct = reg.get("good-1")
        assert acct is not None
        assert acct.id == "good-1"
        assert acct.status == "disabled"
        assert acct.last_error == "corrupt_record"
        (listed,) = reg.list()
        assert (listed.id, listed.status) == ("good-1", "disabled")

    def test_foreign_body_id_decodes_corrupt(self, store: AccountStore) -> None:
        store.put_record("good-1", {"id": "other-1", "provider": "grok"})
        acct = _registry(store).get("good-1")
        assert acct is not None
        assert acct.id == "good-1"
        assert acct.status == "disabled"

    def test_unsafe_store_key_lists_disabled_and_refuses_ops(self, tmp_path: Path) -> None:
        """A stray ``accounts/<unsafe>.json`` (other tools, older versions)
        stays visible-but-disabled; per-id ops still refuse its key."""
        root = tmp_path / "store"
        (root / "accounts").mkdir(parents=True)
        (root / "accounts" / ".hidden.json").write_text(
            json.dumps({"id": ".hidden", "provider": "grok", "status": "active"}),
            encoding="utf-8",
        )
        reg = _registry(FileAccountStore(root))
        (listed,) = reg.list()
        assert listed.status == "disabled"
        assert listed.last_error == "corrupt_record"
        with pytest.raises(ValueError):
            reg.get(".hidden")
        with pytest.raises(ValueError):
            reg.mark_status(".hidden", "active")
        with pytest.raises(ValueError):
            reg.remove(".hidden")
        # The planted file is untouched — remove never formed a path from it.
        assert (root / "accounts" / ".hidden.json").is_file()

    def test_smuggled_id_never_schedules(self, store: AccountStore) -> None:
        store.put_record("good-1", {"id": "../victim", "provider": "grok", "status": "active"})
        sched = AccountScheduler(_registry(store))
        assert sched.decide(provider="grok").account is None
        assert sched.decide(provider="grok", account="good-1").error == "account_unavailable"
        with pytest.raises(ScheduleRefused):
            sched.acquire(provider="grok", account="good-1")

    def test_cli_list_survives_unsafe_stored_filenames(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "store"
        (root / "accounts").mkdir(parents=True)
        (root / "accounts" / ".hidden.json").write_text(
            json.dumps({"id": ".hidden", "provider": "grok", "status": "active"}),
            encoding="utf-8",
        )
        (root / "accounts" / "grok-1.json").write_text(
            json.dumps({"id": "grok-1", "provider": "grok", "status": "active"}),
            encoding="utf-8",
        )
        rc = accounts_main(["--store-dir", str(root), "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert ".hidden" in out
        assert "grok-1" in out
        assert "running=0" in out
