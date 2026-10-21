"""`sbx mcp serve` as a real stdio subprocess against a live isolated API.

The MCP client is the official SDK client; the server starts exactly as Codex or Claude Code
would start it. Every create names its billing route (custom_api Connection or subscription
Machine Slot) explicitly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters
from tests.support.api import ApiStack
from tests.support.live_api import LiveApi, free_port

from sbx.sdk import SBXClient

ZEN = "byok-mcp-key-000000000001"
TOOL_NAMES = {
    "sbx_list_harnesses",
    "sbx_list_resources",
    "sbx_list_tasks",
    "sbx_create_task",
    "sbx_get_task",
    "sbx_get_events",
    "sbx_get_result",
    "sbx_send_followup",
    "sbx_cancel_task",
    "sbx_retry_task",
    "sbx_acknowledge_unknown",
}


class Env:
    """One live API, one user with a write-scope key, and the SDK for cross-entry checks."""

    def __init__(
        self,
        live: LiveApi,
        sdk: SBXClient,
        api_key: str,
        key_id: str,
        connection_id: str,
        root: Path,
    ) -> None:
        self.live = live
        self.sdk = sdk
        self.api_key = api_key
        self.key_id = key_id
        self.connection_id = connection_id
        self.root = root

    def child_env(self, *, api_key: str | None = None, config_home: Path | None = None) -> dict:
        """What a client hands the server: its own HOME and config, no ambient SBX variables."""
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        return {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home or (self.root / "xdg")),
            "SBX_BASE_URL": self.live.base_url,
            "SBX_API_KEY": self.api_key if api_key is None else api_key,
        }

    def create_args(self, key: str, prompt: str, **overrides: Any) -> dict[str, Any]:
        args = {
            "prompt": prompt,
            "harness_id": "opencode",
            "executor_backend": "local",
            "inference_mode": "custom_api",
            "inference_connection_id": self.connection_id,
            "idempotency_key": key,
        }
        args.update(overrides)
        return args


@pytest.fixture
def env(db, tmp_path) -> Any:
    stack = ApiStack(db, tmp_path)
    with LiveApi(stack) as live:
        email, password = "mcp@example.test", "mcp password 12345"
        sdk = SBXClient(live.base_url, http=httpx.Client(base_url=live.base_url, timeout=30))
        sdk.register(email, password)
        sdk.verify_email(stack.services.mailer.latest(email)["body"].split("token=")[1].strip())
        sdk.login(email, password)
        connection = sdk.connections.add_inference(
            ZEN, model="test-model", base_url="https://inference.example.test/v1", label="byok"
        )
        sdk.connections.wait_health(connection["id"])
        key = sdk.create_api_key("mcp-test", ["write"])
        yield Env(live, sdk, key["key"], key["id"], connection["id"], tmp_path)


@asynccontextmanager
async def mcp(env: Env, **kw: Any) -> AsyncIterator[Client]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sbx", "mcp", "serve"],
        env=env.child_env(**kw),
    )
    async with Client(params) as client:
        yield client


async def call(client: Client, name: str, **args: Any) -> tuple[dict[str, Any], bool]:
    result = await client.call_tool(name, args)
    return result.structured_content or {}, bool(result.is_error)


async def ok(client: Client, name: str, **args: Any) -> dict[str, Any]:
    body, failed = await call(client, name, **args)
    assert not failed, body
    return body


async def wait_terminal(client: Client, session_id: str, turn_id: str) -> dict[str, Any]:
    with anyio.fail_after(60):
        while True:
            turn = (await ok(client, "sbx_get_task", session_id=session_id, turn_id=turn_id))[
                "turn"
            ]
            if turn["terminal"]:
                return turn
            await anyio.sleep(0.1)


async def test_tools_are_discoverable_with_honest_annotations(env: Env) -> None:
    async with mcp(env) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == TOOL_NAMES
        for name in ("sbx_list_harnesses", "sbx_list_resources", "sbx_get_result"):
            assert tools[name].annotations.read_only_hint is True
        for name in ("sbx_create_task", "sbx_send_followup", "sbx_retry_task"):
            assert tools[name].annotations.read_only_hint is False
            assert tools[name].annotations.destructive_hint is True
        for name, tool in tools.items():
            assert tool.input_schema["additionalProperties"] is False, name
            assert tool.output_schema is not None, name

        harnesses = await ok(client, "sbx_list_harnesses")
        startable = {h["provider_id"] for h in harnesses["items"] if h["startable"]}
        assert {"opencode", "codex", "claude"} <= startable

        resources = await ok(client, "sbx_list_resources", harness_id="opencode")
        (route,) = resources["harnesses"][0]["api_key_routes"]
        assert route["connection_id"] == env.connection_id
        assert route["usable"] is True and "test-model" in route["models"]
        assert env.api_key not in json.dumps(resources)
        assert "local" in {b["kind"] for b in resources["executor_backends"]}


async def test_create_result_followup_and_cancel_across_turns(env: Env) -> None:
    async with mcp(env) as client:
        created = await ok(
            client, "sbx_create_task", **env.create_args("create-key-0001", "remember KIWI")
        )
        session_id, first = created["session_id"], created["turn_id"]
        assert (await wait_terminal(client, session_id, first))["state"] == "succeeded"
        first_text = (await ok(client, "sbx_get_result", turn_id=first))["text"]
        assert first_text

        # Same key and body: the same Session comes back, and no second one exists.
        replay = await ok(
            client, "sbx_create_task", **env.create_args("create-key-0001", "remember KIWI")
        )
        assert replay["session_id"] == session_id
        assert len(env.sdk.sessions.list(limit=50)["items"]) == 1

        # Same key, different body: a conflict, never a silent new task.
        body, failed = await call(
            client, "sbx_create_task", **env.create_args("create-key-0001", "something else")
        )
        assert failed and body["code"] == "idempotency_conflict"

        followup = await ok(
            client,
            "sbx_send_followup",
            session_id=session_id,
            content="what fruit? [recall]",
            idempotency_key="followup-key-01",
        )
        second = followup["turn_id"]
        assert (await wait_terminal(client, session_id, second))["state"] == "succeeded"
        second_text = (await ok(client, "sbx_get_result", turn_id=second))["text"]
        assert second_text and second_text != first_text
        # Exact-Turn output: the first Turn still returns its own text, not the later one.
        assert (await ok(client, "sbx_get_result", turn_id=first))["text"] == first_text

        again = await ok(
            client,
            "sbx_send_followup",
            session_id=session_id,
            content="what fruit? [recall]",
            idempotency_key="followup-key-01",
        )
        assert again["turn_id"] == second
        assert len(env.sdk.turns.list(session_id)) == 2

        # The SDK, a separate entry point, reads the same durable Session.
        assert env.sdk.sessions.get(session_id)["session"]["id"] == session_id

        # Cancellation: a hanging Turn stays pending until the API reports it terminal.
        hung = await ok(client, "sbx_create_task", **env.create_args("hang-key-0001", "[hang]"))
        with anyio.fail_after(30):
            while (
                await ok(
                    client, "sbx_get_task", session_id=hung["session_id"], turn_id=hung["turn_id"]
                )
            )["turn"]["state"] != "running":
                await anyio.sleep(0.1)
        cancelled = await ok(
            client, "sbx_cancel_task", turn_id=hung["turn_id"], idempotency_key="cancel-key-0001"
        )
        assert cancelled["turn"]["cancel_requested"] is True
        final = await wait_terminal(client, hung["session_id"], hung["turn_id"])
        assert final["state"] == "cancelled" and final["cancel_pending"] is False


async def test_history_is_paged_and_bounded(env: Env) -> None:
    async with mcp(env) as client:
        created = await ok(
            client, "sbx_create_task", **env.create_args("page-key-00001", "remember KIWI")
        )
        await wait_terminal(client, created["session_id"], created["turn_id"])
        seen: list[int] = []
        after = 0
        while True:
            page = await ok(
                client, "sbx_get_events", session_id=created["session_id"], after=after, limit=2
            )
            assert len(page["items"]) <= 2
            if not page["items"]:
                break
            seen += [event["seq"] for event in page["items"]]
            after = page["next_after"]
        assert seen == sorted(set(seen)) and len(seen) > 2
        history = await ok(
            client, "sbx_get_events", session_id=created["session_id"], before=seen[-1] + 1, limit=2
        )
        assert [e["seq"] for e in history["items"]] == seen[-2:]
        body, failed = await call(
            client, "sbx_get_events", session_id=created["session_id"], after=1, before=2
        )
        assert failed and body["code"] == "validation_failed"


async def test_task_listing_pages_without_duplicates(env: Env) -> None:
    for _ in range(3):
        env.sdk.sessions.create(harness={"provider_id": "opencode"}, executor={"backend": "local"})
    async with mcp(env) as client:
        first = await ok(client, "sbx_list_tasks", limit=2)
        assert len(first["items"]) == 2 and first["next_cursor"]
        second = await ok(client, "sbx_list_tasks", limit=2, cursor=first["next_cursor"])
        ids = [s["id"] for s in first["items"] + second["items"]]
        assert len(ids) == len(set(ids)) == 3


async def test_subscription_is_forwarded_as_subscription_never_as_an_api_key(env: Env) -> None:
    async with mcp(env) as client:
        body, failed = await call(
            client,
            "sbx_create_task",
            prompt="hello",
            harness_id="codex",
            executor_backend="modal",
            inference_mode="subscription",
            machine_slot_id="slot_missing_for_test",
            idempotency_key="slot-key-000001",
        )
        assert failed and body["code"] == "not_found"
        assert env.sdk.sessions.list(limit=50)["items"] == []

        # A subscription request that also names an API-key Connection is refused locally.
        body, failed = await call(
            client,
            "sbx_create_task",
            prompt="hello",
            harness_id="codex",
            executor_backend="modal",
            inference_mode="subscription",
            machine_slot_id="slot_missing_for_test",
            inference_connection_id=env.connection_id,
            idempotency_key="slot-key-000002",
        )
        assert failed and body["code"] == "validation_failed"
        assert env.sdk.sessions.list(limit=50)["items"] == []


async def test_a_revoked_key_is_rejected_on_the_next_call(env: Env) -> None:
    async with mcp(env) as client:
        assert (await ok(client, "sbx_list_harnesses"))["items"]
        env.sdk.revoke_api_key(env.key_id)
        body, failed = await call(client, "sbx_list_harnesses")
        assert failed and body["code"] == "unauthenticated" and body["action"]
        assert env.api_key not in json.dumps(body)


async def test_missing_credentials_are_reported_without_a_prompt(env: Env) -> None:
    async with mcp(env, api_key="", config_home=env.root / "empty-xdg") as client:
        body, failed = await call(client, "sbx_list_harnesses")
        assert failed and body["code"] == "unauthenticated"
        assert body["details"] == {"source": "client_config"}
        assert "sbx auth login" in body["action"]


async def test_client_configuration_file_is_reused_unchanged(env: Env) -> None:
    config_home = env.root / "xdg-file"
    (config_home / "sbx").mkdir(parents=True)
    (config_home / "sbx" / "client.json").write_text(
        json.dumps({"base_url": env.live.base_url, "api_key": env.api_key})
    )
    async with mcp(env, api_key="", config_home=config_home) as client:
        assert (await ok(client, "sbx_list_harnesses"))["items"]


async def test_unreachable_api_reports_reachability_with_the_setting(env: Env) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sbx", "mcp", "serve"],
        env={**env.child_env(), "SBX_BASE_URL": f"http://127.0.0.1:{free_port()}"},
    )
    async with Client(params) as client:
        body, failed = await call(client, "sbx_list_harnesses")
    assert failed and body["code"] == "transport_error" and body["category"] == "reachability"
    assert "SBX_BASE_URL" in body["action"]


def _serve_stdio(env: Env, frames: list[dict[str, Any]]) -> tuple[dict[Any, Any], str]:
    """One server process per client connection: the protocol era is fixed per connection."""
    payload = "".join(json.dumps(frame) + "\n" for frame in frames)
    done = subprocess.run(
        [sys.executable, "-m", "sbx", "mcp", "serve"],
        input=payload,
        capture_output=True,
        text=True,
        env=env.child_env(),
        timeout=120,
    )
    assert done.returncode == 0, done.stderr  # stdin EOF exits cleanly
    replies = {r["id"]: r for r in map(json.loads, done.stdout.splitlines())}  # JSON only
    return replies, done.stderr


def test_legacy_client_initializes_and_lists_tools(env: Env) -> None:
    replies, _ = _serve_stdio(
        env,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ],
    )
    assert replies[1]["result"]["protocolVersion"] == "2025-11-25"
    assert {t["name"] for t in replies[2]["result"]["tools"]} == TOOL_NAMES


def test_modern_client_probes_discover_before_any_request(env: Env) -> None:
    replies, stderr = _serve_stdio(
        env,
        [
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "server/discover",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        ],
    )
    assert "2026-07-28" in json.dumps(replies[3]["result"])
    assert {t["name"] for t in replies[4]["result"]["tools"]} == TOOL_NAMES
    assert "sbx-mcp" not in json.dumps(replies)  # diagnostics never reach the protocol channel


def test_lost_response_retry_through_the_sdk_creates_one_session(env: Env) -> None:
    class DropFirstAnswer:
        """Forwards the first session create, then pretends its response was lost."""

        def __init__(self, inner: httpx.Client) -> None:
            self.inner, self.dropped, self.keys = inner, False, []

        def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
            if method == "POST":
                self.keys.append(kw["headers"].get("Idempotency-Key"))
            response = self.inner.request(method, url, **kw)
            if method == "POST" and url.endswith("/sessions") and not self.dropped:
                self.dropped = True
                raise httpx.ReadTimeout("lost response")
            return response

    flaky = DropFirstAnswer(httpx.Client(base_url=env.live.base_url, timeout=30))
    retrying = SBXClient(
        env.live.base_url, env.api_key, http=flaky, workspace_id=env.sdk.workspace_id
    )
    created = retrying.sessions.create(
        harness={"provider_id": "opencode"},
        executor={"backend": "local"},
        connections={"inference": env.connection_id},
        inference={"mode": "custom_api"},
        message={"content": "remember KIWI"},
        idempotency_key="lost-resp-0001",
    )
    assert flaky.keys[-1] == flaky.keys[-2] == "lost-resp-0001"
    assert [s["id"] for s in env.sdk.sessions.list(limit=50)["items"]] == [created["session_id"]]


async def test_resident_executor_is_gated_until_the_deployment_advertises_it(env: Env) -> None:
    """Until B2 lands, this deployment does not advertise resident: MCP reports that and never
    calls the resident machine routes or creates a Session on them."""
    async with mcp(env) as client:
        resources = await ok(client, "sbx_list_resources")
        assert resources["resident_executor"] == "not_enabled"
        assert resources["resident_machines"] == []
        body, failed = await call(
            client,
            "sbx_create_task",
            **env.create_args(
                "resident-key-0001",
                "hello",
                executor_backend="resident",
                resident_machine_id="rmach_missing_for_test",
            ),
        )
        assert failed and body["code"] == "validation_failed"
        assert "not enabled" in body["message"]
        assert env.sdk.sessions.list(limit=50)["items"] == []


async def test_enabled_resident_discovery_uses_the_sdk_list_contract(db, tmp_path) -> None:
    """The resident SDK returns a list, while the HTTP response wraps it in items."""
    from control.executors.resident import ResidentExecutor

    executor = ResidentExecutor()
    stack = ApiStack(db, tmp_path, executors={"resident": executor})
    try:
        with LiveApi(stack) as live:
            sdk = SBXClient(live.base_url)
            email = "resident-mcp@example.test"
            sdk.register(email, "REDACTED-test-password")
            sdk.verify_email(stack.services.mailer.latest(email)["body"].split("token=")[1].strip())
            sdk.login(email, "REDACTED-test-password")
            key = sdk.create_api_key("resident-discovery", ["write"])
            target = Env(live, sdk, key["key"], key["id"], "unused", tmp_path)
            async with mcp(target) as client:
                resources = await ok(client, "sbx_list_resources", probe=False)
                assert resources["resident_executor"] == "advertised"
                assert resources["resident_machines"] == []
    finally:
        executor.tunnels.close_all()
