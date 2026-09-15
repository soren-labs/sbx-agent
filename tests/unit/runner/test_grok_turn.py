"""runner init/turn/export-credentials for provider=grok (SOR-62).

End-to-end through ``python -m runtime.runner`` with ``GROK_BIN`` pointed
at ``tests/unit/runner/replay_grok.py``, which speaks the production argv
contract and replays the staged real SOR-60 captures
(``tests/unit/runner/fixtures/grok/*.jsonl``). Covers argv shape, stdin
closed, credential blob lifecycle, the grok env denylist, stale-resume
detection (rc=1 + empty stdout + stderr 404), health -> exit-code
mapping, and events.raw.jsonl / events.jsonl separation. A second e2e
path drives ``tests/fakes/fake_grok.py`` (WP0 fixture shape).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest
from tests.unit.runner.conftest import load_json, parsed_events, run_runner

MODEL = "grok-4.6"
SESS_ID = "01a0a2d7-87bc-7192-b642-ad17e2cb3b55"
WP0_ID = "01a09b11-0000-7f90-b96e-42adeefa05e0"
REPLAYER = Path(__file__).resolve().parent / "replay_grok.py"
REAL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "grok"
WP0_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "events" / "grok"
FAKE_GROK = Path(__file__).resolve().parents[2] / "fakes" / "fake_grok.py"
AUTH_REL = ".grok/auth.json"
AUTH_JSON = (
    '{"https://auth.x.ai::00000000-0000-0000-0000-000000000000":'
    '{"key":"REDACTED","auth_mode":"oauth","refresh_token":"REDACTED",'
    '"expires_at":"2000-01-01T00:00:00Z"}}\n'
)


@pytest.fixture
def grok_env(work: Path, repo_root: Path) -> dict[str, str]:
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SBX_WORK": str(work),
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
        "GROK_BIN": str(REPLAYER),
        "GROK_REPLAY_FIXTURE": str(REAL_FIXTURES / "success.jsonl"),
        "SBX_BACKEND": "local",
    }


def init_grok(env: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
    args = ["init", "--provider", "grok", "--model", MODEL]
    if "account_id" in extra:
        args += ["--account-id", extra["account_id"]]
    result = run_runner(args, env)
    assert result.returncode == 0, result.stderr
    return result


def turn(
    env: dict[str, str], work: Path, n: int, *, max_seconds: int = 30, timeout: float = 30.0
) -> tuple[int, dict, list[dict]]:
    msg = work / f"msg{n}.md"
    msg.write_text(f"turn {n} prompt\n", encoding="utf-8")
    result = run_runner(
        ["turn", "--n", str(n), "--message-file", str(msg), "--max-seconds", str(max_seconds)],
        env,
        timeout=timeout,
    )
    turn_path = work / "turns" / f"{n}.json"
    doc = load_json(turn_path) if turn_path.is_file() else {}
    return result.returncode, doc, parsed_events(work)


def test_init_layout_and_session(work: Path, grok_env: dict[str, str]) -> None:
    init_grok(grok_env, account_id="acct-grok-1")
    session = load_json(work / "session.json")
    assert session["provider"] == "grok"
    assert session["account_id"] == "acct-grok-1"
    assert session["model"] == MODEL
    assert session["native_session_id"] is None
    assert (work / "events.jsonl").read_text() == ""
    assert (work / "events.raw.jsonl").read_text() == ""
    # Codex-specific artefacts must not appear for other providers.
    assert not (work / ".codex" / "auth.json").exists()
    # prepare_home created a writable ~/.grok for the CLI's self-extract.
    grok_dir = work / "home" / ".grok"
    assert grok_dir.is_dir()
    assert stat.S_IMODE(grok_dir.stat().st_mode) == 0o700


def test_init_restores_auth_blob(work: Path, grok_env: dict[str, str]) -> None:
    grok_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "grok", "files": {AUTH_REL: AUTH_JSON}}
    )
    init_grok(grok_env)
    auth = work / "home" / AUTH_REL
    assert auth.is_file()
    assert auth.read_text(encoding="utf-8") == AUTH_JSON
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    assert stat.S_IMODE(auth.parent.stat().st_mode) == 0o700
    session = load_json(work / "session.json")
    assert session["credential_files"] == [AUTH_REL]
    # The blob value must not leak into events or session files.
    assert AUTH_JSON not in (work / "events.jsonl").read_text(encoding="utf-8")
    assert "SBX_ACCOUNT_CREDENTIAL" not in (work / "session.json").read_text(encoding="utf-8")


def test_init_rejects_provider_mismatch(work: Path, grok_env: dict[str, str]) -> None:
    grok_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "codex", "files": {".codex/auth.json": "{}"}}
    )
    result = run_runner(["init", "--provider", "grok", "--model", MODEL], grok_env)
    assert result.returncode != 0
    assert not (work / "home" / AUTH_REL).exists()


def test_turn_success_canonical_events(work: Path, grok_env: dict[str, str]) -> None:
    init_grok(grok_env, account_id="acct-1")
    code, doc, events = turn(grok_env, work, 1)
    assert code == 0
    types = [e["type"] for e in events]
    assert types[0] == "sbx.session_meta"
    assert events[0]["provider"] == "grok"
    assert events[0]["model"] == MODEL
    assert events[0]["account_id"] == "acct-1"
    assert types[1] == "sbx.turn_started"
    assert "turn.started" in types
    assert "item.started" in types
    assert "item.completed" in types
    assert "turn.completed" in types
    assert "thread.started" in types
    started = [e for e in events if e["type"] == "thread.started"]
    assert started[0]["thread_id"] == SESS_ID
    assert types[-1] == "sbx.turn_finished"
    assert events[-1]["status"] == "success"
    assert events[-1]["exit_code"] == 0

    items = [e["item"] for e in events if isinstance(e.get("item"), dict)]
    kinds = {i["type"] for i in items}
    assert {"reasoning", "command_execution", "file_change", "agent_message"} <= kinds
    messages = [i for i in items if i["type"] == "agent_message"]
    assert messages[-1]["text"] == "DONE"

    assert doc["status"] == "success"
    assert doc["native_session_id"] == SESS_ID
    assert doc["codex_session_id"] == SESS_ID
    assert doc["message"] == "DONE"
    assert doc["usage"]["input_tokens"] == 17255
    assert doc["usage"]["cached_input_tokens"] == 16768
    assert doc["usage"]["reasoning_output_tokens"] == 66
    session = load_json(work / "session.json")
    assert session["native_session_id"] == SESS_ID
    assert session["turn"] == 1

    # Native NDJSON lands in events.raw.jsonl, canonical in events.jsonl.
    raw_lines = (work / "events.raw.jsonl").read_text().splitlines()
    assert any('"type": "available_commands"' in line for line in raw_lines)
    assert not any('"sbx.turn_started"' in line for line in raw_lines)
    assert (work / "turns" / "1.stderr").is_file()


def test_usage_signature_redacted_from_raw_stream(work: Path, grok_env: dict[str, str]) -> None:
    """Step-level usage.signature is an account-bound blob: it must be
    redacted before the native line reaches events.raw.jsonl."""
    init_grok(grok_env)
    fixture = work / "sig_fixture.jsonl"
    fixture.write_text(
        json.dumps(
            {
                "type": "usage",
                "usage": {"input_tokens": 1},
                "signature": "sig-SECRETBLOB",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "end",
                "stopReason": "end_turn",
                "sessionId": "sig-sess",
                "usage": {"input_tokens": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    grok_env["GROK_REPLAY_FIXTURE"] = str(fixture)
    code, _, _ = turn(grok_env, work, 1)
    assert code == 0
    raw = (work / "events.raw.jsonl").read_text(encoding="utf-8")
    assert "sig-SECRETBLOB" not in raw
    assert "REDACTED" in raw


def test_argv_exact_and_stdin_devnull(work: Path, grok_env: dict[str, str]) -> None:
    spy_out = work / "spy.json"
    grok_env["GROK_REPLAY_SPY_OUT"] = str(spy_out)
    init_grok(grok_env)
    code, _, _ = turn(grok_env, work, 1)
    assert code == 0
    spy = json.loads(spy_out.read_text())
    assert spy["argv"] == [
        "-p",
        "turn 1 prompt\n",
        "--output-format",
        "streaming-json",
        "--model",
        MODEL,
        "--permission-mode",
        "bypassPermissions",
    ]
    assert spy["stdin_target"] == "/dev/null"
    assert spy["stdin_isatty"] is False

    grok_env["GROK_REPLAY_FIXTURE"] = str(REAL_FIXTURES / "resume.jsonl")
    code2, _, _ = turn(grok_env, work, 2)
    assert code2 == 0
    spy2 = json.loads(spy_out.read_text())
    assert spy2["argv"] == [
        "-p",
        "turn 2 prompt\n",
        "--resume",
        SESS_ID,
        "--output-format",
        "streaming-json",
        "--model",
        MODEL,
        "--permission-mode",
        "bypassPermissions",
    ]


def test_credential_env_not_forwarded_to_cli(work: Path, grok_env: dict[str, str]) -> None:
    """The CLI subprocess never sees the credential blob or any grok/x.ai
    env auth channel (GROK_SPIKE.md env denylist)."""
    spy_out = work / "spy.json"
    grok_env.update(
        {
            "GROK_REPLAY_SPY_OUT": str(spy_out),
            "SBX_ACCOUNT_CREDENTIAL": json.dumps(
                {"provider": "grok", "files": {AUTH_REL: AUTH_JSON}}
            ),
            "CODEX_AUTH_JSON": json.dumps({"tokens": {"access_token": "REDACTED"}}),
            "XAI_API_KEY": "sentinel-xai-key",
            "GROK_AUTH_PROVIDER_ACCESS_TOKEN": "sentinel-grok-token",
        }
    )
    init_grok(grok_env)
    code, _, events = turn(grok_env, work, 1)
    assert code == 0
    spy = json.loads(spy_out.read_text())
    assert spy["has_SBX_ACCOUNT_CREDENTIAL"] is False
    assert spy["has_CODEX_AUTH_JSON"] is False
    assert spy["has_XAI_API_KEY"] is False
    assert spy["has_GROK_AUTH_PROVIDER_ACCESS_TOKEN"] is False
    raw = (work / "events.jsonl").read_text(encoding="utf-8")
    assert "sentinel" not in raw
    assert AUTH_JSON not in raw


def test_resume_turn_keeps_session(work: Path, grok_env: dict[str, str]) -> None:
    init_grok(grok_env)
    code1, doc1, _ = turn(grok_env, work, 1)
    assert code1 == 0
    grok_env["GROK_REPLAY_FIXTURE"] = str(REAL_FIXTURES / "resume.jsonl")
    code2, doc2, events2 = turn(grok_env, work, 2)
    assert code2 == 0
    assert doc2["native_session_id"] == doc1["native_session_id"] == SESS_ID
    types2 = [e["type"] for e in events2]
    assert types2.count("sbx.session_meta") == 1  # emitted once, on turn 1
    assert types2.count("sbx.turn_started") == 2
    session = load_json(work / "session.json")
    assert session["native_session_id"] == SESS_ID


def test_stale_resume_fails_rc1_empty_stdout(work: Path, grok_env: dict[str, str]) -> None:
    """Stale --resume id: real CLI exits rc=1 with EMPTY stdout and a
    stderr 404 — no stream signal. The turn fails (exit 2) and
    session.json keeps the requested id."""
    init_grok(grok_env)
    code1, doc1, _ = turn(grok_env, work, 1)
    assert code1 == 0
    assert doc1["native_session_id"] == SESS_ID
    grok_env.update(
        {
            "GROK_REPLAY_FIXTURE": str(REAL_FIXTURES / "success.jsonl"),
            "GROK_REPLAY_STALE": "1",
        }
    )
    code2, doc2, events2 = turn(grok_env, work, 2)
    assert code2 == 2
    assert doc2["status"] == "codex_error"
    assert doc2["native_session_id"] == SESS_ID
    # Nothing was emitted for the failed resume turn.
    started2 = [e for e in events2 if e["type"] == "thread.started"]
    assert {e["thread_id"] for e in started2} == {SESS_ID}
    finished = [e for e in events2 if e["type"] == "sbx.turn_finished"]
    assert finished[-1]["status"] == "codex_error"
    session = load_json(work / "session.json")
    assert session["native_session_id"] == SESS_ID
    stderr_tail = (work / "turns" / "2.stderr").read_text(encoding="utf-8")
    assert "Failed to restore session from remote" in stderr_tail


def test_nonzero_exits_2(work: Path, grok_env: dict[str, str]) -> None:
    init_grok(grok_env)
    grok_env["GROK_REPLAY_FIXTURE"] = str(REAL_FIXTURES / "nonzero.jsonl")
    grok_env["GROK_REPLAY_RC"] = "1"
    code, doc, events = turn(grok_env, work, 1)
    assert code == 2
    assert doc["status"] == "codex_error"
    failed = [e for e in events if e["type"] == "turn.failed"]
    assert failed and failed[-1]["error"]["message"].startswith("Couldn't set model")
    assert any(e["type"] == "error" for e in events)


def test_auth_invalid_exits_5(work: Path, grok_env: dict[str, str]) -> None:
    init_grok(grok_env)
    grok_env["GROK_REPLAY_FIXTURE"] = str(REAL_FIXTURES / "auth_invalid.jsonl")
    grok_env["GROK_REPLAY_RC"] = "1"
    grok_env["GROK_REPLAY_STDERR"] = (
        "Error: Not signed in. To authenticate without a browser, run: grok login --device-code"
    )
    code, doc, events = turn(grok_env, work, 1)
    assert code == 5
    assert doc["status"] == "auth_invalid"
    assert doc["health"] == "auth_invalid"
    finished = [e for e in events if e["type"] == "sbx.turn_finished"]
    assert finished[-1]["status"] == "auth_invalid"


def test_badjson_exits_4(work: Path, grok_env: dict[str, str]) -> None:
    init_grok(grok_env)
    grok_env["GROK_REPLAY_FIXTURE"] = str(WP0_FIXTURES / "badjson.jsonl")
    code, doc, events = turn(grok_env, work, 1)
    assert code == 4
    assert doc["status"] == "bad_json"
    assert doc["bad_json_lines"] >= 1
    assert any(e["type"] == "sbx.error" for e in events)
    # Stream continues after the bad line.
    assert any(e["type"] == "turn.completed" for e in events)


def test_slow_completes(work: Path, grok_env: dict[str, str]) -> None:
    init_grok(grok_env)
    grok_env["GROK_REPLAY_PAUSE_S"] = "0.5"
    started = time.monotonic()
    code, doc, _ = turn(grok_env, work, 1, max_seconds=30)
    assert code == 0
    assert time.monotonic() - started >= 0.5
    assert doc["status"] == "success"


def test_hang_times_out(work: Path, grok_env: dict[str, str]) -> None:
    """WP0-shape hang fixture: init carries sessionId on line 0, so the
    thread id is still recorded before the hang (unlike real
    streaming-json, where the id only arrives on `end`)."""
    init_grok(grok_env)
    grok_env["GROK_REPLAY_FIXTURE"] = str(WP0_FIXTURES / "hang.jsonl")
    grok_env["GROK_REPLAY_HANG"] = "1"
    code, doc, events = turn(grok_env, work, 1, max_seconds=1, timeout=60.0)
    assert code == 3
    assert doc["status"] == "timeout"
    assert doc["native_session_id"] == WP0_ID  # thread seen before hang
    finished = [e for e in events if e["type"] == "sbx.turn_finished"]
    assert finished[-1]["status"] == "timeout"


def test_export_credentials_roundtrip(work: Path, grok_env: dict[str, str]) -> None:
    grok_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "grok", "files": {AUTH_REL: AUTH_JSON}}
    )
    init_grok(grok_env)
    same = run_runner(["export-credentials"], grok_env)
    assert same.returncode == 0
    assert same.stdout.strip() == ""
    # CLI token refresh rewrote auth.json -> export prints the new blob.
    auth = work / "home" / AUTH_REL
    auth.write_text(
        '{"https://auth.x.ai::00000000-0000-0000-0000-000000000000":'
        '{"key":"REDACTED","refresh_token":"REDACTED_NEW"}}\n',
        encoding="utf-8",
    )
    out = run_runner(["export-credentials"], grok_env)
    assert out.returncode == 0
    exported = json.loads(out.stdout.strip())
    assert exported["provider"] == "grok"
    assert "REDACTED_NEW" in exported["files"][AUTH_REL]


# -- WP0 fake end-to-end (tests/fakes/fake_grok.py, WP0 fixture shape) ----


@pytest.fixture
def fake_env(work: Path, repo_root: Path) -> dict[str, str]:
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SBX_WORK": str(work),
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
        "GROK_BIN": str(FAKE_GROK),
        "FAKE_GROK_SCENARIO": "success",
        "SBX_BACKEND": "local",
    }


def test_fake_grok_success_and_resume(work: Path, fake_env: dict[str, str]) -> None:
    """The WP0 fake speaks the same ``grok -p/--resume`` surface well
    enough for runner e2e: ``-p`` scans as a flag (prompt positional),
    ``--resume`` marks the invocation a resume."""
    init_grok(fake_env)
    code1, doc1, events1 = turn(fake_env, work, 1)
    assert code1 == 0
    assert doc1["native_session_id"] == WP0_ID
    assert (work / "hello.txt").read_text() == "hello from fake_grok\n"
    items = [e["item"] for e in events1 if isinstance(e.get("item"), dict)]
    assert {i["type"] for i in items} >= {"reasoning", "command_execution", "agent_message"}

    fake_env["FAKE_GROK_SCENARIO"] = "resume"
    code2, doc2, _ = turn(fake_env, work, 2)
    assert code2 == 0
    assert doc2["native_session_id"] == WP0_ID
    assert "resumed by fake_grok" in (work / "hello.txt").read_text()


def test_fake_grok_auth_invalid_exits_5(work: Path, fake_env: dict[str, str]) -> None:
    init_grok(fake_env)
    fake_env["FAKE_GROK_SCENARIO"] = "auth_invalid"
    code, doc, _ = turn(fake_env, work, 1)
    assert code == 5
    assert doc["status"] == "auth_invalid"
    assert doc["health"] == "auth_invalid"
