"""runtime.runner.credentials: SBX_ACCOUNT_CREDENTIAL restore + env scrub (SOR-74)."""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from runtime.runner.codex import child_env
from runtime.runner.credentials import (
    AGENT_ENV_EXCLUDE,
    CREDENTIAL_ENV,
    DEVIN_ENV_EXCLUDE,
    CredentialError,
    restore_credential_blob,
    sandbox_home,
    scrub_child_env,
)

DEVIN_CRED_TOML = (
    'windsurf_api_key = "REDACTED"\n'
    'api_server_url = "https://server.codeium.com"\n'
    'devin_webapp_host = "app.devin.ai"\n'
    'devin_api_url = "https://api.devin.ai"\n'
)


def _blob(files: dict, provider: str = "devin") -> str:
    return json.dumps({"provider": provider, "files": files})


def test_denylist_covers_acp_and_devin_key_env() -> None:
    for key in (
        "ACP_BACKEND",
        "DEVIN_API_KEY",
        "DEVIN_V3_API_KEY",
        "DEVIN_LEGACY_API_KEY",
        "DEVIN_ORG_ID",
        "WINDSURF_API_KEY",
        "DEVIN_OUTPOSTS_TOKEN",
    ):
        assert key in DEVIN_ENV_EXCLUDE
        assert key in AGENT_ENV_EXCLUDE
    assert CREDENTIAL_ENV in AGENT_ENV_EXCLUDE
    assert "CODEX_AUTH_JSON" in AGENT_ENV_EXCLUDE


def test_sandbox_home_is_work_home(tmp_path: Path) -> None:
    assert sandbox_home(tmp_path) == tmp_path / "home"


def test_restore_writes_files_mode_600(tmp_path: Path) -> None:
    env = {
        CREDENTIAL_ENV: _blob(
            {".local/share/devin/credentials.toml": DEVIN_CRED_TOML, "notes.txt": "hi"}
        )
    }
    home = tmp_path / "home"
    written = restore_credential_blob(home, provider="devin", env=env)
    cred = home / ".local" / "share" / "devin" / "credentials.toml"
    assert written == [cred, home / "notes.txt"]
    assert cred.read_text(encoding="utf-8") == DEVIN_CRED_TOML
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / "notes.txt").stat().st_mode) == 0o600


def test_restore_no_env_is_noop(tmp_path: Path) -> None:
    home = tmp_path / "sbx" / "home"  # not the conftest-isolated $HOME
    assert restore_credential_blob(home, provider="devin", env={}) == []
    assert not home.exists()


def test_restore_provider_mismatch_raises(tmp_path: Path) -> None:
    home = tmp_path / "sbx" / "home"
    env = {CREDENTIAL_ENV: _blob({".codex/auth.json": "{}"}, provider="codex")}
    with pytest.raises(CredentialError, match="does not match"):
        restore_credential_blob(home, provider="devin", env=env)
    assert not home.exists()


def test_restore_rejects_bad_json(tmp_path: Path) -> None:
    env = {CREDENTIAL_ENV: "not-json{"}
    with pytest.raises(CredentialError, match="not valid JSON"):
        restore_credential_blob(tmp_path / "home", provider="devin", env=env)


@pytest.mark.parametrize(
    "relpath",
    ["../escape.txt", "/abs/credentials.toml", "a/../../escape.txt", ".hidden/../x", ""],
)
def test_restore_rejects_traversal(tmp_path: Path, relpath: str) -> None:
    env = {CREDENTIAL_ENV: _blob({relpath: "x"})}
    with pytest.raises(CredentialError):
        restore_credential_blob(tmp_path / "home", provider="devin", env=env)
    assert not (tmp_path / "escape.txt").exists()


def test_restore_rejects_bad_content_type(tmp_path: Path) -> None:
    env = {CREDENTIAL_ENV: _blob({"cred.txt": 123})}
    with pytest.raises(CredentialError):
        restore_credential_blob(tmp_path / "home", provider="devin", env=env)


def test_restore_decodes_content_b64(tmp_path: Path) -> None:
    payload = base64.b64encode(b"\x00binary\xff").decode("ascii")
    env = {CREDENTIAL_ENV: _blob({"bin.dat": {"content_b64": payload}})}
    restore_credential_blob(tmp_path / "home", provider="devin", env=env)
    assert (tmp_path / "home" / "bin.dat").read_bytes() == b"\x00binary\xff"


def test_scrub_child_env_removes_denylist_only() -> None:
    src = {key: "x" for key in AGENT_ENV_EXCLUDE}
    src.update({"HOME": "/h", "SBX_WORK": "/w", "DEVIN_MODEL": "swe-2-high"})
    env = scrub_child_env(src)
    for key in AGENT_ENV_EXCLUDE:
        assert key not in env
    assert env["HOME"] == "/h"
    assert env["DEVIN_MODEL"] == "swe-2-high"


def test_codex_child_env_drops_credential_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_ENV, "blob")
    monkeypatch.setenv("CODEX_AUTH_JSON", "{}")
    monkeypatch.setenv("SBX_PROVIDER_API_KEY", "keepme")  # needed by --auth provider
    env = child_env(Path("/w"), Path("/h"))
    assert CREDENTIAL_ENV not in env
    assert "CODEX_AUTH_JSON" not in env
    assert env["SBX_PROVIDER_API_KEY"] == "keepme"


def test_scrubbed_spawned_process_cannot_see_bridge_env() -> None:
    src = dict(os.environ)
    src.update({key: "polluted" for key in DEVIN_ENV_EXCLUDE})
    src[CREDENTIAL_ENV] = "blob"
    out = subprocess.run(
        [sys.executable, "-c", "import json,os;print(json.dumps(sorted(os.environ)))"],
        env=scrub_child_env(src),
        capture_output=True,
        text=True,
        check=True,
    )
    keys = json.loads(out.stdout)
    for key in AGENT_ENV_EXCLUDE:
        assert key not in keys
