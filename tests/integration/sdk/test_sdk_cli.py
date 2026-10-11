"""Unified Python SDK + CLI against the real API (RFC 08 SDK/CLI)."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import threading
import time

import httpx
import pytest
from tests.support.api import ApiStack
from tests.support.slots import AGY_CATALOG, CODE, GROK_CATALOG, MODAL, URL, SetupCloud

from sbx.cli import main as cli_main
from sbx.sdk import SBXClient, SBXError

ZEN = "byok-sdk-key-000000000001"
PASTED = "REDACTED-authorization-code"
AGY_URL = "https://accounts.google.com/o/oauth2/auth?client_id=x&state=y"


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


@pytest.fixture
def slot_env(db, tmp_path):
    cloud = SetupCloud()
    stack = ApiStack(db, tmp_path, executors={"modal": cloud})
    stack.services.slots.poll_seconds = 0.05
    stop = threading.Event()
    thread = threading.Thread(
        target=stack.worker.run_forever, args=(stop,), kwargs={"idle_sleep": 0.02}, daemon=True
    )
    thread.start()
    client = SBXClient("http://testserver", http=stack.client())
    email, password = "slots@example.test", "slots password 12345"
    client.register(email, password)
    client.verify_email(stack.services.mailer.latest(email)["body"].split("token=")[1].strip())
    client.login(email, password)
    client.connections.wait_health(client.connections.add("modal", MODAL)["id"], deadline=30)
    yield stack, client, cloud
    stop.set()
    thread.join(timeout=5)
    stack.shutdown()


def _approve(
    cloud: SetupCloud, slot_id: str, *, outcome: str = "succeeded", hold: float = 0.4
) -> threading.Thread:
    """Play the user: wait for the Setup VM, show the code, then approve (or let it fail)."""

    def play() -> None:
        import time

        slot = {"id": slot_id}
        for _ in range(400):
            try:
                cloud.vm_for(slot)
                break
            except StopIteration:
                time.sleep(0.02)
        cloud.show_code(slot)
        time.sleep(hold)  # how long the user takes to approve
        cloud.finish(slot, outcome, None if outcome == "succeeded" else "login_denied")

    thread = threading.Thread(target=play, daemon=True)
    thread.start()
    return thread


def _sign_in_as_user(
    cloud: SetupCloud, slot_id: str, url: str, *, approve: bool = True, hold: float = 0.2
) -> threading.Thread:
    """Play the user of an authorization-code login: open the URL, paste the shown code, finish."""

    def play() -> None:
        slot = {"id": slot_id}
        for _ in range(400):
            try:
                cloud.vm_for(slot)
                break
            except StopIteration:
                time.sleep(0.02)
        cloud.show_url(slot, url)
        if not approve:
            return
        for _ in range(400):
            if cloud.vm_for(slot).get("inputs"):
                break
            time.sleep(0.02)
        state = cloud.vm_for(slot)["state"]
        state.update(phase="verifying")
        state.pop("input", None)
        time.sleep(hold)
        cloud.finish(slot, "succeeded")

    thread = threading.Thread(target=play, daemon=True)
    thread.start()
    return thread


def _wait_for_input(client: SBXClient, slot_id: str) -> None:
    for _ in range(400):
        if client.slots.get(slot_id)["login"].get("input") == "authorization_code":
            return
        time.sleep(0.02)
    raise AssertionError("the login never asked for the browser code")


def test_sdk_slot_login_models_and_subscription_session(slot_env) -> None:
    stack, client, cloud = slot_env
    assert client.slots.providers()[0]["provider_id"] == "codex"
    slot = client.slots.add("codex", label="Laptop", account_alias="team")
    assert slot["status"] == "login_pending"
    seen: list[dict] = []
    player = _approve(cloud, slot["id"])
    ready = client.slots.wait(slot["id"], deadline=30, poll=0.05, on_code=seen.append)
    player.join(timeout=10)
    assert ready["status"] == "ready" and ready["login"]["user_code"] is None
    assert [(s["verification_url"], s["user_code"]) for s in seen] == [(URL, CODE)]
    catalog = client.slots.models(slot["id"])
    assert catalog["source"] == "codex app-server model/list"
    assert [m["id"] for m in catalog["models"]] == ["model-a", "model-b"]
    overview = client.slots.overview()
    assert overview["summary"]["ready"] == 1 and len(client.slots.list()) == 1
    assert client.slots.update(slot["id"], label="Desk")["label"] == "Desk"

    created = client.sessions.create(
        harness={"provider_id": "codex", "model": "model-a", "effort": "ultra"},
        executor={"backend": "modal"},
        inference={"mode": "subscription", "machine_slot_id": slot["id"]},
    )
    assert created["session"]["inference"]["machine_slot_id"] == slot["id"]
    with pytest.raises(SBXError) as err:
        client.sessions.create(
            harness={"provider_id": "codex", "model": "model-b", "effort": "ultra"},
            executor={"backend": "modal"},
            inference={"mode": "subscription", "machine_slot_id": slot["id"]},
        )
    assert err.value.details["supported"] == ["low"]
    with pytest.raises(SBXError) as wrong:
        client.slots.delete(slot["id"], confirm="Laptop")
    assert wrong.value.code == "validation_failed"
    assert client.slots.delete(slot["id"], confirm="Desk")["status"] == "deleting"


def test_sdk_antigravity_authorization_code_login(slot_env) -> None:
    stack, client, cloud = slot_env
    cloud.catalog = AGY_CATALOG
    slot = client.slots.add("antigravity", label="AGY")
    assert slot["status"] == "login_pending"
    code_calls: list[dict] = []
    asked: list[dict] = []
    player = _sign_in_as_user(cloud, slot["id"], AGY_URL)
    ready = client.slots.wait(
        slot["id"],
        deadline=30,
        poll=0.05,
        on_code=code_calls.append,
        on_input=lambda login: asked.append(login) or PASTED,
    )
    player.join(timeout=10)
    assert ready["status"] == "ready"
    assert code_calls == [], "a login without a device code never announces one"
    assert len(asked) == 1 and asked[0]["verification_url"] == AGY_URL
    assert asked[0]["user_code"] is None
    assert [text for _, text in cloud.vm_for(slot)["inputs"]] == [PASTED]
    assert client.slots.models(slot["id"])["source"] == "agy models"


def test_sdk_submit_code_rejections(slot_env) -> None:
    stack, client, cloud = slot_env
    codex = client.slots.add("codex", label="Device")
    with pytest.raises(SBXError) as not_waiting:
        client.slots.submit_code(codex["id"], PASTED)
    assert not_waiting.value.code == "invalid_transition"

    cloud.catalog = AGY_CATALOG
    agy = client.slots.add("antigravity", label="AGY malformed")
    player = _sign_in_as_user(cloud, agy["id"], AGY_URL, approve=False)
    _wait_for_input(client, agy["id"])
    with pytest.raises(SBXError) as malformed:
        client.slots.submit_code(agy["id"], "x")
    assert malformed.value.code == "validation_failed"
    assert client.slots.get(agy["id"])["login"]["input"] == "authorization_code"
    assert client.slots.submit_code(agy["id"], PASTED)["login"]["state"] in (
        "awaiting_user",
        "verifying",
    )
    player.join(timeout=10)
    assert [text for _, text in cloud.vm_for(agy)["inputs"]] == [PASTED]


def test_cli_slots_json_exit_codes_and_no_code_after_use(slot_env, capsys) -> None:
    stack, client, cloud = slot_env
    assert cli_main(["slots", "providers"], client=client) == 0
    assert json.loads(capsys.readouterr().out)[0]["login_method"] == "device_code"
    assert cli_main(["slots", "add", "--label", "CI box"], client=client) == 0
    slot = json.loads(capsys.readouterr().out)
    assert slot["status"] == "login_pending"

    player = _approve(cloud, slot["id"], hold=3.0)  # longer than one CLI poll
    assert cli_main(["slots", "wait", slot["id"], "--timeout", "30"], client=client) == 0
    player.join(timeout=10)
    captured = capsys.readouterr()
    done = json.loads(captured.out)
    assert done["status"] == "ready"
    assert URL in captured.err and CODE in captured.err, "the prompt goes to stderr"
    assert CODE not in captured.out, "stdout JSON never carries a used code"

    assert cli_main(["slots", "models", slot["id"]], client=client) == 0
    assert json.loads(capsys.readouterr().out)["default_model"] == "model-a"
    assert cli_main(["slots", "rename", slot["id"], "--label", "Build"], client=client) == 0
    assert json.loads(capsys.readouterr().out)["label"] == "Build"
    assert cli_main(["slots", "list"], client=client) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["summary"]["total"] == 1 and CODE not in json.dumps(listing)

    args = ["sessions", "create", "--slot", slot["id"], "--model", "model-b"]
    assert cli_main([*args, "--effort", "ultra"], client=client) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "validation_failed" and error["details"]["supported"] == ["low"]
    assert cli_main([*args, "--effort", "low"], client=client) == 0
    session = json.loads(capsys.readouterr().out)["session"]
    assert session["harness"] == {"provider_id": "codex", "model": "model-b", "effort": "low"}
    assert session["inference"]["mode"] == "subscription"

    assert cli_main(["slots", "delete", slot["id"], "--confirm", "nope"], client=client) == 1
    capsys.readouterr()

    assert cli_main(["slots", "add", "--label", "Denied"], client=client) == 0
    denied = json.loads(capsys.readouterr().out)
    player = _approve(cloud, denied["id"], outcome="failed")
    assert cli_main(["slots", "wait", denied["id"], "--timeout", "30"], client=client) == 2
    player.join(timeout=10)
    assert json.loads(capsys.readouterr().out)["status"] == "needs_login"

    assert cli_main(["slots", "add", "--label", "Slow"], client=client) == 0
    slow = json.loads(capsys.readouterr().out)
    assert cli_main(["slots", "wait", slow["id"], "--timeout", "0.3"], client=client) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "login_pending"
    assert cli_main(["slots", "cancel-login", slow["id"]], client=client) == 0


def test_cli_slots_providers_lists_all_three(slot_env, capsys) -> None:
    stack, client, cloud = slot_env
    assert cli_main(["slots", "providers"], client=client) == 0
    providers = json.loads(capsys.readouterr().out)
    assert [(p["provider_id"], p["login_method"]) for p in providers] == [
        ("codex", "device_code"),
        ("antigravity", "authorization_code"),
        ("grok", "device_code"),
    ]


def test_cli_antigravity_code_from_flag_and_stdin(slot_env, capsys, monkeypatch) -> None:
    stack, client, cloud = slot_env
    cloud.catalog = AGY_CATALOG
    for use_flag in (True, False):
        assert cli_main(["slots", "add", "--provider", "antigravity"], client=client) == 0
        slot = json.loads(capsys.readouterr().out)
        player = _sign_in_as_user(cloud, slot["id"], AGY_URL)
        _wait_for_input(client, slot["id"])
        if use_flag:
            assert cli_main(["slots", "code", slot["id"], "--code", PASTED], client=client) == 0
        else:
            monkeypatch.setattr(sys, "stdin", io.StringIO(PASTED + "\n"))
            assert cli_main(["slots", "code", slot["id"]], client=client) == 0
        sent = capsys.readouterr()
        assert PASTED not in sent.out and PASTED not in sent.err
        assert cli_main(["slots", "wait", slot["id"], "--timeout", "30"], client=client) == 0
        player.join(timeout=10)
        done = capsys.readouterr()
        assert json.loads(done.out)["status"] == "ready"
        assert PASTED not in done.out + done.err
        assert [text for _, text in cloud.vm_for(slot)["inputs"]] == [PASTED]


def test_cli_antigravity_wait_without_tty_prints_url_and_hint(
    slot_env, capsys, monkeypatch
) -> None:
    stack, client, cloud = slot_env
    cloud.catalog = AGY_CATALOG
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli_main(["slots", "add", "--provider", "antigravity"], client=client) == 0
    slot = json.loads(capsys.readouterr().out)
    player = _sign_in_as_user(cloud, slot["id"], AGY_URL, approve=False)
    assert cli_main(["slots", "wait", slot["id"], "--timeout", "1"], client=client) == 3
    player.join(timeout=10)
    err = capsys.readouterr().err
    assert AGY_URL in err and "sbx slots code" in err and slot["id"] in err
    assert "None" not in err
    assert cli_main(["slots", "cancel-login", slot["id"]], client=client) == 0
    capsys.readouterr()


def test_cli_grok_device_code_and_harness_follows_slot(slot_env, capsys) -> None:
    stack, client, cloud = slot_env
    cloud.catalog = GROK_CATALOG
    assert cli_main(["slots", "add", "--provider", "grok", "--label", "Grok"], client=client) == 0
    slot = json.loads(capsys.readouterr().out)
    player = _approve(cloud, slot["id"], hold=3.0)
    assert cli_main(["slots", "wait", slot["id"], "--timeout", "30"], client=client) == 0
    player.join(timeout=10)
    captured = capsys.readouterr()
    assert URL in captured.err and CODE in captured.err
    assert CODE not in captured.out

    args = ["sessions", "create", "--slot", slot["id"], "--model", "grok-a", "--effort", "xhigh"]
    assert cli_main(args, client=client) == 0
    session = json.loads(capsys.readouterr().out)["session"]
    assert session["harness"] == {"provider_id": "grok", "model": "grok-a", "effort": "xhigh"}
