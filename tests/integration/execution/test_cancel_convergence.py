"""A cancel always settles, whenever it lands (issue #228 AUD01).

Found by the browser e2e: a Stop pressed while the first Turn was still preparing left it
``cancelling`` forever on main (018a0e0). The cancel re-enqueued ``turn.dispatch`` onto
the dispatch Job that was busy allocating (same dedupe key); that Job then skipped the
no-longer-preparing Turn without settling it. Real workers run in background threads
here, as in the deployed worker pool.
"""

from __future__ import annotations

import threading
import time

import pytest
from tests.support.api import ApiStack, User, inference

SPEC = {"defaults": {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}}}


@pytest.fixture
def env(db, tmp_path):
    stack = ApiStack(db, tmp_path)
    user = User(stack)
    user.connect("inference_api", inference("zen-key-cancel-000000"))
    stack.drain()
    project = user.post(
        f"/api/workspaces/{user.workspace_id}/projects", {"slug": "cancel", "spec": SPEC}
    ).json()
    stop = threading.Event()

    def work() -> None:
        while not stop.is_set():
            if not stack.worker.run_once():
                time.sleep(0.05)

    yield stack, user, project, (stop, work)
    stop.set()
    time.sleep(0.2)
    stack.shutdown()


def state(user: User, turn_id: str) -> str:
    return user.get(f"/api/turns/{turn_id}").json()["turn"]["state"]


def wait(fn, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize("delay", [0.0, 0.3, 0.8, 1.5], ids=["queued", "early", "late", "running"])
def test_cancel_settles_from_any_startup_stage_and_the_queue_moves_on(env, delay) -> None:
    stack, user, project, (stop, work) = env
    created = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {"project_id": project["id"], "message": {"content": "first task [slow:8]"}},
    ).json()
    sid, first = created["session_id"], created["turn_id"]
    second = user.post(f"/api/sessions/{sid}/messages", {"content": "second task"}).json()
    workers = [threading.Thread(target=work, daemon=True) for _ in range(2)]
    for w in workers:
        w.start()
    time.sleep(delay)
    assert user.post(f"/api/turns/{first}/cancellations", {}).status_code in (200, 202)
    assert wait(lambda: state(user, first) in ("cancelled", "interrupted")), state(user, first)
    assert state(user, first) == "cancelled"
    assert wait(lambda: state(user, second["turn_id"]) == "succeeded", timeout=30)
