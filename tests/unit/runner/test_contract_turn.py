"""``runner turn --output-contract`` end-to-end (SOR-130).

Exercises the real ``python -m runtime.runner`` turn pipeline against
``fake_codex.py``: the contract file is the control-plane artifact
(``_contract_<n>.json``), the runner steers the provider prompt, evaluates
the final message, and records ``structured_output`` + ``output_contract``
evidence on ``turns/<n>.json``. The runner reports the verdict; run-level
enforcement lives in the control plane (see control/api_v1 tests).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.runner.conftest import (
    init_runner,
    load_json,
    parsed_events,
    run_runner,
    write_message,
)

SCHEMA = {
    "type": "object",
    "required": ["summary", "ok"],
    "properties": {
        "summary": {"type": "string"},
        "ok": {"type": "boolean"},
        "files": {"type": "array", "items": {"type": "string"}},
    },
}
VALUE = {"summary": "created hello.txt", "ok": True, "files": ["hello.txt"]}


def _contract(work: Path, body: dict | str) -> Path:
    path = work / "_contract_1.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return path


def _turn(
    env: dict[str, str],
    work: Path,
    *,
    n: int = 1,
    contract: Path | None = None,
    timeout: float = 25.0,
) -> tuple[int, dict, list[dict]]:
    msg = write_message(work)
    argv = ["turn", "--n", str(n), "--message-file", str(msg), "--max-seconds", "30"]
    if contract is not None:
        argv += ["--output-contract", str(contract)]
    result = run_runner(argv, env, timeout=timeout)
    turn_path = work / "turns" / f"{n}.json"
    doc = load_json(turn_path) if turn_path.is_file() else {}
    return result.returncode, doc, parsed_events(work)


def test_contract_valid_output(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "structured"
    contract = _contract(work, {"schema": SCHEMA})
    code, turn, events = _turn(runner_env, work, contract=contract)
    assert code == 0
    assert turn["status"] == "success"
    assert turn["structured_output"] == VALUE
    verdict = turn["output_contract"]
    assert verdict["status"] == "valid"
    assert verdict["enforcement"] == "strict"
    assert verdict["schema_digest"].startswith("sha256:")
    assert verdict["extraction"] == "raw"
    assert verdict["violations"] == []
    # Text result + event stream unchanged.
    assert turn["message"]
    assert (work / "inbox" / "1.md").read_text(encoding="utf-8") == "hello from test\n"
    types = [e["type"] for e in events]
    assert types[0] == "sbx.session_meta"
    assert types[-1] == "sbx.turn_finished"


def test_contract_fence_extraction(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "structured_fence"
    contract = _contract(work, {"schema": SCHEMA})
    code, turn, _ = _turn(runner_env, work, contract=contract)
    assert code == 0
    assert turn["output_contract"]["status"] == "valid"
    assert turn["output_contract"]["extraction"] == "fence"
    assert turn["structured_output"] == VALUE


def test_contract_schema_violation_is_diagnosable(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "structured"
    schema = dict(SCHEMA, required=["summary", "ok", "missing_field"])
    contract = _contract(work, {"schema": schema})
    code, turn, _ = _turn(runner_env, work, contract=contract)
    assert code == 0  # the provider ran fine; the verdict rides the payload
    assert turn["status"] == "success"
    verdict = turn["output_contract"]
    assert verdict["status"] == "invalid"
    assert verdict["violations"][0]["code"] == "required"
    assert "missing_field" in verdict["violations"][0]["message"]
    assert turn["structured_output"] == VALUE  # extracted, but not valid


def test_contract_malformed_output(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "success"  # plain-text agent message
    contract = _contract(work, {"schema": SCHEMA})
    code, turn, _ = _turn(runner_env, work, contract=contract)
    assert code == 0
    verdict = turn["output_contract"]
    assert verdict["status"] == "invalid"
    assert verdict["extraction"] is None
    assert verdict["violations"][0]["code"] == "not_json"
    assert turn["structured_output"] is None


def test_contract_skipped_on_failed_turn(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "nonzero"
    contract = _contract(work, {"schema": SCHEMA})
    code, turn, _ = _turn(runner_env, work, contract=contract)
    assert code == 2
    assert turn["status"] == "codex_error"
    assert turn["output_contract"]["status"] == "skipped"
    assert turn["structured_output"] is None


def test_malformed_contract_file_fails_closed(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    contract = _contract(work, "{not json")
    code, turn, events = _turn(runner_env, work, contract=contract)
    assert code == 1  # EXIT_INTERNAL — never run uncontracted
    assert not (work / "turns" / "1.json").is_file()
    errors = [e for e in events if e.get("type") == "sbx.error"]
    assert errors and "invalid output contract" in errors[-1]["message"]


def test_contract_instruction_reaches_provider_prompt(
    work: Path, runner_env: dict[str, str]
) -> None:
    """The schema instruction is appended to the argv prompt, not the inbox."""
    init_runner(runner_env)
    spy = Path(__file__).resolve().parent / "codex_spy.py"
    runner_env["CODEX_BIN"] = str(spy)
    dump = work / "spy.json"
    runner_env["CODEX_SPY_TARGET"] = ""
    runner_env["CODEX_SPY_OUT"] = str(dump)
    contract = _contract(work, {"schema": SCHEMA})
    msg = write_message(work)
    result = run_runner(
        ["turn", "--n", "1", "--message-file", str(msg), "--output-contract", str(contract)],
        runner_env,
    )
    spy_data = json.loads(dump.read_text(encoding="utf-8"))
    prompt = spy_data["argv"][-1]
    assert "[output-contract]" in prompt
    assert '"required":["summary","ok"]' in prompt or '"required": ["summary", "ok"]' in prompt
    assert result.returncode == 0


def test_no_contract_leaves_payload_unchanged(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "structured"
    code, turn, _ = _turn(runner_env, work)
    assert code == 0
    assert "structured_output" not in turn
    assert "output_contract" not in turn
