"""Modal allocation is idempotent by operation identity (deterministic fault injection)."""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any

import pytest
from control.domain.errors import DomainError
from control.executors.modal import ModalExecutor

COMPUTE = {"token_id": "ak-REDACTED", "token_secret": "as-REDACTED"}


class NotFoundError(Exception):
    pass


class AlreadyExistsError(Exception):
    pass


class Cloud:
    """In-memory Modal control plane with injectable faults."""

    def __init__(self) -> None:
        self.sandboxes: list[FakeSandbox] = []
        self.ids = itertools.count(1)
        self.creates = 0
        self.terminated: list[str] = []
        self.fail_list = False
        self.fail_create_before = False
        self.lose_create_response = False
        self.index_lag = 0  # next N list/from_name calls miss the newest sandbox

    def lagging(self) -> bool:
        if self.index_lag > 0:
            self.index_lag -= 1
            return True
        return False


class FakeSandbox:
    def __init__(self, cloud: Cloud, name: str | None, tags: dict[str, str]) -> None:
        self.cloud = cloud
        self.object_id = f"sb-{next(cloud.ids)}"
        self.name = name
        self.tags = dict(tags)
        self.alive = True

    def poll(self) -> int | None:
        return None if self.alive else 0

    def set_tags(self, tags: dict[str, str]) -> None:
        raise AssertionError("tags must be applied atomically at create time")

    def get_tags(self) -> dict[str, str]:
        return dict(self.tags)

    def terminate(self) -> None:
        self.alive = False
        self.cloud.terminated.append(self.object_id)


def make_sdk(cloud: Cloud) -> Any:
    class Sandbox:
        @staticmethod
        def create(*args: str, name: str | None = None, tags: Any = None, **_: Any) -> FakeSandbox:
            cloud.creates += 1
            if cloud.fail_create_before:
                raise ConnectionError("create failed before reaching Modal")
            if name and any(s.name == name and s.alive for s in cloud.sandboxes):
                raise AlreadyExistsError(f"a running sandbox named {name} exists")
            sandbox = FakeSandbox(cloud, name, tags or {})
            cloud.sandboxes.append(sandbox)
            if cloud.lose_create_response:
                raise TimeoutError("response lost after the sandbox was created")
            return sandbox

        @staticmethod
        def list(*, app_id: str, tags: dict[str, str], client: Any) -> list[FakeSandbox]:
            if cloud.fail_list:
                raise ConnectionError("list failed")
            if cloud.lagging():
                return []
            return [s for s in cloud.sandboxes if tags.items() <= s.tags.items()]

        @staticmethod
        def from_name(app_name: str, name: str, *, client: Any) -> FakeSandbox:
            if cloud.lagging():
                raise NotFoundError(name)
            for s in cloud.sandboxes:
                if s.name == name and s.alive:
                    return s
            raise NotFoundError(name)

        @staticmethod
        def from_id(sandbox_id: str, *, client: Any) -> FakeSandbox:
            for s in cloud.sandboxes:
                if s.object_id == sandbox_id:
                    return s
            raise NotFoundError(sandbox_id)

    return SimpleNamespace(
        Sandbox=Sandbox,
        Client=SimpleNamespace(from_credentials=lambda *_: object()),
        App=SimpleNamespace(lookup=lambda *_, **__: SimpleNamespace(app_id="ap-test")),
        exception=SimpleNamespace(NotFoundError=NotFoundError),
    )


@pytest.fixture
def env(monkeypatch):
    cloud = Cloud()
    executor = ModalExecutor(sdk=make_sdk(cloud))
    monkeypatch.setattr(executor, "image", lambda *_: SimpleNamespace(object_id="im-test"))
    return cloud, executor


def spec(op: str = "op_0001fault") -> dict[str, Any]:
    return {
        "workspace_id": "wsp_1",
        "session_id": "sess_1",
        "lease_id": "lease_1",
        "generation": 1,
        "enrollment_key": "REDACTED",
        "compute": COMPUTE,
        "allocation_operation_id": op,
    }


def test_create_applies_name_and_tags_atomically(env) -> None:
    cloud, executor = env
    handle = executor.allocate(spec(), "op_0001fault")
    [sandbox] = cloud.sandboxes
    assert handle["sandbox_id"] == sandbox.object_id and handle["status"] == "running"
    assert sandbox.name == ModalExecutor.sandbox_name("op_0001fault")
    assert sandbox.tags["sbx_alloc"] == "op_0001fault" and sandbox.tags["sbx_lease"] == "lease_1"


def test_lost_create_response_adopts_the_created_sandbox(env) -> None:
    cloud, executor = env
    cloud.lose_create_response = True
    handle = executor.allocate(spec(), "op_0001fault")
    assert len(cloud.sandboxes) == 1 and handle["sandbox_id"] == cloud.sandboxes[0].object_id
    assert executor.lookup("op_0001fault", COMPUTE)["sandbox_id"] == handle["sandbox_id"]


def test_retry_after_success_never_creates_a_second_sandbox(env) -> None:
    cloud, executor = env
    first = executor.allocate(spec(), "op_0001fault")
    second = executor.allocate(spec(), "op_0001fault")
    assert first["sandbox_id"] == second["sandbox_id"]
    assert cloud.creates == 1 and len(cloud.sandboxes) == 1


def test_unique_name_prevents_a_twin_when_tag_listing_misses(env) -> None:
    cloud, executor = env
    first = executor.allocate(spec(), "op_0001fault")
    cloud.index_lag = 2  # both the tag listing and the name lookup miss it once
    second = executor.allocate(spec(), "op_0001fault")
    assert cloud.creates == 2, "the retry did reach create"
    assert len(cloud.sandboxes) == 1, "Modal refused a same-name twin"
    assert second["sandbox_id"] == first["sandbox_id"], "and the refusal was adopted"


def test_lookup_failure_never_creates_blind(env) -> None:
    cloud, executor = env
    cloud.fail_list = True
    with pytest.raises(DomainError) as err:
        executor.lookup("op_0001fault", COMPUTE)
    assert err.value.code == "executor_unavailable" and err.value.retryable
    with pytest.raises(DomainError):
        executor.allocate(spec(), "op_0001fault")
    assert cloud.creates == 0


def test_dead_tagged_sandbox_is_reported_terminated_not_replaced(env) -> None:
    cloud, executor = env
    executor.allocate(spec(), "op_0001fault")
    cloud.sandboxes[0].alive = False
    found = executor.lookup("op_0001fault", COMPUTE)
    assert found["status"] == "terminated" and found["sandbox_id"] == cloud.sandboxes[0].object_id
    again = executor.allocate(spec(), "op_0001fault")
    assert again["status"] == "terminated" and cloud.creates == 1, "never silently replaced"


def test_failed_create_with_nothing_created_is_retryable(env) -> None:
    cloud, executor = env
    cloud.fail_create_before = True
    with pytest.raises(DomainError) as err:
        executor.allocate(spec(), "op_0001fault")
    assert err.value.retryable and cloud.sandboxes == []
    assert executor.lookup("op_0001fault", COMPUTE) is None, "authoritative absence"


def test_terminate_and_describe_confirm_stop(env) -> None:
    cloud, executor = env
    handle = executor.allocate(spec(), "op_0001fault")
    assert executor.describe(handle, COMPUTE)["status"] == "running"
    assert executor.terminate(handle, "op:terminate", COMPUTE) is True
    assert cloud.terminated == [handle["sandbox_id"]]
    assert executor.describe(handle, COMPUTE)["status"] == "terminated"
    assert executor.terminate({"sandbox_id": "sb-missing"}, "op", COMPUTE) is True
