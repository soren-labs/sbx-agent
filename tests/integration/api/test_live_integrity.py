"""Live file and terminal integrity: partial views never replace files, and terminal
commands keyed by Idempotency-Key run at most once across lost responses (issue #227)."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import pytest
from control.application.ports import RuntimeUnavailable
from tests.support.api import ApiStack, User, inference

SPEC = {"defaults": {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}}}
TAIL = "TAIL-MARKER-227\n"


@pytest.fixture
def env(db, tmp_path):
    stack = ApiStack(db, tmp_path)
    user = User(stack)
    user.connect("inference_api", inference("zen-key-live-000000"))
    stack.drain()
    project = user.post(
        f"/api/workspaces/{user.workspace_id}/projects", {"slug": "integrity", "spec": SPEC}
    ).json()
    created = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {"project_id": project["id"], "message": {"content": "hi [write:a.txt=one]"}},
    ).json()
    stack.drive(
        lambda: user.get(f"/api/turns/{created['turn_id']}").json()["turn"]["state"] == "succeeded"
    )
    yield stack, user, created["session_id"], tmp_path
    stack.shutdown()


def put(user: User, sid: str, body: dict[str, Any], key: str) -> Any:
    return user.http.put(
        f"/api/sessions/{sid}/files",
        json=body,
        headers={"X-CSRF-Token": user.csrf, "Idempotency-Key": key},
    )


def on_disk(tmp: Path, name: str) -> Path:
    found = [p for p in (tmp / "executor").rglob(name) if "/worktree/" in str(p)]
    assert len(found) == 1, found
    return found[0]


def test_truncated_view_cannot_replace_the_original_bytes(env) -> None:
    stack, user, sid, tmp = env
    original = "a" * (1_000_019 - len(TAIL)) + TAIL
    assert len(original.encode()) == 1_000_019
    assert (
        put(
            user, sid, {"path": "big.txt", "content": original, "expected_digest": "absent"}, "big"
        ).status_code
        == 200
    )
    digest = "sha256:" + hashlib.sha256(original.encode()).hexdigest()

    view = user.get(f"/api/sessions/{sid}/files/content?path=big.txt").json()
    assert view["truncated"] is True and len(view["content"]) == 1_000_000
    assert view["size"] == 1_000_019
    assert view["digest"] is None and view["editable"] is False
    assert view["readonly_reason"] == "truncated"

    # Echoing the partial view with its (null) precondition, as "absent", or with the full
    # digest learned elsewhere: the first two are refused, nothing replaces the file blindly.
    echoed = put(
        user,
        sid,
        {"path": "big.txt", "content": view["content"], "expected_digest": view["digest"]},
        "echo",
    )
    assert echoed.status_code == 422 and echoed.json()["error"]["code"] == "validation_failed"
    missing = put(user, sid, {"path": "big.txt", "content": view["content"]}, "missing")
    assert missing.status_code == 422
    absent = put(
        user,
        sid,
        {"path": "big.txt", "content": view["content"], "expected_digest": "absent"},
        "absent",
    )
    assert absent.status_code == 409 and absent.json()["error"]["code"] == "version_conflict"
    assert "already exists" in absent.json()["error"]["message"]

    data = on_disk(tmp, "big.txt").read_bytes()
    assert len(data) == 1_000_019 and data.endswith(TAIL.encode())
    assert "sha256:" + hashlib.sha256(data).hexdigest() == digest


def test_read_limit_boundaries_and_small_files_stay_editable(env) -> None:
    stack, user, sid, tmp = env
    exact = "b" * 999_999 + "\n"
    assert (
        put(
            user, sid, {"path": "exact.txt", "content": exact, "expected_digest": "absent"}, "e"
        ).status_code
        == 200
    )
    view = user.get(f"/api/sessions/{sid}/files/content?path=exact.txt").json()
    assert view["truncated"] is False and view["editable"] is True and view["digest"]
    saved = put(
        user,
        sid,
        {"path": "exact.txt", "content": "short\n", "expected_digest": view["digest"]},
        "e2",
    )
    assert saved.status_code == 200, saved.text
    assert on_disk(tmp, "exact.txt").read_text() == "short\n"

    # The read limit falls inside a two-byte character: the view stays text, never binary.
    wide = "c" * 999_999 + "é" + TAIL
    assert (
        put(
            user, sid, {"path": "wide.txt", "content": wide, "expected_digest": "absent"}, "w"
        ).status_code
        == 200
    )
    view = user.get(f"/api/sessions/{sid}/files/content?path=wide.txt").json()
    assert view["encoding"] == "utf-8" and view["truncated"] is True
    assert view["content"] == "c" * 999_999 and view["digest"] is None


def test_open_or_create_uses_absent_and_reports_concurrent_creation(env) -> None:
    stack, user, sid, tmp = env
    missing = user.get(f"/api/sessions/{sid}/files/content?path=nested/dir/new.txt")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    created = put(
        user,
        sid,
        {"path": "nested/dir/new.txt", "content": "fresh\n", "expected_digest": "absent"},
        "c1",
    )
    assert created.status_code == 200, created.text
    raced = put(
        user,
        sid,
        {"path": "nested/dir/new.txt", "content": "other\n", "expected_digest": "absent"},
        "c2",
    )
    assert raced.status_code == 409 and raced.json()["error"]["code"] == "version_conflict"
    assert "already exists" in raced.json()["error"]["message"]
    assert on_disk(tmp, "new.txt").read_text() == "fresh\n"


class LossyConnector:
    """Delivers the frame to the runtime, then loses the response for the next ``times`` ops of ``kind``."""

    def __init__(self, inner: Any, kind: str, times: int = 1) -> None:
        self.inner = inner
        self.kind = kind
        self.remaining = times

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def channel(self, lease: dict[str, Any]) -> Any:
        real = self.inner.channel(lease)
        outer = self

        class Channel:
            def __getattr__(self, name: str) -> Any:
                return getattr(real, name)

            def op(self, kind: str, *args: Any, **kwargs: Any) -> Any:
                response = real.op(kind, *args, **kwargs)
                if kind == outer.kind and outer.remaining > 0:
                    outer.remaining -= 1
                    raise RuntimeUnavailable("ReadTimeout")
                return response

        return Channel()


def wait_on_disk(tmp: Path, name: str) -> Path:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not list((tmp / "executor").rglob(name)):
        time.sleep(0.05)
    return on_disk(tmp, name)


def counted_lines(path: Path, settle: float = 1.0) -> int:
    time.sleep(settle)  # a duplicate execution would land within this window
    return len(path.read_text().splitlines())


def test_terminal_input_retry_after_lost_response_runs_once(env) -> None:
    stack, user, sid, tmp = env
    live = stack.services.live
    real = live.connector

    # terminal.create: the response is lost once; the retry with the same key returns
    # the same terminal instead of starting a second shell.
    live.connector = LossyConnector(real, "terminal.create")
    lost = user.post(f"/api/sessions/{sid}/terminals", {}, key="create-1")
    assert lost.status_code == 409 and lost.json()["error"]["code"] == "outcome_unknown"
    assert lost.json()["error"]["retryable"] is True
    term = user.post(f"/api/sessions/{sid}/terminals", {}, key="create-1").json()["terminal_id"]
    again = user.post(f"/api/sessions/{sid}/terminals", {}, key="create-1").json()["terminal_id"]
    assert again == term
    listed = user.get(f"/api/sessions/{sid}/terminals").json()["items"]
    assert [t["terminal_id"] for t in listed] == [term]

    # terminal.input: executed, response lost (control <-> runtime), then a gateway 503
    # (client <-> control) is simulated by simply sending the same request again.
    live.connector = LossyConnector(real, "terminal.input")
    command = {"data": "echo run >> counter.txt\n"}
    path = f"/api/sessions/{sid}/terminals/{term}/input"
    first = user.post(path, command, key="input-1")
    assert first.status_code == 409 and first.json()["error"]["code"] == "outcome_unknown"
    assert user.post(path, command, key="input-1").status_code == 200
    assert user.post(path, command, key="input-1").status_code == 200
    counter = wait_on_disk(tmp, "counter.txt")
    assert counted_lines(counter) == 1

    # The same key with another command (or another terminal) is a conflict, not a run.
    other = user.post(path, {"data": "echo other >> counter.txt\n"}, key="input-1")
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"
    elsewhere = user.post(f"/api/sessions/{sid}/terminals/pty_other/input", command, key="input-1")
    assert elsewhere.status_code == 409
    assert elsewhere.json()["error"]["code"] == "idempotency_conflict"
    assert counted_lines(counter) == 1

    # A fresh key is a new command.
    assert user.post(path, command, key="input-2").status_code == 200
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and counted_lines(counter, settle=0) < 2:
        time.sleep(0.05)
    assert counted_lines(counter) == 2

    # Without a key there is no retry safety, so the request is refused.
    unkeyed = user.http.post(path, json=command, headers={"X-CSRF-Token": user.csrf})
    assert unkeyed.status_code == 422
    assert counted_lines(counter, settle=0.3) == 2


def test_terminal_list_and_close(env) -> None:
    stack, user, sid, tmp = env
    term = user.post(f"/api/sessions/{sid}/terminals", {}).json()["terminal_id"]
    items = user.get(f"/api/sessions/{sid}/terminals").json()["items"]
    assert items[0]["terminal_id"] == term and items[0]["closed"] is False
    assert items[0]["created_at"]
    closed = user.delete(f"/api/sessions/{sid}/terminals/{term}")
    assert closed.status_code == 200 and closed.json()["closed"] is True
    assert user.get(f"/api/sessions/{sid}/terminals").json()["items"] == []
    gone = user.get(f"/api/sessions/{sid}/terminals/{term}/output?after=0")
    assert gone.status_code == 404
    # Closing again is harmless; another user never reaches the runtime.
    assert user.delete(f"/api/sessions/{sid}/terminals/{term}").status_code == 200
    other = User(stack)
    assert other.get(f"/api/sessions/{sid}/terminals").status_code == 404
