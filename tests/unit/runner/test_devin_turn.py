"""runner init/turn/export-credentials for provider=devin (SOR-72).

End-to-end through ``python -m runtime.runner`` with
``SBX_DEVIN_TRANSPORT=cli`` and ``DEVIN_BIN=tests/fakes/fake_devin.py``.
Covers the seven shared scenarios, the credential blob lifecycle, and
events.raw.jsonl / events.jsonl separation.
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

MODEL = "swe-2-medium"
DEVIN_SESSION = "devin-session-01a09b11"


@pytest.fixture
def devin_env(work: Path, repo_root: Path) -> dict[str, str]:
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SBX_WORK": str(work),
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
        "SBX_DEVIN_TRANSPORT": "cli",
        "DEVIN_BIN": str(repo_root / "tests" / "fakes" / "fake_devin.py"),
        "FAKE_DEVIN_SCENARIO": "success",
        "SBX_BACKEND": "local",
    }


def init_devin(env: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
    args = ["init", "--provider", "devin", "--model", MODEL]
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


def test_init_devin_layout_and_session(work: Path, devin_env: dict[str, str]) -> None:
    init_devin(devin_env, account_id="acct-9")
    session = load_json(work / "session.json")
    assert session["provider"] == "devin"
    assert session["account_id"] == "acct-9"
    assert session["native_session_id"] is None
    assert session["codex_session_id"] is None
    assert session["model"] == MODEL
    cfg = load_json(work / "home" / ".config" / "devin" / "config.json")
    assert cfg["agent"]["model"] == MODEL
    assert (work / "events.jsonl").read_text() == ""
    assert (work / "events.raw.jsonl").read_text() == ""
    # Codex-specific artefacts must not appear for other providers.
    assert not (work / ".codex" / "auth.json").exists()


def test_init_restores_credential_blob(work: Path, devin_env: dict[str, str]) -> None:
    blob = {
        "provider": "devin",
        "files": {".local/share/devin/credentials.toml": 'windsurf_api_key = "REDACTED"\n'},
    }
    devin_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(blob)
    init_devin(devin_env)
    creds = work / "home" / ".local" / "share" / "devin" / "credentials.toml"
    assert creds.is_file()
    assert creds.read_text(encoding="utf-8") == 'windsurf_api_key = "REDACTED"\n'
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    session = load_json(work / "session.json")
    assert session["credential_files"] == [".local/share/devin/credentials.toml"]


def test_init_rejects_provider_mismatch(work: Path, devin_env: dict[str, str]) -> None:
    devin_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
        {"provider": "codex", "files": {".codex/auth.json": "{}"}}
    )
    result = run_runner(["init", "--provider", "devin", "--model", MODEL], devin_env)
    assert result.returncode != 0


def test_export_credentials_roundtrip(work: Path, devin_env: dict[str, str]) -> None:
    blob = {
        "provider": "devin",
        "files": {".local/share/devin/credentials.toml": 'windsurf_api_key = "REDACTED"\n'},
    }
    devin_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(blob)
    init_devin(devin_env)
    same = run_runner(["export-credentials"], devin_env)
    assert same.returncode == 0
    assert same.stdout.strip() == ""
    # Refresh the on-disk credential -> export prints the new blob.
    creds = work / "home" / ".local" / "share" / "devin" / "credentials.toml"
    creds.write_text('windsurf_api_key = "REDACTED_NEW"\n', encoding="utf-8")
    out = run_runner(["export-credentials"], devin_env)
    assert out.returncode == 0
    exported = json.loads(out.stdout.strip())
    assert exported["provider"] == "devin"
    assert exported["files"][".local/share/devin/credentials.toml"] == (
        'windsurf_api_key = "REDACTED_NEW"\n'
    )


def test_success_turn_canonical_events(work: Path, devin_env: dict[str, str]) -> None:
    init_devin(devin_env, account_id="acct-1")
    code, doc, events = turn(devin_env, work, 1)
    assert code == 0
    types = [e["type"] for e in events]
    assert types[0] == "sbx.session_meta"
    assert events[0]["provider"] == "devin"
    assert events[0]["model"] == MODEL
    assert events[0]["account_id"] == "acct-1"
    assert types[1] == "sbx.turn_started"
    assert "thread.started" in types
    assert "item.completed" in types
    assert types[-1] == "sbx.turn_finished"
    assert events[-1]["status"] == "success"
    assert doc["status"] == "success"
    assert doc["native_session_id"] == DEVIN_SESSION
    assert doc["codex_session_id"] == DEVIN_SESSION
    assert doc["message"]
    assert doc["usage"]["cached_input_tokens"] == 3100
    assert doc["usage"]["reasoning_output_tokens"] == 60
    session = load_json(work / "session.json")
    assert session["native_session_id"] == DEVIN_SESSION
    assert session["turn"] == 1
    # Native NDJSON lands in events.raw.jsonl, canonical in events.jsonl.
    raw_lines = (work / "events.raw.jsonl").read_text().splitlines()
    assert any('"session.started"' in line for line in raw_lines)
    assert not any('"sbx.turn_started"' in line for line in raw_lines)
    assert (work / "hello.txt").read_text() == "hello from fake_devin\n"


def test_resume_turn_keeps_session(work: Path, devin_env: dict[str, str]) -> None:
    init_devin(devin_env)
    code1, doc1, _ = turn(devin_env, work, 1)
    assert code1 == 0
    devin_env["FAKE_DEVIN_SCENARIO"] = "resume"
    code2, doc2, events2 = turn(devin_env, work, 2)
    assert code2 == 0
    assert doc2["native_session_id"] == doc1["native_session_id"] == DEVIN_SESSION
    text = (work / "hello.txt").read_text()
    assert "resumed by fake_devin" in text
    types2 = [e["type"] for e in events2]
    # events.jsonl spans both turns; session_meta is emitted once (turn 1).
    assert types2.count("sbx.session_meta") == 1
    assert types2.count("sbx.turn_started") == 2


def test_nonzero_exits_2(work: Path, devin_env: dict[str, str]) -> None:
    init_devin(devin_env)
    devin_env["FAKE_DEVIN_SCENARIO"] = "nonzero"
    code, doc, events = turn(devin_env, work, 1)
    assert code == 2
    assert doc["status"] == "codex_error"
    assert any(e["type"] == "turn.failed" for e in events)


def test_auth_invalid_exits_5(work: Path, devin_env: dict[str, str]) -> None:
    init_devin(devin_env)
    devin_env["FAKE_DEVIN_SCENARIO"] = "auth_invalid"
    code, doc, events = turn(devin_env, work, 1)
    assert code == 5
    assert doc["status"] == "auth_invalid"
    assert doc["health"] == "auth_invalid"
    finished = [e for e in events if e["type"] == "sbx.turn_finished"]
    assert finished[-1]["status"] == "auth_invalid"


def test_badjson_exits_4(work: Path, devin_env: dict[str, str]) -> None:
    init_devin(devin_env)
    devin_env["FAKE_DEVIN_SCENARIO"] = "badjson"
    code, doc, events = turn(devin_env, work, 1)
    assert code == 4
    assert doc["status"] == "bad_json"
    assert doc["bad_json_lines"] >= 1
    assert any(e["type"] == "sbx.error" for e in events)
    # Stream continues after the bad line.
    assert any(e["type"] == "turn.completed" for e in events)


def test_slow_completes(work: Path, devin_env: dict[str, str]) -> None:
    init_devin(devin_env)
    devin_env["FAKE_DEVIN_SCENARIO"] = "slow"
    devin_env["FAKE_DEVIN_SLOW_SECONDS"] = "0.5"
    started = time.monotonic()
    code, doc, _ = turn(devin_env, work, 1, max_seconds=30)
    assert code == 0
    assert time.monotonic() - started >= 0.5
    assert doc["status"] == "success"


def test_hang_times_out(work: Path, devin_env: dict[str, str]) -> None:
    init_devin(devin_env)
    devin_env["FAKE_DEVIN_SCENARIO"] = "hang"
    code, doc, events = turn(devin_env, work, 1, max_seconds=2, timeout=60.0)
    assert code == 3
    assert doc["status"] == "timeout"
    assert doc["native_session_id"] == DEVIN_SESSION  # thread seen before hang
    finished = [e for e in events if e["type"] == "sbx.turn_finished"]
    assert finished[-1]["status"] == "timeout"


def test_credential_env_not_forwarded_to_cli(
    work: Path, devin_env: dict[str, str], repo_root: Path
) -> None:
    """The CLI subprocess never sees SBX_ACCOUNT_CREDENTIAL / ACP_BACKEND / DEVIN_*."""
    spy = work / "devin_spy.py"
    spy.write_text(
        "import json, os, sys, runpy\n"
        "from pathlib import Path\n"
        "Path(os.environ['SPY_OUT']).write_text(json.dumps({\n"
        "    'SBX_ACCOUNT_CREDENTIAL': 'SBX_ACCOUNT_CREDENTIAL' in os.environ,\n"
        "    'ACP_BACKEND': 'ACP_BACKEND' in os.environ,\n"
        "    'DEVIN_API_KEY': 'DEVIN_API_KEY' in os.environ,\n"
        "    'DEVIN_V3_API_KEY': 'DEVIN_V3_API_KEY' in os.environ,\n"
        "    'DEVIN_LEGACY_API_KEY': 'DEVIN_LEGACY_API_KEY' in os.environ,\n"
        "    'DEVIN_ORG_ID': 'DEVIN_ORG_ID' in os.environ,\n"
        "}))\n"
        "sys.argv[0] = os.environ['DEVIN_FAKE']\n"
        "sys.path.insert(0, os.path.dirname(sys.argv[0]))\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n",
        encoding="utf-8",
    )
    spy_out = work / "spy.json"
    blob = {
        "provider": "devin",
        "files": {".local/share/devin/credentials.toml": 'windsurf_api_key = "REDACTED"\n'},
    }
    devin_env.update(
        {
            "SBX_ACCOUNT_CREDENTIAL": json.dumps(blob),
            "DEVIN_BIN": str(spy),
            "DEVIN_FAKE": str(repo_root / "tests" / "fakes" / "fake_devin.py"),
            "SPY_OUT": str(spy_out),
            "ACP_BACKEND": "windsurf",
            "DEVIN_API_KEY": "sentinel-key",
            "DEVIN_V3_API_KEY": "sentinel-v3",
            "DEVIN_LEGACY_API_KEY": "sentinel-legacy",
            "DEVIN_ORG_ID": "sentinel-org",
        }
    )
    init_devin(devin_env)
    code, _doc, events = turn(devin_env, work, 1)
    assert code == 0
    seen = json.loads(spy_out.read_text())
    assert seen == {
        "SBX_ACCOUNT_CREDENTIAL": False,
        "ACP_BACKEND": False,
        "DEVIN_API_KEY": False,
        "DEVIN_V3_API_KEY": False,
        "DEVIN_LEGACY_API_KEY": False,
        "DEVIN_ORG_ID": False,
    }
    # And no credential material leaks into the event stream.
    raw = (work / "events.jsonl").read_text(encoding="utf-8")
    assert "sentinel" not in raw
    assert "REDACTED" not in raw or "windsurf_api_key" not in raw


def test_output_contract_devin_path(work: Path, devin_env: dict[str, str]) -> None:
    """SOR-130: the contract path is provider-neutral — Devin's ``-p``
    transport gets the same instruction + normalized verdict as Codex."""
    contract = work / "_contract_1.json"
    contract.write_text(
        json.dumps(
            {
                "schema": {
                    "type": "object",
                    "required": ["summary", "ok"],
                    "properties": {
                        "summary": {"type": "string"},
                        "ok": {"type": "boolean"},
                        "files": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "enforcement": "strict",
            }
        ),
        encoding="utf-8",
    )
    init_devin(devin_env)
    devin_env["FAKE_DEVIN_SCENARIO"] = "structured"
    msg = work / "msg1.md"
    msg.write_text("summarize your work\n", encoding="utf-8")
    result = run_runner(
        ["turn", "--n", "1", "--message-file", str(msg), "--output-contract", str(contract)],
        devin_env,
    )
    assert result.returncode == 0
    doc = load_json(work / "turns" / "1.json")
    assert doc["status"] == "success"
    assert doc["native_session_id"] == DEVIN_SESSION
    assert doc["structured_output"] == {
        "summary": "created hello.txt",
        "ok": True,
        "files": ["hello.txt"],
    }
    verdict = doc["output_contract"]
    assert verdict["status"] == "valid"
    assert verdict["extraction"] == "raw"
    assert verdict["violations"] == []
    # inbox keeps the user text; the instruction only rides the provider argv.
    assert (work / "inbox" / "1.md").read_text(encoding="utf-8") == "summarize your work\n"


def test_output_contract_instruction_reaches_devin_argv(
    work: Path, devin_env: dict[str, str]
) -> None:
    """The contract instruction is appended to the ``-p`` prompt for Devin."""
    argv_file = work / "argv.json"
    recorder = work / "argv_recorder.py"
    recorder.write_text(
        "import json, os, sys, runpy\n"
        "from pathlib import Path\n"
        "Path(os.environ['ARGV_OUT']).write_text(json.dumps(sys.argv[1:]))\n"
        "sys.argv[0] = os.environ['DEVIN_FAKE']\n"
        "sys.path.insert(0, os.path.dirname(sys.argv[0]))\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n",
        encoding="utf-8",
    )
    contract = work / "_contract_1.json"
    contract.write_text(
        json.dumps({"schema": {"type": "object", "required": ["ok"]}}),
        encoding="utf-8",
    )
    devin_env.update(
        {
            "DEVIN_BIN": str(recorder),
            "DEVIN_FAKE": str(Path(__file__).resolve().parents[2] / "fakes" / "fake_devin.py"),
            "ARGV_OUT": str(argv_file),
            "FAKE_DEVIN_SCENARIO": "structured",
        }
    )
    init_devin(devin_env)
    msg = work / "msg1.md"
    msg.write_text("summarize\n", encoding="utf-8")
    result = run_runner(
        ["turn", "--n", "1", "--message-file", str(msg), "--output-contract", str(contract)],
        devin_env,
    )
    assert result.returncode == 0
    argv = json.loads(argv_file.read_text())
    assert argv[0] == "-p"
    prompt = argv[1]
    assert prompt.startswith("summarize\n")
    assert "[output-contract]" in prompt
    assert '"required":["ok"]' in prompt


def test_cli_transport_argv_is_devin_p(work: Path, devin_env: dict[str, str]) -> None:
    """cli transport uses the fake's ``-p`` / ``--resume`` argv contract."""
    argv_file = work / "argv.json"
    recorder = work / "argv_recorder.py"
    recorder.write_text(
        "import json, os, sys, runpy\n"
        "from pathlib import Path\n"
        "Path(os.environ['ARGV_OUT']).write_text(json.dumps(sys.argv[1:]))\n"
        "sys.argv[0] = os.environ['DEVIN_FAKE']\n"
        "sys.path.insert(0, os.path.dirname(sys.argv[0]))\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n",
        encoding="utf-8",
    )
    devin_env.update(
        {
            "DEVIN_BIN": str(recorder),
            "DEVIN_FAKE": str(Path(__file__).resolve().parents[2] / "fakes" / "fake_devin.py"),
            "ARGV_OUT": str(argv_file),
        }
    )
    init_devin(devin_env)
    code, _, _ = turn(devin_env, work, 1)
    assert code == 0
    assert json.loads(argv_file.read_text()) == ["-p", "turn 1 prompt\n"]
    devin_env["FAKE_DEVIN_SCENARIO"] = "resume"
    code2, _, _ = turn(devin_env, work, 2)
    assert code2 == 0
    argv2 = json.loads(argv_file.read_text())
    assert argv2 == ["--resume", "devin-session-01a09b11", "-p", "turn 2 prompt\n"]
