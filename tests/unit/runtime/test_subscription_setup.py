"""Setup VM supervisor against a scripted stand-in CLI (real subprocesses, no cloud).

The stand-in fails if its stdin is left open, guarding the stall found in the #199
experiments: official CLIs block on an open stdin in automation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from runtime.subscriptions.setup import Supervisor

FAKE_CLI = r"""
import json, pathlib, sys, time
home = pathlib.Path(__file__).parent
scenario = json.loads((home / "scenario.json").read_text())
if sys.stdin.read() != "":
    sys.exit(97)  # stdin must be closed
command = sys.argv[1]
if command == "login":
    print("Open \x1b[94mhttps://auth.example.test/device\x1b[0m and enter")
    print("  WXYZ-12345  (expires in 15 minutes)", flush=True)
    time.sleep(scenario.get("approve_after", 0.2))
    how = scenario["login"]
    if how in ("approve", "approve_but_exit_nonzero"):
        (home / "logged_in").write_text("1")
        sys.exit(0 if how == "approve" else 1)
    if how == "deny":
        print("Error: authorization denied")
        sys.exit(1)
    time.sleep(60)
elif command == "status":
    ok = (home / "logged_in").exists()
    print("Logged in using Test" if ok else "Not logged in")
    sys.exit(0 if ok else 1)
elif command == "exec":
    print(scenario.get("exec_output", '{"type":"turn.completed"}'))
    sys.exit(scenario.get("exec_rc", 0))
elif command == "version":
    print("fake-cli 1.2.3")
"""


@pytest.fixture
def run(tmp_path):
    cli = tmp_path / "cli.py"
    cli.write_text(FAKE_CLI)
    profile = tmp_path / "profile"
    profile.mkdir()

    def start(mode: str = "login", **scenario) -> dict:
        logged_in = scenario.pop("logged_in", False)
        overrides = scenario.pop("spec", {})
        (tmp_path / "scenario.json").write_text(json.dumps(scenario))
        if logged_in:
            (tmp_path / "logged_in").write_text("1")
        base = [sys.executable, str(cli)]
        spec = {
            "mode": mode,
            "work_dir": str(tmp_path / "work"),
            "profile_dir": str(profile),
            "ensure_dirs": [str(profile / ".cli")],
            "login_argv": [*base, "login"],
            "status_argv": [*base, "status"],
            "status_ok": "Logged in using Test",
            "version_argv": [*base, "version"],
            "verify_argv": [*base, "exec"],
            "verify_ok": '"turn.completed"',
            "verify_cwd": str(tmp_path / "work" / "verify"),
            "url_pattern": r"https://auth\.example\.test/\S+",
            "code_pattern": r"\b[A-Z0-9]{4,6}-[A-Z0-9]{4,6}\b",
            "expiry_pattern": r"expires in\s+(\d+)\s+minutes?",
            "default_code_seconds": 900,
            "deadline_seconds": 30,
            "status_interval": 0.1,
            "exit_grace": 0.4,
            "linger_seconds": 0,
            **overrides,
        }
        supervisor = Supervisor(spec)
        seen: list[dict] = []
        publish = supervisor.publish

        def recording(**changes):
            publish(**changes)
            seen.append(dict(supervisor.state))

        supervisor.publish = recording
        phase = supervisor.run()
        final = json.loads(Path(spec["work_dir"], "state.json").read_text())
        assert final["phase"] == phase
        return {"final": final, "seen": seen, "work": Path(spec["work_dir"]), "profile": profile}

    return start


def test_device_login_reports_the_real_url_and_code_then_verifies_and_syncs(run) -> None:
    result = run(login="approve")
    waiting = next(s for s in result["seen"] if s["phase"] == "awaiting_user")
    assert waiting["verification_url"] == "https://auth.example.test/device"
    assert waiting["user_code"] == "WXYZ-12345" and waiting["code_expires_at"]
    assert [s["phase"] for s in result["seen"]][-1] == "succeeded"
    final = result["final"]
    assert final["real_model_call"] is True and final["profile_synced"] is True
    assert final["cli_version"] == "fake-cli 1.2.3"
    assert "user_code" not in final, "the one-time code is dropped once it was used"
    assert not (result["work"] / "login-output").exists()
    assert (result["profile"] / ".cli").is_dir()


def test_accepted_login_wins_over_a_login_process_that_exited_nonzero(run) -> None:
    assert run(login="approve_but_exit_nonzero")["final"]["phase"] == "succeeded"


def test_denied_login_fails_without_verifying(run) -> None:
    result = run(login="deny")
    assert result["final"]["phase"] == "failed" and result["final"]["error"] == "login_denied"
    assert "verifying" not in [s["phase"] for s in result["seen"]]
    assert "user_code" not in result["final"]


def test_unapproved_code_expires_and_stops_the_login_process(run) -> None:
    result = run(login="hang", spec={"deadline_seconds": 1.2})
    assert result["final"]["phase"] == "expired" and result["final"]["error"] == "code_expired"


def test_login_that_cannot_make_a_real_call_is_not_ready(run) -> None:
    result = run(login="approve", exec_rc=1, exec_output="401 Unauthorized")
    assert result["final"]["phase"] == "failed"
    assert result["final"]["error"] == "verification_failed:unauthorized"


def test_usage_limited_plan_is_still_an_authorized_login(run) -> None:
    result = run(login="approve", exec_rc=1, exec_output="You've hit your usage limit")
    assert result["final"]["phase"] == "succeeded"
    assert result["final"]["real_model_call"] is False
    assert result["final"]["warning"] == "usage_limited"


def test_verify_mode_never_starts_a_login(run) -> None:
    ok = run("verify", logged_in=True)
    assert ok["final"]["phase"] == "succeeded"
    assert all("verification_url" not in s for s in ok["seen"])


def test_verify_mode_reports_a_missing_login(run) -> None:
    missing = run("verify")
    assert missing["final"]["phase"] == "failed" and missing["final"]["error"] == "not_logged_in"
