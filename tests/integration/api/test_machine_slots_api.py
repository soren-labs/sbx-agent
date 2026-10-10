"""Machine Slots: official-login state machine, one holder per Slot, cleanup, isolation.

The Setup VM backend is scripted here; the real cloud path is covered by the opt-in
``tests/e2e_modal`` checks. No credential ever appears: the backend only reports the
supervisor's state file.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from tests.support.api import ApiStack, User

MODAL = {"token_id": "ak-test0000000000001", "token_secret": "as-secret00000000000001"}
URL = "https://auth.openai.com/codex/device"
CODE = "ABCD-12345"


class SetupCloud:
    """In-memory stand-in for the Modal executor's Setup VM and Volume operations."""

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.vms: dict[str, dict[str, Any]] = {}
        self.volumes: set[str] = set()
        self.deleted_volumes: list[str] = []
        self.terminated: list[str] = []
        self.starts = 0

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
            state.update(cli_version="codex-cli 0.162.0", real_model_call=True, profile_synced=True)

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
