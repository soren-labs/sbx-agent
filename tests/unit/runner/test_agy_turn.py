"""runner init/turn/export-credentials for provider=antigravity (SOR-62).

End-to-end through ``python -m runtime.runner`` with ``AGY_BIN`` pointed at
``tests/unit/runner/replay_agy.py``, which replays the staged real SOR-60
captures (``tests/unit/runner/fixtures/antigravity/*.jsonl``). Covers argv shape,
stdin closed, credential blob lifecycle, stale-resume detection, health →
exit-code mapping, and events.raw.jsonl / events.jsonl separation.
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

MODEL = "gemini-3.8-flash-low"
CONV_ID = "4e9eadd6-eb70-442b-b24a-1660db079181"
REPLAYER = Path(__file__).resolve().parent / "replay_agy.py"
REAL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "antigravity"
WP0_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "events" / "antigravity"
TOKEN_REL = ".gemini/antigravity-cli/antigravity-oauth-token"
TOKEN_JSON = '{"auth_method":"consumer","id_token":"REDACTED","token":"REDACTED"}\n'

SPIKE_ARGV_FLAGS = ["--dangerously-skip-permissions", "--disable-slash-commands"]


@pytest.fixture
def agy_env(work: Path, repo_root: Path) -> dict[str, str]:
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SBX_WORK": str(work),
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
        "AGY_BIN": str(REPLAYER),
        "AGY_REPLAY_FIXTURE": str(REAL_FIXTURES / "success.jsonl"),
        "SBX_BACKEND": "local",
    }


def init_agy(env: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
    args = ["init", "--provider", "antigravity", "--model", MODEL]
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


def test_init_layout_and_session(work: Path, agy_env: dict[str, str]) -> None:
    init_agy(agy_env, account_id="acct-agy-1")
    session = load_json(work / "session.json")
    assert session["provider"] == "antigravity"
    assert session["account_id"] == "acct-agy-1"
    assert session["model"] == MODEL
    assert session["native_session_id"] is None
    assert (work / "events.jsonl").read_text() == ""
    assert (work / "events.raw.jsonl").read_text() == ""
    # Codex-specific artefacts must not appear for other providers.
    assert not (work / ".codex" / "auth.json").exists()


def test_init_restores_token_blob(work: Path, agy_env: dict[str, str]) -> None:
    agy_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "antigravity", "files": {TOKEN_REL: TOKEN_JSON}}
    )
    init_agy(agy_env)
    token = work / "home" / TOKEN_REL
    assert token.is_file()
    assert token.read_text(encoding="utf-8") == TOKEN_JSON
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    session = load_json(work / "session.json")
    assert session["credential_files"] == [TOKEN_REL]
    # The blob value must not leak into events or session files.
    assert TOKEN_JSON not in (work / "events.jsonl").read_text(encoding="utf-8")
    assert "SBX_ACCOUNT_CREDENTIAL" not in (work / "session.json").read_text(encoding="utf-8")


def test_init_restored_token_only_runs_canonical_probe(
    work: Path, agy_env: dict[str, str], repo_root: Path
) -> None:
    """SOR-258 regression: fresh HOME + token-only blob → ``agy models`` passes.

    The canonical auth/model probe once failed on a token-only restore
    ("account not eligible") because the CLI's onboarding marker was
    missing. ``runner init`` now reconstructs the minimal completed state
    during ``prepare_home``, so an SBX-restored account behaves like a
    freshly logged-in one.
    """
    from control.onboarding import provider_auth_argv

    fake = repo_root / "tests" / "fakes" / "fake_agy.py"
    agy_env["AGY_BIN"] = str(fake)
    agy_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "antigravity", "files": {TOKEN_REL: TOKEN_JSON}}
    )
    init_agy(agy_env)

    home = work / "home"
    state = json.loads(
        (home / ".gemini/antigravity-cli/cache/onboarding.json").read_text(encoding="utf-8")
    )
    assert state["onboardingComplete"] is True
    # The probe runs against the restored HOME only — no SBX_* env.
    probe = subprocess.run(
        provider_auth_argv("antigravity", env={"AGY_BIN": str(fake)}),
        env={"PATH": agy_env["PATH"], "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert probe.returncode == 0, probe.stderr


def test_canonical_probe_fails_on_raw_token_only_home(
    work: Path, agy_env: dict[str, str], repo_root: Path
) -> None:
    """The pre-fix failure stays modeled: token restored without the
    onboarding marker (and without ``prepare_home``) hits the eligibility
    gate exactly like the real CLI on a bare token-only HOME."""
    from control.onboarding import provider_auth_argv

    fake = repo_root / "tests" / "fakes" / "fake_agy.py"
    home = work / "home"
    token = home / TOKEN_REL
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(TOKEN_JSON, encoding="utf-8")
    probe = subprocess.run(
        provider_auth_argv("antigravity", env={"AGY_BIN": str(fake)}),
        env={"PATH": agy_env["PATH"], "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert probe.returncode == 1
    assert "not eligible" in probe.stderr


def test_blob_carried_onboarding_state_restored_verbatim(
    work: Path, agy_env: dict[str, str]
) -> None:
    """A bundle that already carries the marker wins over synthesis — the
    file is recorded for export and written byte-for-byte."""
    onboarding_rel = ".gemini/antigravity-cli/cache/onboarding.json"
    state_json = '{"consumerOnboardingComplete": false, "onboardingComplete": true}\n'
    agy_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {
            "provider": "antigravity",
            "files": {TOKEN_REL: TOKEN_JSON, onboarding_rel: state_json},
        }
    )
    init_agy(agy_env)
    restored = work / "home" / onboarding_rel
    assert restored.read_text(encoding="utf-8") == state_json
    session = load_json(work / "session.json")
    assert set(session["credential_files"]) == {TOKEN_REL, onboarding_rel}


def test_init_rejects_provider_mismatch(work: Path, agy_env: dict[str, str]) -> None:
    agy_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "codex", "files": {".codex/auth.json": "{}"}}
    )
    result = run_runner(["init", "--provider", "antigravity", "--model", MODEL], agy_env)
    assert result.returncode != 0
    assert not (work / "home" / TOKEN_REL).exists()


def test_turn_success_canonical_events(work: Path, agy_env: dict[str, str]) -> None:
    init_agy(agy_env, account_id="acct-1")
    code, doc, events = turn(agy_env, work, 1)
    assert code == 0
    types = [e["type"] for e in events]
    assert types[0] == "sbx.session_meta"
    assert events[0]["provider"] == "antigravity"
    assert events[0]["model"] == MODEL
    assert events[0]["account_id"] == "acct-1"
    assert types[1] == "sbx.turn_started"
    assert "thread.started" in types
    started = [e for e in events if e["type"] == "thread.started"]
    assert started[0]["thread_id"] == CONV_ID
    assert "turn.started" in types
    assert "item.started" in types
    assert "item.completed" in types
    assert "turn.completed" in types
    assert types[-1] == "sbx.turn_finished"
    assert events[-1]["status"] == "success"
    assert events[-1]["exit_code"] == 0

    items = [e["item"] for e in events if isinstance(e.get("item"), dict)]
    kinds = {i["type"] for i in items}
    assert {"command_execution", "file_change", "agent_message"} <= kinds
    messages = [i for i in items if i["type"] == "agent_message"]
    assert messages[-1]["text"] == "DONE\n"

    assert doc["status"] == "success"
    assert doc["native_session_id"] == CONV_ID
    assert doc["codex_session_id"] == CONV_ID
    assert doc["message"] == "DONE\n"
    assert doc["usage"]["input_tokens"] == 26610
    assert doc["usage"]["cached_input_tokens"] == 24406
    assert doc["usage"]["reasoning_output_tokens"] == 258
    session = load_json(work / "session.json")
    assert session["native_session_id"] == CONV_ID
    assert session["turn"] == 1

    # Native NDJSON lands in events.raw.jsonl, canonical in events.jsonl.
    raw_lines = (work / "events.raw.jsonl").read_text().splitlines()
    assert any('"event": "init"' in line for line in raw_lines)
    assert not any('"sbx.turn_started"' in line for line in raw_lines)
    assert (work / "turns" / "1.stderr").is_file()


def test_argv_exact_and_stdin_devnull(work: Path, agy_env: dict[str, str]) -> None:
    spy_out = work / "spy.json"
    agy_env["AGY_REPLAY_SPY_OUT"] = str(spy_out)
    init_agy(agy_env)
    code, _, _ = turn(agy_env, work, 1)
    assert code == 0
    spy = json.loads(spy_out.read_text())
    assert spy["argv"] == [
        "-p",
        "turn 1 prompt\n",
        "--output-format",
        "stream-json",
        "--model",
        MODEL,
        *SPIKE_ARGV_FLAGS,
    ]
    assert spy["stdin_target"] == "/dev/null"
    assert spy["stdin_isatty"] is False

    agy_env["AGY_REPLAY_FIXTURE"] = str(REAL_FIXTURES / "resume.jsonl")
    code2, _, _ = turn(agy_env, work, 2)
    assert code2 == 0
    spy2 = json.loads(spy_out.read_text())
    assert spy2["argv"] == [
        "-p",
        "turn 2 prompt\n",
        "--conversation",
        CONV_ID,
        "--output-format",
        "stream-json",
        "--model",
        MODEL,
        *SPIKE_ARGV_FLAGS,
    ]


def test_credential_env_not_forwarded_to_cli(work: Path, agy_env: dict[str, str]) -> None:
    spy_out = work / "spy.json"
    agy_env.update(
        {
            "AGY_REPLAY_SPY_OUT": str(spy_out),
            "SBX_ACCOUNT_CREDENTIAL": json.dumps(
                {"provider": "antigravity", "files": {TOKEN_REL: TOKEN_JSON}}
            ),
            "CODEX_AUTH_JSON": json.dumps({"tokens": {"access_token": "REDACTED"}}),
        }
    )
    init_agy(agy_env)
    code, _, events = turn(agy_env, work, 1)
    assert code == 0
    spy = json.loads(spy_out.read_text())
    assert spy["has_SBX_ACCOUNT_CREDENTIAL"] is False
    assert spy["has_CODEX_AUTH_JSON"] is False
    raw = (work / "events.jsonl").read_text(encoding="utf-8")
    assert TOKEN_JSON not in raw
    assert "REDACTED" not in raw


def test_resume_turn_keeps_conversation(work: Path, agy_env: dict[str, str]) -> None:
    init_agy(agy_env)
    code1, doc1, _ = turn(agy_env, work, 1)
    assert code1 == 0
    agy_env["AGY_REPLAY_FIXTURE"] = str(REAL_FIXTURES / "resume.jsonl")
    code2, doc2, events2 = turn(agy_env, work, 2)
    assert code2 == 0
    assert doc2["native_session_id"] == doc1["native_session_id"] == CONV_ID
    types2 = [e["type"] for e in events2]
    assert types2.count("sbx.session_meta") == 1  # emitted once, on turn 1
    assert types2.count("sbx.turn_started") == 2
    session = load_json(work / "session.json")
    assert session["native_session_id"] == CONV_ID


def test_stale_resume_fails_without_forking(work: Path, agy_env: dict[str, str]) -> None:
    """Stale --conversation id: CLI exits 0 with a NEW conversation id; the
    turn must fail and session.json must keep the requested id."""
    init_agy(agy_env)
    code1, doc1, _ = turn(agy_env, work, 1)
    assert code1 == 0
    assert doc1["native_session_id"] == CONV_ID
    agy_env.update(
        {
            "AGY_REPLAY_FIXTURE": str(REAL_FIXTURES / "success.jsonl"),
            "AGY_REPLAY_STALE": "1",
            "AGY_REPLAY_SESSION_ID": "00000000-0000-0000-0000-000000000000",
        }
    )
    code2, doc2, events2 = turn(agy_env, work, 2)
    assert code2 == 2
    assert doc2["status"] == "codex_error"
    assert doc2["native_session_id"] == CONV_ID
    errors = [e for e in events2 if e["type"] == "error"]
    assert errors and "not found" in errors[0]["message"]
    assert any(e["type"] == "turn.failed" for e in events2)
    finished = [e for e in events2 if e["type"] == "sbx.turn_finished"]
    assert finished[-1]["status"] == "codex_error"
    session = load_json(work / "session.json")
    assert session["native_session_id"] == CONV_ID  # never adopts the new id


def test_nonzero_exits_2(work: Path, agy_env: dict[str, str]) -> None:
    init_agy(agy_env)
    agy_env["AGY_REPLAY_FIXTURE"] = str(REAL_FIXTURES / "nonzero.jsonl")
    agy_env["AGY_REPLAY_RC"] = "1"
    code, doc, events = turn(agy_env, work, 1)
    assert code == 2
    assert doc["status"] == "codex_error"
    failed = [e for e in events if e["type"] == "turn.failed"]
    assert failed and failed[-1]["error"]["message"].startswith("invalid model selection")


def test_auth_invalid_exits_5(work: Path, agy_env: dict[str, str]) -> None:
    init_agy(agy_env)
    agy_env["AGY_REPLAY_FIXTURE"] = str(REAL_FIXTURES / "auth_invalid.jsonl")
    agy_env["AGY_REPLAY_RC"] = "1"
    agy_env["AGY_REPLAY_STDERR"] = "Authentication required. Please visit the URL to log in:"
    code, doc, events = turn(agy_env, work, 1)
    assert code == 5
    assert doc["status"] == "auth_invalid"
    assert doc["health"] == "auth_invalid"
    finished = [e for e in events if e["type"] == "sbx.turn_finished"]
    assert finished[-1]["status"] == "auth_invalid"


def test_badjson_exits_4(work: Path, agy_env: dict[str, str]) -> None:
    init_agy(agy_env)
    agy_env["AGY_REPLAY_FIXTURE"] = str(WP0_FIXTURES / "badjson.jsonl")
    code, doc, events = turn(agy_env, work, 1)
    assert code == 4
    assert doc["status"] == "bad_json"
    assert doc["bad_json_lines"] >= 1
    assert any(e["type"] == "sbx.error" for e in events)
    # Stream continues after the bad line.
    assert any(e["type"] == "turn.completed" for e in events)


def test_slow_completes(work: Path, agy_env: dict[str, str]) -> None:
    init_agy(agy_env)
    agy_env["AGY_REPLAY_PAUSE_S"] = "0.5"
    started = time.monotonic()
    code, doc, _ = turn(agy_env, work, 1, max_seconds=30)
    assert code == 0
    assert time.monotonic() - started >= 0.5
    assert doc["status"] == "success"


def test_hang_times_out(work: Path, agy_env: dict[str, str]) -> None:
    init_agy(agy_env)
    agy_env["AGY_REPLAY_HANG"] = "1"
    code, doc, events = turn(agy_env, work, 1, max_seconds=1, timeout=60.0)
    assert code == 3
    assert doc["status"] == "timeout"
    assert doc["native_session_id"] == CONV_ID  # thread seen before hang
    finished = [e for e in events if e["type"] == "sbx.turn_finished"]
    assert finished[-1]["status"] == "timeout"


def test_export_credentials_roundtrip(work: Path, agy_env: dict[str, str]) -> None:
    agy_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "antigravity", "files": {TOKEN_REL: TOKEN_JSON}}
    )
    init_agy(agy_env)
    same = run_runner(["export-credentials"], agy_env)
    assert same.returncode == 0
    assert same.stdout.strip() == ""
    # CLI refresh rewrote the token file -> export prints the new blob.
    token = work / "home" / TOKEN_REL
    token.write_text('{"auth_method":"consumer","id_token":"REDACTED_NEW"}\n', encoding="utf-8")
    out = run_runner(["export-credentials"], agy_env)
    assert out.returncode == 0
    exported = json.loads(out.stdout.strip())
    assert exported["provider"] == "antigravity"
    assert exported["files"][TOKEN_REL] == '{"auth_method":"consumer","id_token":"REDACTED_NEW"}\n'
