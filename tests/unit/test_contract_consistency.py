"""Parse the four frozen contracts and assert identifiers match."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
API_YAML = CONTRACTS / "api.yaml"

CODEX_EVENTS = {
    "thread.started",
    "turn.started",
    "item.started",
    "item.updated",
    "item.completed",
    "turn.completed",
    "error",
}
RUNNER_EVENTS = {"sbx.turn_started", "sbx.turn_finished", "sbx.error"}
ITEM_TYPES = {"agent_message", "command_execution", "file_change", "reasoning"}
PATHS = {"inbox/<n>.md", "turns/<n>.json", "events.jsonl", "session.json"}
EXIT_CODES = {"0": "success", "2": "codex_nonzero", "3": "timeout", "4": "bad_json"}
ERROR_CODES = {401, 404, 409, 429}
USAGE = {"input_tokens", "cached_input_tokens", "output_tokens"}
COMMANDS = {"init", "turn", "stop"}
HTTP_PATHS = {
    "/api/sessions",
    "/api/sessions/{id}",
    "/api/sessions/{id}/messages",
    "/api/sessions/{id}/stop",
    "/api/sessions/{id}/events",
}


def _canonical_md(name: str) -> dict:
    text = (CONTRACTS / name).read_text(encoding="utf-8")
    match = re.search(r"```canonical-yaml\n(.*?)```", text, re.S)
    assert match, f"{name} missing canonical-yaml block"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict)
    return data


def _load_api() -> dict:
    data = yaml.safe_load(API_YAML.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_canonical_blocks_agree() -> None:
    fs = _canonical_md("filesystem.md")
    events = _canonical_md("events.md")
    runner = _canonical_md("runner-cli.md")
    api = _load_api()
    canon = api["x-canonical"]

    for blob in (events, runner, canon):
        assert set(blob["codex_events"]) == CODEX_EVENTS
        assert set(blob["runner_events"]) == RUNNER_EVENTS
        assert set(blob["item_types"]) == ITEM_TYPES
        assert set(blob["usage_fields"]) == USAGE

    assert set(fs["paths"]) == PATHS
    assert set(events["paths"]) == PATHS
    assert set(runner["paths"]) == PATHS
    assert set(canon["paths"]) == PATHS

    def _exits(blob: dict) -> dict[str, str]:
        return {str(k): str(v) for k, v in blob["exit_codes"].items()}

    assert _exits(events) == EXIT_CODES
    assert _exits(runner) == EXIT_CODES
    assert _exits(canon) == EXIT_CODES

    assert set(events["error_codes"]) == ERROR_CODES
    assert set(runner["error_codes"]) == ERROR_CODES
    assert set(canon["error_codes"]) == ERROR_CODES

    assert set(runner["commands"]) == COMMANDS
    assert set(canon["commands"]) == COMMANDS

    assert fs["codex_home"] == "$SBX_WORK/.codex"
    assert fs["production_work"] == "/work"


def test_api_yaml_paths_and_status_codes() -> None:
    api = _load_api()
    assert api["openapi"].startswith("3.1")
    paths = set(api["paths"])
    assert HTTP_PATHS <= paths

    codes: set[int] = set()
    for path_item in api["paths"].values():
        for op in path_item.values():
            if not isinstance(op, dict) or "responses" not in op:
                continue
            for code in op["responses"]:
                if str(code).isdigit():
                    codes.add(int(code))
    assert ERROR_CODES <= codes

    events_op = api["paths"]["/api/sessions/{id}/events"]["get"]
    desc = events_op["responses"]["200"]["description"]
    assert "id: <events.jsonl" in desc
    assert "event: <type>" in desc
    assert "data: <json>" in desc
    assert ": keepalive" in desc
    assert "15 s" in desc or "15s" in desc

    session = api["components"]["schemas"]["Session"]
    required = set(session["required"])
    assert required == {
        "id",
        "title",
        "status",
        "created_at",
        "updated_at",
        "model",
        "turns",
        "usage",
        "cost_estimate_usd",
        "sandbox_seconds",
        "messages",
    }
    statuses = set(api["components"]["schemas"]["SessionStatus"]["enum"])
    assert statuses == {"creating", "idle", "running", "closed", "timed_out", "lost"}

    schemes = api["components"]["securitySchemes"]
    assert schemes["basicAuth"]["scheme"] == "basic"


def test_markdown_bodies_mention_shared_tokens() -> None:
    events = (CONTRACTS / "events.md").read_text(encoding="utf-8")
    runner = (CONTRACTS / "runner-cli.md").read_text(encoding="utf-8")
    filesystem = (CONTRACTS / "filesystem.md").read_text(encoding="utf-8")
    for name in CODEX_EVENTS | RUNNER_EVENTS:
        assert name in events, name
        assert name in runner, name
    for path in PATHS:
        assert path in filesystem
        assert path in runner
    assert "exit" in runner.lower()
    for code in ("0", "2", "3", "4"):
        assert re.search(rf"\b{code}\b", runner)


def test_fixtures_only_use_catalogued_event_names() -> None:
    fixture_dir = ROOT / "tests" / "fixtures" / "events"
    seen: set[str] = set()
    item_seen: set[str] = set()
    for path in fixture_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            obj = json.loads(line)
            seen.add(obj["type"])
            item = obj.get("item")
            if isinstance(item, dict) and "type" in item:
                item_seen.add(item["type"])
    assert seen <= (CODEX_EVENTS | RUNNER_EVENTS | {"turn.failed"})
    assert item_seen <= ITEM_TYPES
    assert "thread.started" in seen
    assert "agent_message" in item_seen
