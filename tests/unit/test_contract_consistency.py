"""Parse the four frozen contracts and assert identifiers match."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
API_YAML = CONTRACTS / "api.yaml"
API_V1_YAML = CONTRACTS / "api-v1.yaml"

CODEX_EVENTS = {
    "thread.started",
    "turn.started",
    "item.started",
    "item.updated",
    "item.completed",
    "turn.completed",
    "turn.failed",
    "error",
}
RUNNER_EVENTS = {"sbx.turn_started", "sbx.turn_finished", "sbx.error", "sbx.session_meta"}
ITEM_TYPES = {"agent_message", "command_execution", "file_change", "reasoning", "error"}
PATHS = {"inbox/<n>.md", "turns/<n>.json", "events.jsonl", "events.raw.jsonl", "session.json"}
EXIT_CODES = {
    "0": "success",
    "2": "cli_nonzero",
    "3": "timeout",
    "4": "bad_json",
    "5": "auth_invalid",
}
ERROR_CODES = {400, 401, 404, 409, 429}
ERROR_SUBCODES = {
    "unauthorized",
    "not_found",
    "invalid_provider",
    "turn_in_progress",
    "session_not_runnable",
    "account_busy",
    "account_unavailable",
    "provider_exhausted",
    "concurrency_limit",
}
PROVIDERS = {"codex", "antigravity", "grok", "opencode", "devin"}
USAGE = {"input_tokens", "cached_input_tokens", "output_tokens"}
USAGE_OPTIONAL = {"cache_write_input_tokens", "reasoning_output_tokens"}
USAGE_MAPPING = {
    "cache_read_tokens": "cached_input_tokens",
    "thinking_tokens": "reasoning_output_tokens",
    "cache_write_tokens": "cache_write_input_tokens",
}
COMMANDS = {"init", "turn", "stop", "export-credentials"}
HTTP_PATHS = {
    "/api/sessions",
    "/api/sessions/{id}",
    "/api/sessions/{id}/messages",
    "/api/sessions/{id}/stop",
    "/api/sessions/{id}/events",
    "/api/providers",
    "/api/accounts",
    "/api/accounts/{id}",
    "/api/accounts/{id}/verify",
    "/api/api-keys",
    "/api/api-keys/{id}",
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
        assert set(blob["usage_fields_optional"]) == USAGE_OPTIONAL
        assert set(blob["error_subcodes"]) == ERROR_SUBCODES

    for blob in (events, runner, canon):
        assert set(blob["providers"]) == PROVIDERS
    assert dict(events["usage_mapping"]) == USAGE_MAPPING

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

    assert fs["home"] == "$SBX_WORK/home"
    assert fs["codex_home"] == "$HOME/.codex"
    assert fs["codex_home_v1"] == "$SBX_WORK/.codex"
    assert fs["production_work"] == "/work"
    assert fs["credential_env"] == "SBX_ACCOUNT_CREDENTIAL"
    assert fs["account_id_env"] == "SBX_ACCOUNT_ID"
    assert fs["codex_rollout"] == ("$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl")
    assert str(fs["auth_json_mode"]) == "600"
    assert fs["config_toml"]["approval_policy"] == "never"
    assert fs["config_toml"]["sandbox_mode"] == "danger-full-access"
    assert fs["shell_environment_policy_exclude"] == [
        "CODEX_AUTH_JSON",
        "SBX_PROVIDER_API_KEY",
        "SBX_ACCOUNT_CREDENTIAL",
    ]
    assert set(fs["session_json_fields"]) == {
        "turn",
        "native_session_id",
        "provider",
        "account_id",
    }
    assert fs["session_json_aliases"]["codex_session_id"] == "native_session_id"


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
        "provider",
        "account_id",
        "model",
        "turns",
        "usage",
        "cost_estimate_usd",
        "sandbox_seconds",
        "messages",
    }
    statuses = set(api["components"]["schemas"]["SessionStatus"]["enum"])
    assert statuses == {"creating", "idle", "running", "closed", "timed_out", "lost"}

    assert set(api["components"]["schemas"]["ProviderId"]["enum"]) == PROVIDERS
    create_req = api["components"]["schemas"]["CreateSessionRequest"]["properties"]
    assert "provider" in create_req
    assert "account_id" in create_req
    assert "model" in create_req

    account_schema = api["components"]["schemas"]["Account"]
    assert set(account_schema["required"]) == {
        "id",
        "provider",
        "label",
        "status",
        "max_concurrent",
    }
    assert set(api["components"]["schemas"]["AccountStatus"]["enum"]) == {
        "active",
        "cooling",
        "invalid",
        "disabled",
    }

    usage_schema = api["components"]["schemas"]["Usage"]
    assert set(usage_schema["required"]) == USAGE
    assert USAGE_OPTIONAL <= set(usage_schema["properties"])

    schemes = api["components"]["securitySchemes"]
    assert schemes["basicAuth"]["scheme"] == "basic"


def test_api_v1_yaml_is_cursor_shaped() -> None:
    """Public v1 contract: /v1 routes, Bearer sbx_ auth, {error:{code,...}}."""
    data = yaml.safe_load(API_V1_YAML.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert str(data["openapi"]).startswith("3.1")
    paths = set(data["paths"])
    assert paths, "api-v1.yaml has no paths"
    for path in paths:
        assert path.startswith("/v1/"), path
    assert "/v1/agents" in paths
    assert "/v1/agents/{id}" in paths
    schemes = data["components"]["securitySchemes"]
    bearer = schemes["bearerAuth"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    err = data["components"]["schemas"]["ErrorBody"]
    assert "error" in err["required"]
    props = set(err["properties"]["error"]["properties"])
    assert {"code", "message"} <= props


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
    for code in ("0", "2", "3", "4", "5"):
        assert re.search(rf"\b{code}\b", runner)
    assert "stdin=subprocess.DEVNULL" in runner
    assert "bufsize=1" in runner
    assert "export-credentials" in runner
    assert "SBX_ACCOUNT_CREDENTIAL" in runner
    for provider in PROVIDERS:
        assert provider in runner
        assert provider in events
        assert provider in filesystem
    assert "sbx.session_meta" in events
    assert "CODEX_AUTH_JSON" in filesystem
    assert "SBX_ACCOUNT_CREDENTIAL" in filesystem
    assert "600" in filesystem


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
    assert seen <= (CODEX_EVENTS | RUNNER_EVENTS)
    assert item_seen <= ITEM_TYPES
    assert "thread.started" in seen
    assert "agent_message" in item_seen
    assert "error" in item_seen
    assert "turn.failed" in seen


def test_real_multiturn_types_are_canonical() -> None:
    path = ROOT / "tests" / "fixtures" / "events" / "real_multiturn.jsonl"
    types: set[str] = set()
    item_types: set[str] = set()
    thread_ids: list[str] = []
    turn_markers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        types.add(obj["type"])
        turn_markers.append(obj["type"])
        if obj["type"] == "thread.started":
            thread_ids.append(obj["thread_id"])
        item = obj.get("item")
        if isinstance(item, dict) and "type" in item:
            item_types.add(item["type"])
    assert types <= CODEX_EVENTS
    assert item_types <= ITEM_TYPES
    assert thread_ids == ["01a09a36-b4fb-7f90-b96e-42adeefa05e0"] * 3
    assert turn_markers.count("thread.started") == 3
    assert turn_markers.count("turn.completed") == 3
    assert "error" in item_types
    assert "command_execution" in item_types
    assert "file_change" in item_types


def test_fixture_observed_codex_shape() -> None:
    """P0-recorded sequencing: agent_message is completed-only; bash -lc; 5 usage fields."""
    fixture_dir = ROOT / "tests" / "fixtures" / "events"
    for path in fixture_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            obj = json.loads(stripped)
            item = obj.get("item")
            if not isinstance(item, dict):
                if obj.get("type") == "turn.completed":
                    usage = obj["usage"]
                    assert USAGE <= set(usage)
                    assert USAGE_OPTIONAL <= set(usage)
                    assert usage["cached_input_tokens"] >= 10000
                if obj.get("type") == "thread.started":
                    assert obj["thread_id"] == "01a09a36-b4fb-7f90-b96e-42adeefa05e0"
                continue
            if item.get("type") == "agent_message":
                assert obj["type"] == "item.completed"
            if item.get("type") == "command_execution":
                assert item["command"].startswith("/bin/bash -lc ")
                if obj["type"] == "item.started":
                    assert item.get("exit_code") is None
            if item.get("type") in {"command_execution", "file_change"}:
                assert obj["type"] in {"item.started", "item.updated", "item.completed"}
