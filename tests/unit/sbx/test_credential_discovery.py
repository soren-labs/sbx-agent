"""``sbx.credentials`` local discovery (SOR-115).

Clean-HOME guidance, per-provider statuses, permission/schema findings, and
the injected/CLI auth check. No real credentials: fixture content is
``REDACTED``-shaped JSON/TOML under an isolated HOME.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sbx.credentials import (
    CredentialScan,
    cli_auth_check,
    scan_credentials,
    scan_provider,
)

SECRET = "sk-test-REDACTED"

CODEX_REL = ".codex/auth.json"
DEVIN_REL = ".local/share/devin/credentials.toml"
AGY_REL = ".gemini/antigravity-cli/antigravity-oauth-token"
AGY_ONBOARDING_REL = ".gemini/antigravity-cli/cache/onboarding.json"
GROK_REL = ".grok/auth.json"
OPENCODE_REL = ".local/share/opencode/auth.json"


def _write(home: Path, relpath: str, content: str, mode: int = 0o600) -> Path:
    path = home / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _scan(scan: CredentialScan) -> str:
    return f"{scan.status} {scan.detail} {scan.hint}"


class TestCleanHome:
    def test_clean_home_reports_not_found_with_login_guidance(self, tmp_path: Path) -> None:
        scans = scan_credentials(
            ("codex", "devin", "antigravity", "grok", "opencode"), home=tmp_path
        )
        assert [s.provider for s in scans] == [
            "codex",
            "devin",
            "antigravity",
            "grok",
            "opencode",
        ]
        for scan in scans:
            assert scan.status == "not_found"
            assert not scan.ok
            assert scan.login  # official login entry point always present
            assert "~/" in scan.detail  # path the login command writes

    def test_not_found_hint_names_the_file_and_login(self, tmp_path: Path) -> None:
        scan = scan_provider("codex", home=tmp_path)
        assert "~/.codex/auth.json" in scan.detail
        assert "codex login" in (scan.hint or "")

    def test_unselected_providers_are_not_scanned(self, tmp_path: Path) -> None:
        scans = scan_credentials(("grok",), home=tmp_path)
        assert [s.provider for s in scans] == ["grok"]

    def test_unknown_provider_is_skipped(self, tmp_path: Path) -> None:
        scan = scan_provider("not-a-provider", home=tmp_path)
        assert scan.status == "skipped"


class TestFindings:
    def test_found_credential_is_discovered(self, tmp_path: Path) -> None:
        _write(tmp_path, GROK_REL, f'{{"token": "{SECRET}"}}')
        scan = scan_provider("grok", home=tmp_path)
        assert scan.status == "discovered"
        assert scan.ok
        # Contents never surface; the import path is the actionable hint.
        assert SECRET not in _scan(scan)
        assert "control.onboarding" in (scan.hint or "")

    def test_codex_import_hint_uses_modal_secret(self, tmp_path: Path) -> None:
        _write(tmp_path, CODEX_REL, '{"tokens": {"access_token": "REDACTED"}}')
        scan = scan_provider("codex", home=tmp_path)
        assert "modal secret create" in (scan.hint or "")
        assert "sbx-codex-auth" in (scan.hint or "")

    def test_toml_schema_accepted(self, tmp_path: Path) -> None:
        _write(tmp_path, DEVIN_REL, 'token = "REDACTED"\n')
        scan = scan_provider("devin", home=tmp_path)
        assert scan.status == "discovered"

    def test_token_only_agy_is_discovered(self, tmp_path: Path) -> None:
        """SOR-258: the onboarding marker is optional — its absence is not
        a finding; a clean token alone discovers the account."""
        _write(tmp_path, AGY_REL, '{"token": "x"}')
        scan = scan_provider("antigravity", home=tmp_path)
        assert scan.status == "discovered"

    def test_full_agy_bundle_discovers(self, tmp_path: Path) -> None:
        _write(tmp_path, AGY_REL, '{"token": "x"}')
        _write(tmp_path, AGY_ONBOARDING_REL, '{"onboardingComplete": true}')
        scan = scan_provider("antigravity", home=tmp_path)
        assert scan.status == "discovered"

    def test_broken_agy_marker_still_flags(self, tmp_path: Path) -> None:
        """A present-but-malformed marker is schema_invalid — optional
        only means reconstructible when absent, not ignorable."""
        _write(tmp_path, AGY_REL, '{"token": "x"}')
        _write(tmp_path, AGY_ONBOARDING_REL, "not json")
        scan = scan_provider("antigravity", home=tmp_path)
        assert scan.status == "schema_invalid"

    def test_open_permissions_are_explained(self, tmp_path: Path) -> None:
        path = _write(tmp_path, GROK_REL, '{"token": "x"}', mode=0o644)
        scan = scan_provider("grok", home=tmp_path)
        assert scan.status == "permission_invalid"
        assert "0o644" in scan.detail
        assert f"chmod 600 {path}" in (scan.hint or "")
        assert "--allow-open-permissions" in (scan.hint or "")

    def test_open_permissions_override(self, tmp_path: Path) -> None:
        _write(tmp_path, GROK_REL, '{"token": "x"}', mode=0o644)
        scan = scan_provider("grok", home=tmp_path, allow_open_permissions=True)
        assert scan.status == "discovered"

    def test_invalid_json_is_schema_invalid(self, tmp_path: Path) -> None:
        _write(tmp_path, GROK_REL, "not json at all")
        scan = scan_provider("grok", home=tmp_path)
        assert scan.status == "schema_invalid"
        assert "grok" in (scan.hint or "")  # login guidance, not a guess

    def test_empty_file_is_schema_invalid(self, tmp_path: Path) -> None:
        _write(tmp_path, CODEX_REL, "")
        scan = scan_provider("codex", home=tmp_path)
        assert scan.status == "schema_invalid"

    def test_symlink_is_refused(self, tmp_path: Path) -> None:
        real = _write(tmp_path / "elsewhere", "real.json", "{}")
        link = tmp_path / ".grok"
        link.mkdir(parents=True)
        (link / "auth.json").symlink_to(real)
        scan = scan_provider("grok", home=tmp_path)
        assert scan.status == "schema_invalid"
        assert "symlink" in scan.detail

    def test_never_exposes_content(self, tmp_path: Path) -> None:
        _write(tmp_path, CODEX_REL, f'{{"access_token": "{SECRET}"}}', mode=0o644)
        for allow in (False, True):
            scan = scan_provider("codex", home=tmp_path, allow_open_permissions=allow)
            assert SECRET not in _scan(scan)


class TestAuthCheck:
    def test_verified_when_provider_cli_accepts(self, tmp_path: Path) -> None:
        _write(tmp_path, GROK_REL, '{"token": "x"}')
        scan = scan_provider("grok", home=tmp_path, auth_check=lambda p, h: "ok")
        assert scan.status == "verified"
        assert "auth check passed" in scan.detail

    def test_auth_invalid_produces_relogin_hint(self, tmp_path: Path) -> None:
        _write(tmp_path, GROK_REL, '{"token": "x"}')
        scan = scan_provider("grok", home=tmp_path, auth_check=lambda p, h: "auth_invalid")
        assert scan.status == "auth_invalid"
        assert not scan.ok
        assert "log in" in (scan.hint or "")

    def test_inconclusive_check_keeps_discovered(self, tmp_path: Path) -> None:
        _write(tmp_path, GROK_REL, '{"token": "x"}')
        for outcome in ("probe_unavailable", "cli_missing"):
            scan = scan_provider("grok", home=tmp_path, auth_check=lambda p, h, o=outcome: o)
            assert scan.status == "discovered"
            assert "auth check" in scan.detail

    def test_check_exception_is_inconclusive(self, tmp_path: Path) -> None:
        _write(tmp_path, GROK_REL, '{"token": "x"}')

        def boom(p: str, h: Path) -> str:
            raise RuntimeError("spawn failed")

        scan = scan_provider("grok", home=tmp_path, auth_check=boom)
        assert scan.status == "discovered"

    def test_check_only_runs_on_clean_file(self, tmp_path: Path) -> None:
        _write(tmp_path, GROK_REL, "garbage")
        called: list[str] = []

        def check(p: str, h: Path) -> str:
            called.append(p)
            return "ok"

        scan = scan_provider("grok", home=tmp_path, auth_check=check)
        assert scan.status == "schema_invalid"
        assert called == []  # no point asking the provider about a bad file


class TestCliAuthCheck:
    def test_missing_cli_is_cli_missing(self, tmp_path: Path) -> None:
        env = {"GROK_BIN": str(tmp_path / "no-such-cli"), "PATH": os.defpath}
        assert cli_auth_check("grok", tmp_path, env=env) == "cli_missing"

    def test_unknown_provider_is_probe_unavailable(self, tmp_path: Path) -> None:
        assert cli_auth_check("nope", tmp_path, env={}) == "probe_unavailable"

    def test_runner_failure_is_probe_unavailable(self, tmp_path: Path) -> None:
        def boom(*a: object, **k: object) -> object:
            raise OSError("cannot fork")

        out = cli_auth_check("grok", tmp_path, env={"GROK_BIN": "grok"}, runner=boom)
        assert out == "probe_unavailable"

    def test_child_env_is_scrubbed(self, tmp_path: Path) -> None:
        seen: dict[str, object] = {}

        def runner(argv: object, **kw: object) -> object:
            seen["env"] = kw.get("env")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        cli_auth_check(
            "grok",
            tmp_path,
            env={"GROK_BIN": "grok", "PATH": "/bin", "XAI_API_KEY": SECRET},
            runner=runner,
        )
        child = seen["env"]
        assert isinstance(child, dict)
        assert "XAI_API_KEY" not in child  # ambient credentials never reach the check
        assert child["HOME"] == str(tmp_path)

    def test_bin_override_runs_py_with_interpreter(self, repo_root: Path, tmp_path: Path) -> None:
        fake = repo_root / "tests" / "fakes" / "fake_grok.py"
        home = tmp_path / "home"
        _write(home, GROK_REL, '{"token": "x"}')
        env = {"GROK_BIN": str(fake), "PATH": os.environ.get("PATH", os.defpath)}
        assert cli_auth_check("grok", home, env=env) == "ok"

    def test_bin_override_reports_auth_invalid(self, repo_root: Path, tmp_path: Path) -> None:
        fake = repo_root / "tests" / "fakes" / "fake_grok.py"
        home = tmp_path / "home"  # exists (conftest) but holds no credential
        env = {"GROK_BIN": str(fake), "PATH": os.environ.get("PATH", os.defpath)}
        assert cli_auth_check("grok", home, env=env) == "auth_invalid"

    def test_sys_executable_for_py_bins(self, tmp_path: Path) -> None:
        from control.onboarding import provider_auth_argv

        argv = provider_auth_argv("codex", env={"CODEX_BIN": "/x/fake_codex.py"})
        assert argv == [sys.executable, "/x/fake_codex.py", "login", "status"]


class TestScanToCheck:
    def test_findings_are_warn_level(self, tmp_path: Path) -> None:
        check = scan_provider("grok", home=tmp_path).to_check()
        assert check.name == "cred:grok"
        assert not check.ok and check.warn  # advisory, never a hard failure
        assert check.hint

    def test_discovered_is_ok(self, tmp_path: Path) -> None:
        _write(tmp_path, GROK_REL, "{}")
        check = scan_provider("grok", home=tmp_path).to_check()
        assert check.ok
