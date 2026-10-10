"""Machine Slots: official-login state machine, one holder per Slot, cleanup, isolation.

The Setup VM backend is scripted here; the real cloud path is covered by the opt-in
``tests/e2e_modal`` checks. No credential ever appears: the backend only reports the
supervisor's state file.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from control.domain.errors import DomainError
from control.integrations.connectors.base import Observation
from tests.support.api import INFERENCE_MODEL, ApiStack, User, inference

MODAL = {"token_id": "ak-test0000000000001", "token_secret": "as-secret00000000000001"}
URL = "https://auth.openai.com/codex/device"
CODE = "ABCD-12345"


CATALOG = {
    "complete": True,
    "items": [
        {
            "id": "model-a",
            "displayName": "Model A",
            "isDefault": True,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low", "description": "Fast"},
                {"reasoningEffort": "medium", "description": "Balanced"},
                {"reasoningEffort": "ultra", "description": "Maximum"},
            ],
        },
        {
            "id": "model-b",
            "displayName": "Model B",
            "defaultReasoningEffort": "low",
            "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Fast"}],
        },
    ],
}


class SetupCloud:
    """In-memory stand-in for the Modal executor's Setup VM and Volume operations."""

    catalog: Any = CATALOG

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.vms: dict[str, dict[str, Any]] = {}
        self.volumes: set[str] = set()
        self.deleted_volumes: list[str] = []
        self.terminated: list[str] = []
        self.starts = 0
        self.allocations: list[dict[str, Any]] = []
        self.synced: list[tuple[str, str]] = []
        self.max_mounts = 0

    def setup_start(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
        assert set(spec["compute"]) >= {"token_id", "token_secret"}
        existing = self.lookup(operation_id, spec["compute"])
        if existing:
            return existing
        self.starts += 1
        vm = {
            "sandbox_id": f"sb-{next(self.ids)}",
            "operation_id": operation_id,
            "spec": spec,
            "alive": True,
            "state": {"phase": "starting", "mode": spec["setup"]["mode"]},
        }
        self.vms[vm["sandbox_id"]] = vm
        self.volumes.add(spec["volume_name"])
        return {"sandbox_id": vm["sandbox_id"], "operation_id": operation_id, "status": "running"}

    def lookup(self, operation_id: str, compute: Any) -> dict[str, Any] | None:
        for vm in self.vms.values():
            if vm["operation_id"] == operation_id:
                return {
                    "sandbox_id": vm["sandbox_id"],
                    "operation_id": operation_id,
                    "status": "running" if vm["alive"] else "terminated",
                }
        return None

    def setup_observe(self, handle: dict[str, Any], compute: Any, path: str) -> dict[str, Any]:
        vm = self.vms[handle["sandbox_id"]]
        if not vm["alive"]:
            return {"status": "terminated", "state": None}
        return {"status": "running", "state": dict(vm["state"])}

    def terminate(self, handle: dict[str, Any], operation_id: str, compute: Any) -> bool:
        self.vms[handle["sandbox_id"]]["alive"] = False
        self.terminated.append(handle["sandbox_id"])
        return True

    # -- Worker allocation (the runtime never becomes reachable in these tests) -----------
    def allocate(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
        self.allocations.append(spec)
        vm = {
            "sandbox_id": f"sb-{next(self.ids)}",
            "operation_id": operation_id,
            "spec": {"tags": {"sbx_slot": None}},
            "alive": True,
            "state": {},
        }
        self.vms[vm["sandbox_id"]] = vm
        vm["volume"] = (spec.get("profile") or {}).get("volume_name")
        mounted = [v for v in self.vms.values() if v["alive"] and v.get("volume") == vm["volume"]]
        if vm["volume"]:
            self.max_mounts = max(self.max_mounts, len(mounted))
        return {
            "sandbox_id": vm["sandbox_id"],
            "operation_id": operation_id,
            "lease_id": spec["lease_id"],
            "status": "running",
        }

    def describe(self, handle: dict[str, Any], compute: Any) -> dict[str, Any]:
        alive = self.vms[handle["sandbox_id"]]["alive"]
        return {"status": "running" if alive else "terminated"}

    def connect_runtime(self, handle: dict[str, Any], compute: Any) -> str:
        raise DomainError("executor_unavailable", "runtime not reachable yet", retryable=True)

    def profile_sync(self, handle: dict[str, Any], compute: Any, mount: str) -> bool:
        self.synced.append((handle["sandbox_id"], mount))
        return True

    def volume_delete(self, compute: Any, name: str) -> bool:
        self.volumes.discard(name)
        self.deleted_volumes.append(name)
        return True

    # -- scripting ---------------------------------------------------------------------
    def vm_for(self, slot: dict[str, Any]) -> dict[str, Any]:
        return next(
            vm
            for vm in reversed(list(self.vms.values()))
            if vm["spec"]["tags"]["sbx_slot"] == slot["id"]
        )

    def show_code(self, slot: dict[str, Any]) -> None:
        self.vm_for(slot)["state"].update(
            phase="awaiting_user",
            verification_url=URL,
            user_code=CODE,
            code_expires_at="2099-01-01T00:00:00+00:00",
        )

    def finish(self, slot: dict[str, Any], phase: str, error: str | None = None) -> None:
        state = self.vm_for(slot)["state"]
        state.pop("user_code", None)
        state.update(phase=phase, error=error)
        if phase == "succeeded":
            state.update(
                cli_version="codex-cli 0.162.0",
                real_model_call=True,
                profile_synced=True,
                catalog=self.catalog,
            )

    @property
    def running(self) -> list[str]:
        return [vm["sandbox_id"] for vm in self.vms.values() if vm["alive"]]


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
    assert waiting["login"]["code_expires_at"].startswith("2099-01-01")

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
    assert unknown.json()["error"]["details"]["supported"] == ["codex"]


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
