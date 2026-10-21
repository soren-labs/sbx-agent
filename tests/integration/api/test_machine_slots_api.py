"""Machine Slots: official-login state machine, one holder per Slot, cleanup, isolation.

The Setup VM backend is scripted here; the real cloud path is covered by the opt-in
``tests/e2e_modal`` checks. No credential ever appears: the backend only reports the
supervisor's state file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from control.integrations.connectors.base import Observation
from tests.support.api import INFERENCE_MODEL, ApiStack, User, inference
from tests.support.slots import AGY_CATALOG, CODE, GROK_CATALOG, MODAL, URL, SetupCloud


@pytest.fixture
def env(db, tmp_path):
    cloud = SetupCloud()
    stack = ApiStack(db, tmp_path, executors={"modal": cloud})
    stack.services.slots.poll_seconds = 0.0
    yield stack, cloud
    stack.shutdown()


def _user(stack: ApiStack) -> User:
    user = User(stack)
    user.connect("modal", MODAL)
    stack.drain()
    return user


def _add(user: User, **body: Any) -> dict[str, Any]:
    r = user.post(
        f"/api/workspaces/{user.workspace_id}/machine-slots", {"provider": "codex", **body}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _get(user: User, slot: dict[str, Any]) -> dict[str, Any]:
    return user.get(f"/api/machine-slots/{slot['id']}").json()


def _login(stack: ApiStack, cloud: SetupCloud, user: User, **body: Any) -> dict[str, Any]:
    slot = _add(user, **body)
    stack.drain()
    cloud.show_code(slot)
    stack.drain()
    cloud.finish(slot, "succeeded")
    stack.drain()
    return _get(user, slot)


def test_official_login_flows_to_ready_without_a_second_step(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _add(user, label="Work laptop", account_alias="team plan")
    assert slot["status"] == "login_pending" and slot["login"]["state"] == "starting"
    assert slot["login"]["user_code"] is None and slot["volume"]["managed"] is True
    stack.drain()
    vm = cloud.vm_for(slot)
    assert vm["spec"]["volume_name"] == slot["volume"]["name"] and vm["spec"]["mount"] == "/profile"
    assert vm["spec"]["setup"]["mode"] == "login"
    assert vm["spec"]["setup"]["login_argv"][-2:] == ["login", "--device-auth"]
    assert vm["spec"]["env"]["CODEX_HOME"] == "/profile/.codex"
    assert not any("token" in key.lower() for key in vm["spec"]["env"]), "no credential is injected"

    cloud.show_code(slot)
    stack.drain()
    waiting = _get(user, slot)
    assert waiting["status"] == "login_pending" and waiting["login"]["state"] == "awaiting_user"
    assert waiting["login"]["verification_url"] == URL and waiting["login"]["user_code"] == CODE
    # Compare the instant, not the rendered offset: PostgreSQL returns timestamps in the
    # server's session timezone, so the same moment may read back as 2098-12-31T19:00-05:00.
    expires = datetime.fromisoformat(waiting["login"]["code_expires_at"])
    assert expires == datetime(2099, 1, 1, tzinfo=UTC)

    cloud.vm_for(slot)["state"].update(phase="verifying")
    cloud.vm_for(slot)["state"].pop("user_code")
    stack.drain()
    assert _get(user, slot)["login"]["user_code"] is None

    cloud.finish(slot, "succeeded")
    stack.drain()
    ready = _get(user, slot)
    assert ready["status"] == "ready" and ready["state"] == "ready" and ready["busy"] is False
    assert ready["login"]["state"] == "succeeded" and ready["login"]["user_code"] is None
    assert ready["login"]["verification_url"] is None
    assert ready["capabilities"]["cli_version"] == "codex-cli 0.162.0"
    assert ready["capabilities"]["verification"]["real_model_call"] is True
    assert ready["verified_at"] and cloud.running == [], "the Setup VM is destroyed"
    stored = stack.db.read(lambda u: u.find("slot_login_attempts", {"slot_id": slot["id"]}))
    assert [a["user_code"] for a in stored] == [None], "the used code is not retained"
    assert [a["verification_url"] for a in stored] == [None], "nor is the link it was shown with"
    listing = user.get(f"/api/workspaces/{user.workspace_id}/machine-slots").json()
    assert listing["summary"] == {
        "total": 1,
        "running": 0,
        "ready": 1,
        "login_pending": 0,
        "needs_attention": 0,
    }
    assert listing["providers"][0]["provider_id"] == "codex"
    assert CODE not in user.get(f"/api/workspaces/{user.workspace_id}/machine-slots").text


def test_same_account_twice_is_two_independent_slots_and_volumes(env) -> None:
    stack, cloud = env
    user = _user(stack)
    a1 = _login(stack, cloud, user, label="A1", account_alias="account A")
    a2 = _login(stack, cloud, user, label="A2", account_alias="account A")
    assert a1["status"] == a2["status"] == "ready"
    assert a1["volume"]["name"] != a2["volume"]["name"]
    assert cloud.starts == 2 and len(cloud.volumes) == 2
    reused = user.post(
        f"/api/workspaces/{user.workspace_id}/machine-slots",
        {"provider": "codex", "volume_name": a1["volume"]["name"]},
    )
    assert reused.status_code == 422, "one Volume never backs two Slots"


def test_denied_and_expired_logins_need_login_and_can_retry(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _add(user)
    stack.drain()
    cloud.show_code(slot)
    stack.drain()
    cloud.finish(slot, "failed", "login_denied")
    stack.drain()
    denied = _get(user, slot)
    assert denied["status"] == "needs_login" and denied["state_reason"] == "login_denied"
    assert denied["login"]["state"] == "failed" and denied["login"]["user_code"] is None
    assert cloud.running == []

    retry = user.post(f"/api/machine-slots/{slot['id']}/logins")
    assert retry.status_code == 202 and retry.json()["status"] == "login_pending"
    assert retry.json()["login"]["attempt_id"] != denied["login"]["attempt_id"]
    stack.drain()
    cloud.finish(slot, "expired", "code_expired")
    stack.drain()
    expired = _get(user, slot)
    assert expired["status"] == "needs_login" and expired["login"]["state"] == "expired"
    assert cloud.running == [] and cloud.starts == 2


def test_login_window_is_enforced_by_the_control_plane_too(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _add(user)
    stack.drain()
    stack.db.run(
        lambda u: u.update_where(
            "slot_login_attempts",
            {"slot_id": slot["id"]},
            {"deadline_at": u.now()},
        )
    )
    stack.drain()
    after = _get(user, slot)
    assert after["login"]["state"] == "expired" and cloud.running == []


def test_cancel_stops_the_setup_vm_and_frees_the_slot(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _add(user)
    stack.drain()
    cloud.show_code(slot)
    stack.drain()
    busy = user.post(f"/api/machine-slots/{slot['id']}/logins")
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "invalid_transition"
    cancelled = user.delete(f"/api/machine-slots/{slot['id']}/logins/current")
    assert cancelled.status_code == 202
    stack.drain()
    after = _get(user, slot)
    assert after["login"]["state"] == "cancelled" and after["status"] == "needs_login"
    assert cloud.running == []
    assert user.delete(f"/api/machine-slots/{slot['id']}/logins/current").status_code == 409


def test_failed_relogin_keeps_the_previous_login(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _login(stack, cloud, user)
    assert user.post(f"/api/machine-slots/{slot['id']}/logins").status_code == 202
    stack.drain()
    cloud.finish(slot, "expired", "code_expired")
    stack.drain()
    after = _get(user, slot)
    assert after["status"] == "ready" and after["state_reason"] == "relogin_expired"


def test_verification_detects_a_login_that_stopped_working(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _login(stack, cloud, user)
    assert user.post(f"/api/machine-slots/{slot['id']}/verifications").status_code == 202
    stack.drain()
    assert cloud.vm_for(slot)["spec"]["setup"]["mode"] == "verify"
    cloud.finish(slot, "failed", "not_logged_in")
    stack.drain()
    after = _get(user, slot)
    assert after["status"] == "needs_login" and after["state_reason"] == "not_logged_in"


def test_vanished_setup_vm_is_an_error_not_a_stuck_pending(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _add(user)
    stack.drain()
    cloud.vm_for(slot)["alive"] = False
    stack.drain()
    after = _get(user, slot)
    assert after["status"] == "error" and after["state_reason"] == "setup_vm_lost"
    assert after["login"]["state"] == "failed"


def test_adopted_volume_is_verified_and_never_deleted(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _add(user, label="Imported", volume_name="existing-profile-a1")
    assert slot["volume"] == {
        "name": "existing-profile-a1",
        "filesystem": "modal_volume_v2",
        "managed": False,
    }
    stack.drain()
    assert cloud.vm_for(slot)["spec"]["setup"]["mode"] == "verify"
    cloud.finish(slot, "succeeded")
    stack.drain()
    assert _get(user, slot)["status"] == "ready"
    assert user.post(f"/api/machine-slots/{slot['id']}/logout").status_code == 422
    assert user.delete(f"/api/machine-slots/{slot['id']}?confirm=Imported").status_code == 202
    stack.drain()
    assert cloud.deleted_volumes == [] and "existing-profile-a1" in cloud.volumes
    assert user.get(f"/api/machine-slots/{slot['id']}").status_code == 404


def test_rename_logout_and_confirmed_delete(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _login(stack, cloud, user, label="Old name")
    renamed = user.patch(
        f"/api/machine-slots/{slot['id']}", {"label": "Build box", "account_alias": None}
    ).json()
    assert renamed["label"] == "Build box" and renamed["version"] > slot["version"]
    assert user.patch(f"/api/machine-slots/{slot['id']}", {"label": " "}).status_code == 422

    assert user.post(f"/api/machine-slots/{slot['id']}/logout").status_code == 202
    stack.drain()
    out = _get(user, slot)
    assert out["status"] == "needs_login" and out["state_reason"] == "logged_out"
    assert cloud.deleted_volumes == [slot["volume"]["name"]] and out["verified_at"] is None

    assert user.delete(f"/api/machine-slots/{slot['id']}").status_code == 422
    assert user.delete(f"/api/machine-slots/{slot['id']}?confirm=Old name").status_code == 422
    assert user.delete(f"/api/machine-slots/{slot['id']}?confirm=Build box").status_code == 202
    stack.drain()
    assert user.get(f"/api/machine-slots/{slot['id']}").status_code == 404
    assert user.get(f"/api/workspaces/{user.workspace_id}/machine-slots").json()["items"] == []


def test_delete_during_login_cancels_and_reclaims_everything(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _add(user, label="Temp")
    stack.drain()
    cloud.show_code(slot)
    stack.drain()
    assert user.delete(f"/api/machine-slots/{slot['id']}?confirm=Temp").status_code == 202
    stack.drain()
    assert cloud.running == [] and cloud.deleted_volumes == [slot["volume"]["name"]]
    attempt = stack.db.read(lambda u: u.find_one("slot_login_attempts", {"slot_id": slot["id"]}))
    assert attempt["state"] == "cancelled" and attempt["user_code"] is None


def test_slots_are_private_to_their_owner_and_pin_the_modal_connection(env) -> None:
    stack, cloud = env
    owner, other = _user(stack), _user(stack)
    slot = _login(stack, cloud, owner)
    for response in (
        other.get(f"/api/machine-slots/{slot['id']}"),
        other.patch(f"/api/machine-slots/{slot['id']}", {"label": "mine"}),
        other.post(f"/api/machine-slots/{slot['id']}/logins"),
        other.post(f"/api/machine-slots/{slot['id']}/logout"),
        other.delete(f"/api/machine-slots/{slot['id']}?confirm={slot['label']}"),
        other.get(f"/api/workspaces/{owner.workspace_id}/machine-slots"),
    ):
        assert response.status_code == 404, response.text
    foreign = other.post(
        f"/api/workspaces/{other.workspace_id}/machine-slots",
        {"provider": "codex", "compute_connection_id": slot["compute_connection_id"]},
    )
    assert foreign.status_code == 404, "another owner's Modal connection cannot host a slot"
    refused = owner.delete(f"/api/connections/{slot['compute_connection_id']}")
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["machine_slots"] == [slot["id"]]


def test_slots_need_a_verified_modal_connection_and_a_known_provider(env) -> None:
    stack, _ = env
    user = User(stack)
    path = f"/api/workspaces/{user.workspace_id}/machine-slots"
    missing = user.post(path, {"provider": "codex"})
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "connection_required"
    unknown = user.post(path, {"provider": "claude"})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"]["supported"] == ["antigravity", "codex", "grok"]


def test_slots_are_unavailable_without_the_modal_executor(db, tmp_path) -> None:
    stack = ApiStack(db, tmp_path)
    try:
        user = _user(stack)
        r = user.post(f"/api/workspaces/{user.workspace_id}/machine-slots", {"provider": "codex"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_capability"
        providers = user.get("/api/subscription-providers").json()["items"]
        assert providers[0]["available"] is False
    finally:
        stack.shutdown()


# -- Providers ------------------------------------------------------------------------------

GOOGLE_URL = "https://accounts.google.com/o/oauth2/auth?client_id=x&state=y&" + "s" * 700
PASTED = "REDACTED-authorization-code"


def test_three_providers_each_with_its_official_login_method(env) -> None:
    stack, _ = env
    user = _user(stack)
    providers = {
        p["provider_id"]: p for p in user.get("/api/subscription-providers").json()["items"]
    }
    assert list(providers) == ["codex", "antigravity", "grok"]
    assert providers["codex"]["login_method"] == "device_code"
    assert providers["grok"]["login_method"] == "device_code"
    assert providers["grok"]["verification_host"] == "accounts.x.ai"
    assert providers["antigravity"]["login_method"] == "authorization_code"
    assert providers["antigravity"]["verification_host"] == "accounts.google.com"
    assert all(p["available"] for p in providers.values())


def test_grok_device_login_uses_its_own_private_home(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _add(user, provider="grok")
    assert slot["label"] == "Grok Build 1" and slot["provider_name"] == "Grok Build"
    assert slot["login"]["method"] == "device_code"
    stack.drain()
    spec = cloud.vm_for(slot)["spec"]
    assert spec["setup"]["login_argv"] == ["grok", "login", "--device-auth"]
    assert spec["env"]["GROK_HOME"] == "/profile/.grok" and spec["env"]["HOME"] == "/profile"
    assert not any("key" in k.lower() or "token" in k.lower() for k in spec["env"])
    cloud.show_code(slot)
    stack.drain()
    waiting = _get(user, slot)["login"]
    assert waiting["user_code"] == CODE and waiting["input"] is None
    refused = user.post(f"/api/machine-slots/{slot['id']}/logins/current/code", {"code": PASTED})
    assert refused.status_code == 409, "a device login has no code to send back"
    cloud.finish(slot, "succeeded")
    stack.drain()
    assert _get(user, slot)["status"] == "ready" and cloud.running == []


def test_antigravity_login_relays_the_pasted_code_without_keeping_it(env) -> None:
    stack, cloud = env
    user, other = _user(stack), _user(stack)
    slot = _add(user, provider="antigravity")
    path = f"/api/machine-slots/{slot['id']}/logins/current/code"
    assert slot["login"]["method"] == "authorization_code" and slot["login"]["input"] is None
    early = user.post(path, {"code": PASTED})
    assert early.status_code == 409, "nothing is waiting for a code yet"
    stack.drain()
    spec = cloud.vm_for(slot)["spec"]
    assert spec["setup"]["login_argv"] == ["agy"] and spec["setup"]["pty"]["input"]
    assert spec["env"]["HOME"] == "/profile"

    cloud.show_url(slot, GOOGLE_URL)
    stack.drain()
    waiting = _get(user, slot)["login"]
    assert waiting["state"] == "awaiting_user" and waiting["input"] == "authorization_code"
    assert waiting["verification_url"] == GOOGLE_URL, "the long sign-in URL is not truncated"
    assert waiting["user_code"] is None

    assert other.post(path, {"code": PASTED}).status_code == 404
    for bad in ({}, {"code": ""}, {"code": "short"}, {"code": "has space in it"}, {"code": 7}):
        assert user.post(path, bad).status_code == 422
    assert "inputs" not in cloud.vm_for(slot)

    cloud.reachable = False
    unreachable = user.post(path, {"code": PASTED})
    assert unreachable.json()["error"]["code"] == "executor_unavailable"
    assert unreachable.json()["error"]["retryable"] is True
    cloud.reachable = True
    sent = user.post(path, {"code": f"  {PASTED}\n"})
    assert sent.status_code == 202, sent.text
    assert cloud.vm_for(slot)["inputs"] == [("/tmp/sbx-setup/login-input", PASTED)]
    assert PASTED not in sent.text

    # The code exists only inside the Setup VM: no table row, audit entry or Job carries it.
    def dump(uow: Any) -> str:
        tables = (
            "slot_login_attempts",
            "machine_slots",
            "jobs",
            "audit_records",
            "command_deduplication",
        )
        return repr([uow.find(table, {}) for table in tables])

    assert PASTED not in stack.db.read(dump)

    cloud.vm_for(slot)["state"].update(phase="verifying")
    cloud.vm_for(slot)["state"].pop("input")
    stack.drain()
    checking = _get(user, slot)["login"]
    assert checking["state"] == "verifying" and checking["input"] is None
    assert user.post(path, {"code": PASTED}).status_code == 409

    cloud.finish(slot, "failed", "code_rejected")
    stack.drain()
    rejected = _get(user, slot)
    assert rejected["status"] == "needs_login" and rejected["state_reason"] == "code_rejected"
    assert cloud.running == []

    again = user.post(f"/api/machine-slots/{slot['id']}/logins")
    assert again.status_code == 202
    stack.drain()
    cloud.show_url(slot, GOOGLE_URL)
    stack.drain()
    assert user.post(path, {"code": PASTED}).status_code == 202
    cloud.finish(slot, "succeeded")
    stack.drain()
    ready = _get(user, slot)
    assert ready["status"] == "ready" and ready["login"]["verification_url"] is None


def test_providers_and_accounts_never_share_a_volume_or_a_home(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slots = [
        _add(user, provider=p) for p in ("codex", "antigravity", "grok", "grok", "antigravity")
    ]
    stack.drain()
    volumes = [slot["volume"]["name"] for slot in slots]
    assert len(set(volumes)) == 5
    assert [cloud.vm_for(slot)["spec"]["volume_name"] for slot in slots] == volumes
    assert [slot["label"] for slot in slots] == [
        "Codex 1",
        "Antigravity 1",
        "Grok Build 1",
        "Grok Build 2",
        "Antigravity 2",
    ]


# -- Sessions on a Slot ---------------------------------------------------------------------


def _session(user: User, slot: dict[str, Any] | None, **overrides: Any) -> Any:
    body: dict[str, Any] = {
        "harness": {"provider_id": "codex"},
        "executor": {"backend": "modal"},
        "message": {"content": "hello"},
    }
    if slot is not None:
        body["inference"] = {"mode": "subscription", "machine_slot_id": slot["id"]}
    for key, value in overrides.items():
        body[key] = {**body.get(key, {}), **value} if isinstance(value, dict) else value
    return user.post(f"/api/workspaces/{user.workspace_id}/sessions", body)


def test_slot_catalog_is_the_cli_answer_with_its_provenance(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _login(stack, cloud, user)
    catalog = slot["capabilities"]["catalog"]
    assert catalog["status"] == "ready" and catalog["source"] == "codex app-server model/list"
    assert catalog["cli_version"] == "codex-cli 0.162.0" and catalog["observed_at"]
    assert [m["id"] for m in catalog["models"]] == ["model-a", "model-b"]
    assert catalog["default_model"] == "model-a"
    assert [e["id"] for e in catalog["models"][1]["reasoning"]["efforts"]] == ["low"]

    cloud.catalog = None  # the CLI stops answering: nothing is guessed
    user.post(f"/api/machine-slots/{slot['id']}/verifications")
    stack.drain()
    cloud.finish(slot, "succeeded")
    stack.drain()
    refreshed = _get(user, slot)["capabilities"]["catalog"]
    assert refreshed["status"] == "unavailable" and refreshed["models"] == []
    refused = _session(user, slot, harness={"model": "model-a"})
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["capability"] == "model_discovery"
    assert _session(user, slot).status_code == 202, "the provider default still works"


def test_subscription_session_pins_only_catalogued_models_and_their_efforts(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _login(stack, cloud, user)
    created = _session(user, slot, harness={"model": "model-a", "effort": "ultra"})
    assert created.status_code == 202, created.text
    session = created.json()["session"]
    assert session["inference"] == {"mode": "subscription", "machine_slot_id": slot["id"]}
    assert session["harness"] == {"provider_id": "codex", "model": "model-a", "effort": "ultra"}
    assert session["connections"]["inference"] is None, "no API key is attached"
    assert session["connections"]["compute"] == slot["compute_connection_id"]

    invented = _session(user, slot, harness={"model": "gpt-made-up"})
    assert invented.status_code == 422
    assert invented.json()["error"]["details"]["available"] == ["model-a", "model-b"]
    wrong = _session(user, slot, harness={"model": "model-b", "effort": "ultra"})
    assert wrong.status_code == 422
    assert wrong.json()["error"]["details"]["supported"] == ["low"]
    default_model = _session(user, slot, harness={"effort": "ultra"})
    assert default_model.status_code == 202, "an effort alone is checked against the default model"

    turn = user.post(
        f"/api/sessions/{session['id']}/messages",
        {"content": "again", "settings": {"model": "model-b"}},
    )
    assert turn.status_code == 422, "the pinned ultra effort is not valid for model-b"
    ok = user.post(
        f"/api/sessions/{session['id']}/messages",
        {"content": "again", "settings": {"model": "model-b", "effort": "low"}},
    )
    assert ok.status_code in (200, 201, 202), ok.text


def test_subscription_session_prerequisites_and_no_api_fallback(env) -> None:
    stack, cloud = env
    user = _user(stack)
    key = user.connect("inference_api", inference("inference-live-like-key-ABCDEFGHIJ0123"))
    stack.drain()
    slot = _login(stack, cloud, user)
    both = _session(user, slot, connections={"inference": key["id"]})
    assert both.status_code == 422, "never a slot and an API key together"
    assert _session(user, slot, harness={"provider_id": "opencode"}).status_code == 422
    assert _session(user, slot, executor={"backend": "local"}).status_code == 422
    other = _user(stack)
    stolen = other.post(
        f"/api/workspaces/{other.workspace_id}/sessions",
        {
            "harness": {"provider_id": "codex"},
            "executor": {"backend": "modal"},
            "inference": {"mode": "subscription", "machine_slot_id": slot["id"]},
        },
    )
    assert stolen.status_code == 404, "another owner cannot run on this slot"

    pending = _add(user, label="Not logged in")
    not_ready = _session(user, pending)
    assert not_ready.status_code == 409
    assert not_ready.json()["error"]["code"] == "connection_required"
    custom = _session(user, None, harness={"provider_id": "opencode", "effort": "high"})
    assert custom.status_code == 422, "no unverified effort is accepted for a custom API model"
    assert custom.json()["error"]["details"]["capability"] == "effort_settings"


def test_one_worker_per_slot_and_release_syncs_then_frees_it(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _login(stack, cloud, user)
    sessions = {s["session_id"]: s for s in (_session(user, slot).json() for _ in range(2))}
    stack.drain(40)
    assert len(cloud.allocations) == 1, "only one VM may mount the slot volume"
    spec = cloud.allocations[0]
    assert spec["profile"]["volume_name"] == slot["volume"]["name"]
    assert spec["profile"]["mount"] == "/profile"
    holder = sessions.pop(spec["session_id"])
    [waiter] = sessions.values()
    busy = _get(user, slot)
    assert busy["status"] == "running" and busy["busy"] is True
    assert busy["worker"]["session_id"] == holder["session_id"]
    waiting = stack.db.read(lambda u: u.get("turns", waiter["turn_id"]))
    assert waiting["state"] == "preparing" and waiting["reason"] == "waiting_capacity"
    assert user.post(f"/api/machine-slots/{slot['id']}/logins").status_code == 409
    assert (
        user.delete(f"/api/machine-slots/{slot['id']}?confirm={slot['label']}").status_code == 409
    )

    released = user.post(f"/api/sessions/{holder['session_id']}/executor/releases")
    assert released.status_code in (200, 202), released.text
    stack.drive(lambda: len(cloud.allocations) == 2, timeout=30)
    assert cloud.synced and cloud.synced[0][1] == "/profile", "the volume is committed first"
    assert cloud.synced[0][0] in cloud.terminated
    # Whichever Session is dispatched next takes the freed slot; never two at once.
    assert cloud.max_mounts == 1 and len(cloud.running) == 1
    after = _get(user, slot)
    assert after["busy"] is True
    assert after["worker"]["session_id"] == cloud.allocations[1]["session_id"]


def test_custom_api_session_never_mounts_a_slot_volume(env) -> None:
    stack, cloud = env
    user = _user(stack)
    user.connect("inference_api", inference("inference-live-like-key-ABCDEFGHIJ0123"))
    stack.drain()
    _login(stack, cloud, user)
    created = _session(user, None, harness={"provider_id": "opencode"})
    assert created.status_code == 202, created.text
    assert created.json()["session"]["inference"] == {"mode": "custom_api", "machine_slot_id": None}
    stack.drain(40)
    assert "profile" not in cloud.allocations[0]


def test_custom_api_thinking_toggle_needs_a_measured_probe_and_a_verified_harness(env) -> None:
    """DeepSeek-style binary thinking: only ``none``, only where both facts were measured."""
    stack, _ = env

    def validator(material: dict[str, Any], **_: Any) -> Observation:
        catalog = {
            "models": [
                {"id": INFERENCE_MODEL, "reasoning": {"openai_chat": "toggle"}},
                {"id": "plain-model", "reasoning": {"openai_chat": "none"}},
                {"id": "odd-model", "reasoning": {"openai_chat": "unverified"}},
            ],
            "preferred_model": INFERENCE_MODEL,
            "protocols": list(material["endpoints"]),
        }
        return Observation("ready", catalog=catalog, quota_consuming=True)

    stack.services.connections.validators["inference_api"] = validator
    user = _user(stack)
    user.connect("inference_api", inference("inference-live-like-key-ABCDEFGHIJ0123"))
    stack.drain()

    def create(**harness: Any) -> Any:
        return user.post(
            f"/api/workspaces/{user.workspace_id}/sessions",
            {"harness": {"provider_id": "opencode", **harness}, "executor": {"backend": "local"}},
        )

    off = create(effort="none")
    assert off.status_code == 201, off.text
    assert off.json()["session"]["harness"]["effort"] == "none"
    graded = create(effort="high")
    assert graded.status_code == 422, "no invented low/medium/high for a binary control"
    assert graded.json()["error"]["details"]["supported"] == ["none"]
    for model in ("plain-model", "odd-model"):
        refused = create(model=model, effort="none")
        assert refused.status_code == 422, model
        assert refused.json()["error"]["details"]["supported"] == []
    unverified_harness = create(provider_id="grok", effort="none")
    assert unverified_harness.status_code == 422
    assert unverified_harness.json()["error"]["details"]["capability"] == "effort_settings"


# -- Sessions on Grok Build and Antigravity Slots -----------------------------------------


def _ready(stack: ApiStack, cloud: SetupCloud, user: User, provider: str, catalog: Any) -> dict:
    cloud.catalog = catalog
    return _login(stack, cloud, user, provider=provider)


def test_grok_slot_runs_grok_sessions_with_each_models_own_efforts(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _ready(stack, cloud, user, "grok", GROK_CATALOG)
    catalog = slot["capabilities"]["catalog"]
    assert catalog["status"] == "ready" and catalog["source"] == "grok agent stdio initialize"
    assert catalog["default_model"] == "grok-a"
    assert [m["id"] for m in catalog["models"]] == ["grok-a", "grok-b"]

    created = _session(
        user, slot, harness={"provider_id": "grok", "model": "grok-a", "effort": "xhigh"}
    )
    assert created.status_code == 202, created.text
    session = created.json()["session"]
    assert session["harness"] == {"provider_id": "grok", "model": "grok-a", "effort": "xhigh"}
    assert session["connections"]["inference"] is None, "no API key is attached"
    stack.drain()
    [allocation] = cloud.allocations
    profile = allocation["profile"]
    assert profile["volume_name"] == slot["volume"]["name"] and profile["mount"] == "/profile"
    assert profile["provider_id"] == "grok" and profile["env"]["GROK_HOME"] == "/profile/.grok"

    no_menu = _session(
        user, slot, harness={"provider_id": "grok", "model": "grok-b", "effort": "high"}
    )
    assert no_menu.status_code == 422, "grok-b lists no efforts"
    key = user.connect("inference_api", inference("inference-live-like-key-ABCDEFGHIJ0123"))
    invented = _session(user, slot, harness={"provider_id": "grok", "model": "grok-9000"})
    assert invented.json()["error"]["details"]["available"] == ["grok-a", "grok-b"]
    wrong_harness = _session(user, slot)
    assert wrong_harness.status_code == 422
    assert wrong_harness.json()["error"]["details"]["expected"] == "grok"
    with_key = _session(
        user,
        slot,
        harness={"provider_id": "grok"},
        connections={"inference": key["id"]},
    )
    assert with_key.status_code == 422, "a subscription Session never also takes an API key"


def test_antigravity_slot_offers_the_printed_variants_and_no_separate_effort(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _ready(stack, cloud, user, "antigravity", AGY_CATALOG)
    catalog = slot["capabilities"]["catalog"]
    assert catalog["source"] == "agy models" and "effort_scope" not in catalog
    assert [(m["id"], m["name"]) for m in catalog["models"]] == [
        ("agy-pro-1-high", "Agy Pro 1 (High)"),
        ("agy-pro-1-low", "Agy Pro 1 (Low)"),
        ("agy-plain-2", "Agy Plain 2"),
    ]
    assert catalog["default_model"] is None

    harness = {"provider_id": "antigravity", "model": "agy-pro-1-low"}
    created = _session(user, slot, harness=harness)
    assert created.status_code == 202, created.text
    assert created.json()["session"]["harness"] == {**harness, "effort": None}
    # The CLI refuses an effort next to a variant, so the server never lets one through.
    conflict = _session(user, slot, harness={**harness, "model": "agy-pro-1-high", "effort": "low"})
    assert conflict.status_code == 422
    assert conflict.json()["error"]["details"] == {
        "field": "harness.effort",
        "model": "agy-pro-1-high",
        "supported": [],
    }
    assert (
        _session(user, slot, harness={"provider_id": "antigravity", "effort": "low"}).status_code
        == 422
    )
    assert _session(user, slot, harness={**harness, "model": "gemini-made-up"}).status_code == 422
    turn = user.post(
        f"/api/sessions/{created.json()['session']['id']}/messages",
        {"content": "again", "settings": {"effort": "high"}},
    )
    assert turn.status_code == 422, "a per-Turn effort is refused the same way"


def test_antigravity_without_a_readable_model_list_runs_the_provider_default_only(env) -> None:
    stack, cloud = env
    user = _user(stack)
    slot = _ready(stack, cloud, user, "antigravity", {"lines": ["Gemini Pro (preview)"]})
    assert slot["status"] == "ready"
    assert slot["capabilities"]["catalog"]["status"] == "unavailable"
    assert slot["capabilities"]["catalog"]["models"] == []
    default = _session(user, slot, harness={"provider_id": "antigravity"})
    assert default.status_code == 202, default.text
    guessed = _session(user, slot, harness={"provider_id": "antigravity", "model": "gemini-pro"})
    assert guessed.status_code == 422
    assert guessed.json()["error"]["code"] == "unsupported_capability"


def test_antigravity_has_no_custom_api_mode(env) -> None:
    stack, _ = env
    user = _user(stack)
    user.connect("inference_api", inference("inference-live-like-key-ABCDEFGHIJ0123"))
    stack.drain()
    refused = _session(user, None, harness={"provider_id": "antigravity"})
    assert refused.status_code in (409, 422), refused.text
    assert "sess" not in refused.text
