"""``sbx auth`` CLI surface (SOR-213/SOR-216): login/import/verify/relink/
logout over the shared AuthService — deterministic stores + probes, no
real vendor CLIs or cloud credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from control.onboarding import ProbeResult
from sbx_fakes import FakePlane

from sbx.cli import main

SECRET = "SECRET_TOKEN_VALUE_cli_auth_3c4e"
GROK_AUTH_REL = ".grok/auth.json"


class _ScriptedProbe:
    """Deterministic authoritative probe for the AuthService seam."""

    authoritative = True

    def __init__(self, status: str) -> None:
        self.result = ProbeResult(status, "scripted")

    def probe(self, account: Any, blob: dict | None) -> ProbeResult:
        return self.result


def _args(tmp_path: Path) -> list[str]:
    return ["--config", str(tmp_path / "config.toml"), "--state-dir", str(tmp_path / "state")]


@pytest.fixture(autouse=True)
def _store_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SBX_ACCOUNT_STORE_DIR", str(tmp_path / "accounts"))


def _creds(home: Path, token: str = "x") -> Path:
    path = home / GROK_AUTH_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"token": "{token}"}}', encoding="utf-8")
    path.chmod(0o600)
    return path


def test_import_existing_no_verify_lands_unverified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _creds(Path(os.environ["HOME"]), token=SECRET)
    rc = main(
        [
            *_args(tmp_path),
            "auth",
            "import-existing",
            "--provider",
            "grok",
            "--no-verify",
            "--json",
        ],
        plane=FakePlane(),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is True
    assert payload["verified"] is False
    assert payload["session"]["status"] == "unverified"
    assert payload["session"]["auth_state"] == "materialized"
    assert SECRET not in json.dumps(payload)


def test_verify_promotes_then_status_reports_verified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _creds(Path(os.environ["HOME"]))
    rc = main(
        [
            *_args(tmp_path),
            "auth",
            "import-existing",
            "--provider",
            "grok",
            "--account-id",
            "g1",
            "--no-verify",
            "--json",
        ],
        plane=FakePlane(),
    )
    assert rc == 0
    capsys.readouterr()

    rc = main(
        [
            *_args(tmp_path),
            "auth",
            "verify",
            "--account-id",
            "g1",
            "--json",
        ],
        plane=FakePlane(),
        probe=_ScriptedProbe("ok"),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    entry = payload["accounts"][0]
    assert entry["verified"] is True
    assert entry["lane"] == "local"
    assert entry["session"]["auth_state"] == "verified"

    rc = main([*_args(tmp_path), "auth", "status", "--json"], plane=FakePlane())
    assert rc == 0
    listing = json.loads(capsys.readouterr().out)
    account = listing["accounts"][0]
    assert account["account_id"] == "g1"
    assert account["status"] == "active"
    assert account["auth_state"] == "verified"
    assert account["schedulable"] is True


def test_login_runs_official_flow_and_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Path(os.environ["HOME"])
    ran: list[list[str]] = []

    def fake_login(argv: list[str], env: dict[str, str]) -> int:
        ran.append(list(argv))
        assert env["HOME"] == str(home)
        # The official CLI writes its own credential file under HOME.
        _creds(home)
        return 0

    rc = main(
        [*_args(tmp_path), "auth", "login", "--provider", "grok", "--json"],
        plane=FakePlane(),
        probe=_ScriptedProbe("ok"),
        login_runner=fake_login,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert ran == [["grok"]]
    assert payload["verified"] is True
    assert payload["session"]["auth_state"] == "verified"


def test_login_failed_exits_with_error_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [*_args(tmp_path), "auth", "login", "--provider", "grok"],
        plane=FakePlane(),
        login_runner=lambda argv, env: 1,
    )
    assert rc == 1
    assert "error[login_failed]" in capsys.readouterr().err


def test_relink_restores_after_auth_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Path(os.environ["HOME"])
    _creds(home, token="v1")
    rc = main(
        [
            *_args(tmp_path),
            "auth",
            "import-existing",
            "--provider",
            "grok",
            "--account-id",
            "g1",
            "--no-verify",
        ],
        plane=FakePlane(),
    )
    assert rc == 0
    capsys.readouterr()
    # Provider rejects the grant; the account leaves scheduling.
    from control.accounts import PersistentAccountRegistry, select_store
    from control.credlifecycle import CredentialLifecycleService

    registry = PersistentAccountRegistry(select_store())
    CredentialLifecycleService(registry).on_auth_invalid("g1")
    registry.mark_status("g1", "invalid")
    # A new grant at the same path: relink re-captures + verifies.
    _creds(home, token="v2")
    rc = main(
        [*_args(tmp_path), "auth", "relink", "--account-id", "g1", "--json"],
        plane=FakePlane(),
        probe=_ScriptedProbe("ok"),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    entry = payload["accounts"][0]
    assert entry["verified"] is True
    assert entry["session"]["status"] == "active"
    assert entry["session"]["auth_state"] == "verified"


def test_logout_drops_material_and_flips_unverified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Path(os.environ["HOME"])
    creds = _creds(home)
    plane = FakePlane()
    rc = main(
        [
            *_args(tmp_path),
            "auth",
            "import-existing",
            "--provider",
            "grok",
            "--account-id",
            "g1",
            "--no-verify",
        ],
        plane=plane,
    )
    assert rc == 0
    capsys.readouterr()

    rc = main(
        [*_args(tmp_path), "auth", "logout", "--account-id", "g1", "--local", "--json"],
        plane=plane,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    entry = payload["accounts"][0]
    assert entry["session"]["status"] == "unverified"
    assert entry["session"]["auth_state"] == "unauthenticated"
    assert not creds.exists()

    rc = main([*_args(tmp_path), "auth", "status", "--json"], plane=plane)
    listing = json.loads(capsys.readouterr().out)
    assert listing["accounts"][0]["status"] == "unverified"


def test_verify_requires_a_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([*_args(tmp_path), "auth", "verify"], plane=FakePlane())
    assert rc == 1
    assert "error[missing_target]" in capsys.readouterr().err


def test_unknown_provider_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            *_args(tmp_path),
            "auth",
            "import-existing",
            "--provider",
            "bogus",
            "--no-verify",
        ],
        plane=FakePlane(),
    )
    assert rc == 1
    assert "error[unknown_provider]" in capsys.readouterr().err


def test_no_credential_capture_is_structured_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            *_args(tmp_path),
            "auth",
            "import-existing",
            "--provider",
            "grok",
            "--no-verify",
        ],
        plane=FakePlane(),
    )
    assert rc == 1
    assert "error[missing_credential_file]" in capsys.readouterr().err
