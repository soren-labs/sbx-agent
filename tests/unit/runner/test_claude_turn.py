"""runner init/turn for provider=claude (SOR-97, Experimental seam).

End-to-end through ``tests/unit/runner/runner_claude.py`` — a test-only
``python -m runtime.runner`` shim that performs the SOR-96-style
registration in-process (``claude`` is not in the frozen provider set)
— with ``CLAUDE_BIN`` pointed at ``tests/unit/runner/replay_claude.py``,
which speaks the verified production argv contract (``claude -p <PROMPT>
--output-format stream-json [--model <M>] --verbose --permission-mode
bypassPermissions``; resume adds ``--resume <uuid>``) and replays the
staged captures under ``tests/unit/runner/fixtures/claude/``.

Known runner gap (documented in ``docs/reviews/SOR-97.md``): the real
CLI exits rc=0 on stream-level failures, so ``sbx.turn_finished.status``
stays ``success`` even though the canonical stream carries
``error``+``turn.failed`` — the stream is the truthful record. The
stale-resume path *does* fail correctly via the runner's replacement-id
check.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from tests.unit.runner.conftest import load_json, parsed_events

MODEL = "claude-sonnet-4.6"
SESS_ID = "f00dcafe-1234-4abc-8def-0000000000c1"
AUTH_ID = "6b382e55-a657-4719-9122-16cc955c205f"
REPLAYER = Path(__file__).resolve().parent / "replay_claude.py"
RUNNER = Path(__file__).resolve().parent / "runner_claude.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude"
CRED_REL = ".claude/.credentials.json"
CRED_JSON = (
    '{"claudeAiOauth":{"accessToken":"REDACTED","refreshToken":"REDACTED",'
    '"expiresAt":2000000000000,"subscriptionType":"claude_pro"}}\n'
)


def run_claude(
    args: list[str], env: dict[str, str], *, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture
def claude_env(work: Path, repo_root: Path) -> dict[str, str]:
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SBX_WORK": str(work),
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
        "CLAUDE_BIN": str(REPLAYER),
        "CLAUDE_REPLAY_FIXTURE": str(FIXTURES / "success.jsonl"),
        "SBX_BACKEND": "local",
    }


def init_claude(env: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
    args = ["init", "--provider", "claude", "--model", MODEL]
    if "account_id" in extra:
        args += ["--account-id", extra["account_id"]]
    result = run_claude(args, env)
    assert result.returncode == 0, result.stderr
    return result


def turn(
    env: dict[str, str], work: Path, n: int, *, max_seconds: int = 30, timeout: float = 30.0
) -> tuple[int, dict, list[dict]]:
    msg = work / f"msg{n}.md"
    msg.write_text(f"turn {n} prompt\n", encoding="utf-8")
    result = run_claude(
        ["turn", "--n", str(n), "--message-file", str(msg), "--max-seconds", str(max_seconds)],
        env,
        timeout=timeout,
    )
    turn_path = work / "turns" / f"{n}.json"
    doc = load_json(turn_path) if turn_path.is_file() else {}
    return result.returncode, doc, parsed_events(work)


def test_init_layout_and_session(work: Path, claude_env: dict[str, str]) -> None:
    init_claude(claude_env, account_id="acct-claude-1")
    session = load_json(work / "session.json")
    assert session["provider"] == "claude"
    assert session["account_id"] == "acct-claude-1"
    assert session["model"] == MODEL
    assert session["native_session_id"] is None
    assert (work / "events.jsonl").read_text() == ""
    # Codex-specific artefacts must not appear for other providers.
    assert not (work / ".codex" / "auth.json").exists()
    claude_dir = work / "home" / ".claude"
    assert claude_dir.is_dir()
    assert stat.S_IMODE(claude_dir.stat().st_mode) == 0o700


def test_init_restores_credential_blob(work: Path, claude_env: dict[str, str]) -> None:
    claude_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "claude", "files": {CRED_REL: CRED_JSON}}
    )
    init_claude(claude_env)
    cred = work / "home" / CRED_REL
    assert cred.is_file()
    assert cred.read_text(encoding="utf-8") == CRED_JSON
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600
    session = load_json(work / "session.json")
    assert session["credential_files"] == [CRED_REL]
    assert CRED_JSON not in (work / "events.jsonl").read_text(encoding="utf-8")


def test_init_rejects_provider_mismatch(work: Path, claude_env: dict[str, str]) -> None:
    claude_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "codex", "files": {".codex/auth.json": "{}"}}
    )
    result = run_claude(["init", "--provider", "claude", "--model", MODEL], claude_env)
    assert result.returncode != 0
    assert not (work / "home" / CRED_REL).exists()


def test_turn_success_canonical_events(work: Path, claude_env: dict[str, str]) -> None:
    init_claude(claude_env, account_id="acct-1")
    code, doc, events = turn(claude_env, work, 1)
    assert code == 0
    types = [e["type"] for e in events]
    assert types[0] == "sbx.session_meta"
    assert events[0]["provider"] == "claude"
    assert events[0]["model"] == MODEL
    assert types[1] == "sbx.turn_started"
    assert "thread.started" in types
    assert "turn.started" in types
    assert "turn.completed" in types
    started = [e for e in events if e["type"] == "thread.started"]
    assert started[0]["thread_id"] == SESS_ID
    assert types[-1] == "sbx.turn_finished"
    assert events[-1]["status"] == "success"

    items = [e["item"] for e in events if isinstance(e.get("item"), dict)]
    kinds = {i["type"] for i in items}
    assert {"reasoning", "command_execution", "file_change", "agent_message"} <= kinds
    messages = [i for i in items if i["type"] == "agent_message"]
    assert messages[-1]["text"] == "Created marker.txt in the workspace."

    assert doc["status"] == "success"
    assert doc["native_session_id"] == SESS_ID
    assert doc["message"] == "Created marker.txt in the workspace."
    assert doc["usage"]["input_tokens"] == 11200
    assert doc["usage"]["cached_input_tokens"] == 2000
    assert doc["usage"]["cache_write_input_tokens"] == 500
    assert doc["usage"]["reasoning_output_tokens"] == 10
    session = load_json(work / "session.json")
    assert session["native_session_id"] == SESS_ID
    assert session["turn"] == 1

    # Native NDJSON lands in events.raw.jsonl, canonical in events.jsonl.
    raw_lines = (work / "events.raw.jsonl").read_text().splitlines()
    assert any('"type": "system"' in line for line in raw_lines)
    assert not any('"sbx.turn_started"' in line for line in raw_lines)


def test_argv_exact_and_stdin_devnull(work: Path, claude_env: dict[str, str]) -> None:
    spy_out = work / "spy.json"
    claude_env["CLAUDE_REPLAY_SPY_OUT"] = str(spy_out)
    init_claude(claude_env)
    code, _, _ = turn(claude_env, work, 1)
    assert code == 0
    spy = json.loads(spy_out.read_text())
    assert spy["argv"] == [
        "-p",
        "turn 1 prompt\n",
        "--output-format",
        "stream-json",
        "--model",
        MODEL,
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]
    assert spy["stdin_target"] == "/dev/null"
    assert spy["stdin_isatty"] is False

    claude_env["CLAUDE_REPLAY_FIXTURE"] = str(FIXTURES / "resume.jsonl")
    code2, _, _ = turn(claude_env, work, 2)
    assert code2 == 0
    spy2 = json.loads(spy_out.read_text())
    assert spy2["argv"] == [
        "-p",
        "turn 2 prompt\n",
        "--resume",
        SESS_ID,
        "--output-format",
        "stream-json",
        "--model",
        MODEL,
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]


def test_credential_env_not_forwarded_to_cli(work: Path, claude_env: dict[str, str]) -> None:
    """The CLI subprocess never sees the injected credential blob."""
    spy_out = work / "spy.json"
    claude_env.update(
        {
            "CLAUDE_REPLAY_SPY_OUT": str(spy_out),
            "SBX_ACCOUNT_CREDENTIAL": json.dumps(
                {"provider": "claude", "files": {CRED_REL: CRED_JSON}}
            ),
            "CODEX_AUTH_JSON": json.dumps({"tokens": {"access_token": "REDACTED"}}),
        }
    )
    init_claude(claude_env)
    code, _, events = turn(claude_env, work, 1)
    assert code == 0
    spy = json.loads(spy_out.read_text())
    assert spy["has_SBX_ACCOUNT_CREDENTIAL"] is False
    assert spy["has_CODEX_AUTH_JSON"] is False
    raw = (work / "events.jsonl").read_text(encoding="utf-8")
    assert CRED_JSON not in raw


def test_resume_turn_keeps_session(work: Path, claude_env: dict[str, str]) -> None:
    init_claude(claude_env)
    code1, doc1, _ = turn(claude_env, work, 1)
    assert code1 == 0
    claude_env["CLAUDE_REPLAY_FIXTURE"] = str(FIXTURES / "resume.jsonl")
    code2, doc2, events2 = turn(claude_env, work, 2)
    assert code2 == 0
    assert doc2["native_session_id"] == doc1["native_session_id"] == SESS_ID
    types2 = [e["type"] for e in events2]
    assert types2.count("sbx.session_meta") == 1  # emitted once, on turn 1
    assert types2.count("sbx.turn_started") == 2
    session = load_json(work / "session.json")
    assert session["native_session_id"] == SESS_ID


def test_stale_resume_fails_via_replacement_check(work: Path, claude_env: dict[str, str]) -> None:
    """Real stale shape: error result, no thread.started -> the runner's
    stale-resume check fails the turn (exit 2) and keeps the old id."""
    init_claude(claude_env)
    code1, doc1, _ = turn(claude_env, work, 1)
    assert code1 == 0
    assert doc1["native_session_id"] == SESS_ID
    claude_env["CLAUDE_REPLAY_STALE"] = "1"
    code2, doc2, events2 = turn(claude_env, work, 2)
    assert code2 == 2
    assert doc2["status"] == "codex_error"
    failed = [e for e in events2 if e["type"] == "turn.failed"]
    assert failed and "No conversation found" in failed[-1]["error"]["message"]
    started2 = [e for e in events2 if e["type"] == "thread.started"]
    assert {e["thread_id"] for e in started2} == {SESS_ID}
    session = load_json(work / "session.json")
    assert session["native_session_id"] == SESS_ID
    stderr_tail = (work / "turns" / "2.stderr").read_text(encoding="utf-8")
    assert "No conversation found" in stderr_tail


def test_auth_invalid_stream_fails_event_rc0_status_gap(
    work: Path, claude_env: dict[str, str]
) -> None:
    """Real no-auth shape (rc=0): the canonical stream carries
    error+turn.failed, but ``sbx.turn_finished.status`` stays ``success``
    — the rc-derived runner gap documented in the SOR-97 note."""
    init_claude(claude_env)
    claude_env["CLAUDE_REPLAY_FIXTURE"] = str(FIXTURES / "auth_invalid.jsonl")
    code, doc, events = turn(claude_env, work, 1)
    assert code == 0  # masked by the real CLI's exit code
    types = [e["type"] for e in events]
    assert "error" in types
    assert "turn.failed" in types
    errors = [e for e in events if e["type"] == "error"]
    assert any("Not logged in" in e["message"] for e in errors)
    finished = [e for e in events if e["type"] == "sbx.turn_finished"]
    # rc-derived finish status: truthful record is the canonical stream.
    assert finished[-1]["status"] == "success"
    assert doc["native_session_id"] == AUTH_ID


def test_badjson_exits_4(work: Path, claude_env: dict[str, str]) -> None:
    init_claude(claude_env)
    claude_env["CLAUDE_REPLAY_FIXTURE"] = str(FIXTURES / "badjson.jsonl")
    code, doc, events = turn(claude_env, work, 1)
    assert code == 4
    assert doc["status"] == "bad_json"
    assert doc["bad_json_lines"] >= 1
    assert any(e["type"] == "sbx.error" for e in events)
    # Stream continues after the bad line.
    assert any(e["type"] == "turn.completed" for e in events)


def test_export_credentials_roundtrip(work: Path, claude_env: dict[str, str]) -> None:
    claude_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "claude", "files": {CRED_REL: CRED_JSON}}
    )
    init_claude(claude_env)
    same = run_claude(["export-credentials"], claude_env)
    assert same.returncode == 0
    assert same.stdout.strip() == ""
    # CLI token refresh rewrote .credentials.json -> export prints the blob.
    cred = work / "home" / CRED_REL
    cred.write_text(
        '{"claudeAiOauth":{"accessToken":"REDACTED_NEW","refreshToken":"REDACTED"}}\n',
        encoding="utf-8",
    )
    out = run_claude(["export-credentials"], claude_env)
    assert out.returncode == 0
    exported = json.loads(out.stdout.strip())
    assert exported["provider"] == "claude"
    assert "REDACTED_NEW" in exported["files"][CRED_REL]
