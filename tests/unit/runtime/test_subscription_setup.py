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

# Speaks JSON-RPC lines on stdio, like an official CLI's machine interface.
FAKE_APP_SERVER = r"""
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({"id": 1, "result": {"ready": True}}), flush=True)
    if request.get("method") == "model/list":
        items = [
            {"id": "model-a", "displayName": "Model A", "isDefault": True, "secretish": "drop me",
             "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
            {"id": "model-b", "supportedReasoningEfforts": []},
        ]
        print("not json noise", flush=True)
        print(json.dumps({"id": request["id"], "result": {"data": items, "nextCursor": None}}),
              flush=True)
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
        catalog = scenario.pop("catalog", None)
        (tmp_path / "scenario.json").write_text(json.dumps(scenario))
        server = tmp_path / "app_server.py"
        server.write_text(FAKE_APP_SERVER)
        if catalog:
            overrides = {
                **overrides,
                "catalog": {
                    "argv": [sys.executable, str(server)] if catalog == "ok" else ["/nonexistent"],
                    "requests": [
                        {"id": 1, "method": "initialize", "params": {}},
                        {"id": 2, "method": "model/list", "params": {}},
                    ],
                    "result_id": 2,
                    "items_key": "data",
                    "item_fields": ["id", "displayName", "isDefault", "supportedReasoningEfforts"],
                    "timeout": 10,
                },
            }
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


def test_catalog_comes_from_the_cli_and_keeps_only_listed_fields(run) -> None:
    final = run("verify", logged_in=True, catalog="ok")["final"]
    assert final["phase"] == "succeeded" and final["catalog"]["complete"] is True
    first, second = final["catalog"]["items"]
    assert first == {
        "id": "model-a",
        "displayName": "Model A",
        "isDefault": True,
        "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}],
    }
    assert second == {"id": "model-b", "supportedReasoningEfforts": []}


def test_missing_catalog_is_reported_as_missing_not_guessed(run) -> None:
    final = run("verify", logged_in=True, catalog="broken")["final"]
    assert final["phase"] == "succeeded" and final["catalog"] is None


# A full-screen sign-in like an official CLI's remote login: pick a method, show the URL as
# a terminal hyperlink, read the authorization code from the terminal.
FAKE_TUI = r"""
import pathlib, sys, time
home = pathlib.Path(__file__).parent
command = sys.argv[1]
if command == "status":
    ok = (home / "logged_in").exists()
    print("Fetching models...\n" + ("model-a" if ok else "Error: Please sign in to view models."))
    sys.exit(0)
assert sys.stdin.isatty(), "the sign-in screen needs a terminal"
sys.stdout.write("\x1b[2J\x1b[H Welcome. Select login method:\r\n > 1. OAuth\r\n")
sys.stdout.flush()
sys.stdin.readline()
url = "https://signin.example.test/o/oauth2/auth?client_id=abc&state=xyz"
sys.stdout.write("\x1b[2J\x1b[H Open the URL below:\r\n \x1b]8;;" + url + "\x07" + url
                 + "\x1b]8;;\x07\r\n \x1b[2mauthorization code...\x1b[0m\r\n")
sys.stdout.flush()
code = sys.stdin.readline().strip()
if code == "4/0AbC-good_code":
    (home / "logged_in").write_text("1")
    sys.stdout.write("Signed in.\r\n")
else:
    sys.stdout.write("That code did not work.\r\n")
sys.stdout.flush()
time.sleep(60)
"""


@pytest.fixture
def run_pty(run, tmp_path):
    tui = tmp_path / "tui.py"
    tui.write_text(FAKE_TUI)

    def start(code: str | None, **spec) -> dict:
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        if code is not None:
            # The control plane writes the submitted code before or after the URL shows.
            (work / "login-input").write_text(code + "\n")
        return run(
            login="unused",
            spec={
                "login_argv": [sys.executable, str(tui), "login"],
                "status_argv": [sys.executable, str(tui), "status"],
                "status_ok": "",
                "status_fail": ["please sign in"],
                "url_pattern": r"https://signin\.example\.test/o/oauth2/\S+",
                "pty": {
                    "steps": [{"expect": "Select login method", "send": "\r"}],
                    "prompt": "authorization code",
                    "input": "authorization_code",
                    "accept_seconds": 1.5,
                },
                **spec,
            },
        )

    return start


def test_terminal_sign_in_relays_the_url_and_types_the_submitted_code(run_pty) -> None:
    result = run_pty("4/0AbC-good_code")
    waiting = next(s for s in result["seen"] if s["phase"] == "awaiting_user")
    assert waiting["verification_url"] == (
        "https://signin.example.test/o/oauth2/auth?client_id=abc&state=xyz"
    )
    assert waiting["input"] == "authorization_code" and "user_code" not in waiting
    assert result["final"]["phase"] == "succeeded"
    # The submitted code is consumed: not in any published state, not left on disk.
    assert "4/0AbC-good_code" not in json.dumps(result["seen"])
    assert not (result["work"] / "login-input").exists()
    assert not (result["work"] / "login-output").exists()
    assert "input" not in result["final"]


def test_terminal_sign_in_reports_a_code_the_cli_did_not_accept(run_pty) -> None:
    result = run_pty("4/0AbC-wrong_code")
    assert result["final"]["phase"] == "failed"
    assert result["final"]["error"] == "code_rejected"
    assert "4/0AbC-wrong_code" not in json.dumps(result["seen"])


def test_terminal_sign_in_never_types_malformed_input(run_pty) -> None:
    result = run_pty("4/0AbC-good_code; rm -rf /", deadline_seconds=2)
    assert result["final"]["phase"] == "expired"
    assert not (result["work"] / "login-input").exists()


def test_terminal_sign_in_expires_when_no_code_arrives(run_pty) -> None:
    result = run_pty(None, deadline_seconds=1.5)
    assert [s["phase"] for s in result["seen"]][-2:] == ["awaiting_user", "expired"]
    assert result["final"]["error"] == "code_expired"


def test_signed_out_words_decide_even_when_the_status_command_exits_zero(run) -> None:
    result = run(
        "verify",
        logged_in=True,
        spec={"status_ok": "", "status_fail": ["logged in using test"]},
    )
    assert result["final"]["error"] == "not_logged_in"
