"""Unified Python SDK + CLI against the real API (RFC 08 SDK/CLI)."""

from __future__ import annotations

import io
import json
import os
import stat
import threading

import httpx
import pytest
from tests.support.api import ApiStack

from sbx.cli import main as cli_main
from sbx.sdk import SBXClient, SBXError

ZEN = "byok-sdk-key-000000000001"


@pytest.fixture
def env(db, tmp_path):
    stack = ApiStack(db, tmp_path)
    stop = threading.Event()
    thread = threading.Thread(
        target=stack.worker.run_forever, args=(stop,), kwargs={"idle_sleep": 0.05}, daemon=True
    )
    thread.start()
    client = SBXClient("http://testserver", http=stack.client())
    email, password = "sdk@example.test", "sdk password 12345"
    client.register(email, password)
    client.verify_email(stack.services.mailer.latest(email)["body"].split("token=")[1].strip())
    client.login(email, password)
    yield stack, client, email, password
    stop.set()
    thread.join(timeout=5)
    stack.shutdown()


def test_sdk_execute_follow_up_and_events(env) -> None:
    stack, client, *_ = env
    zen = client.connections.add_inference(
        ZEN, model="test-model", base_url="https://inference.example.test/v1", label="byok"
    )
    assert zen["kind"] == "inference_api" and zen["config"]["model"] == "test-model"
    assert client.connections.wait_health(zen["id"])["health"] == "ready"
    assert client.models()["preferred_model"] == "test-model"
    assert client.models("claude")["connections"][0]["compatible"] is False
    tiers = {h["provider_id"]: h["support_tier"] for h in client.harnesses()}
    assert {"opencode", "codex", "claude", "grok", "commandcode"} <= {
        p for p, tier in tiers.items() if tier == "supported"
    }
    seen: list[str] = []
    first = client.execute(
        "remember KIWI",
        executor={"backend": "local"},
        deadline=60,
        on_event=lambda e: seen.append(e["type"]),
    )
    assert first["turn"]["state"] == "succeeded" and "turn.succeeded" in seen
    second = client.execute("what fruit? [recall]", session_id=first["session_id"], deadline=60)
    assert second["turn"]["state"] == "succeeded"
    messages = client.messages.list(first["session_id"])
    assert "remember KIWI" in messages[-1]["parts"][-1]["content"]
    events = list(client.sessions.events(first["session_id"]))
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    sse = list(client.sessions.stream(first["session_id"], max_seconds=1))
    assert sse[0]["type"] == "session.created"
    exported = client.sessions.export(first["session_id"])
    assert exported["session"]["id"] == first["session_id"] and ZEN not in json.dumps(exported)
    with pytest.raises(SBXError) as err:
        client.sessions.get("sess_does_not_exist")
    assert err.value.code == "not_found"


class _DropFirstResponse(httpx.Client):
    """Forwards the first mutation, then pretends the response was lost."""

    def __init__(self, inner: httpx.Client) -> None:
        self.inner, self.dropped, self.keys = inner, False, []

    def request(self, method, url, **kw):  # type: ignore[override]
        if method == "POST":
            self.keys.append(kw["headers"].get("Idempotency-Key"))
        response = self.inner.request(method, url, **kw)
        if method == "POST" and "sessions" in str(url) and not self.dropped:
            self.dropped = True
            raise httpx.ReadTimeout("lost response")
        return response


def test_transport_retry_reuses_idempotency_key(env) -> None:
    stack, client, *_ = env
    client.connections.add_inference(
        ZEN, model="test-model", base_url="https://inference.example.test/v1"
    )
    flaky = _DropFirstResponse(client.http)
    retrying = SBXClient("http://testserver", http=flaky, workspace_id=client.workspace_id)
    retrying.csrf = client.csrf
    created = retrying.sessions.create(
        harness={"provider_id": "opencode"}, executor={"backend": "local"}
    )
    assert flaky.keys[-1] == flaky.keys[-2], "same key reused across network uncertainty"
    assert stack.db.read(lambda u: u.count("sessions")) == 1
    assert created["session_id"]


def test_cli_reads_secrets_from_stdin_and_never_prints_them(
    env, capsys, tmp_path, monkeypatch
) -> None:
    stack, client, email, password = env
    assert (
        cli_main(
            [
                "connections",
                "add",
                "inference_api",
                "--label",
                "cli-byok",
                "--endpoint",
                "openai_chat=https://inference.example.test/v1",
                "--endpoint",
                "anthropic_messages=https://inference.example.test/anthropic",
                "--model",
                "test-model",
            ],
            client=client,
            stdin=io.StringIO(ZEN),
        )
        == 0
    )
    out = capsys.readouterr().out
    added = json.loads(out)
    assert ZEN not in out and added["label"] == "cli-byok"
    assert set(added["config"]["endpoints"]) == {"openai_chat", "anthropic_messages"}
    # Rotating the key from stdin keeps the endpoints and model.
    assert (
        cli_main(
            ["connections", "replace", added["id"]],
            client=client,
            stdin=io.StringIO("rotated-sdk-key-000000000002"),
        )
        == 0
    )
    rotated = json.loads(capsys.readouterr().out)
    assert rotated["config"] == added["config"] and rotated["credential"]["ordinal"] == 2
    assert cli_main(["harnesses"], client=client) == 0
    assert "inference_protocols" in capsys.readouterr().out
    assert cli_main(["connections", "list"], client=client) == 0
    assert ZEN not in capsys.readouterr().out
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    fresh = SBXClient("http://testserver", http=stack.client())
    assert (
        cli_main(
            ["auth", "login", "--email", email], client=fresh, stdin=io.StringIO(password + "\n")
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    path = result["config"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    stored = json.load(open(path))
    assert stored["api_key"].startswith("sbx_key_") and stored["api_key"] not in json.dumps(result)
    keyed = SBXClient("http://testserver", stored["api_key"], http=stack.client())
    assert keyed.me()["auth"]["via"] == "api_key"
    assert cli_main(["sessions", "show", "sess_nope"], client=keyed) == 1
    assert "not_found" in capsys.readouterr().err
