"""SOR-132/SOR-134: the lifecycle chain must resolve one set of values.

``lifecycle_config`` is the single resolution point: the runner's
``--max-seconds``, the sandbox's native ``timeout``/``idle_timeout``, the
reaper's bounds, and the deployed remote env all derive from it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from control.app import create_app
from control.backend import LocalProcessBackend, SandboxSpec
from control.backends.modal import ModalBackend
from control.config import (
    CREATE_GRACE_S,
    IDLE_TIMEOUT_S,
    REMOTE_ENV_KEYS,
    RUN_GRACE_S,
    SANDBOX_IDLE_TIMEOUT_S,
    SANDBOX_TIMEOUT_S,
    TURN_MAX_SECONDS,
    lifecycle_config,
    remote_env_overlay,
)
from control.reaper import reap
from control.store import InMemoryStore, SessionRecord, empty_usage

_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def _record(
    *,
    session_id: str,
    status: str,
    handle_id: str | None,
    last: datetime,
) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        title="t",
        status=status,
        created_at=last,
        updated_at=last,
        model="gpt-5.6-luna",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner="sbx",
        sandbox_id=handle_id,
        sandbox_root="/work" if handle_id else None,
        sandbox_tags={"session_id": session_id, "owner": "sbx"} if handle_id else {},
        last_activity_at=last,
    )


class _FakeSecret:
    @staticmethod
    def from_name(name: str) -> tuple[str, str]:
        return ("secret", name)

    @staticmethod
    def from_dict(data: dict) -> tuple[str, dict]:
        return ("dict", data)


class _FakeApp:
    @staticmethod
    def lookup(name: str, create_if_missing: bool = False) -> SimpleNamespace:
        return SimpleNamespace(app_id="app-1", name=name)


class _FakeSandbox:
    """Captures the kwargs ``ModalBackend._create_with_image`` passes."""

    created_kwargs: dict[str, Any] = {}

    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> SimpleNamespace:
        cls.created_kwargs = dict(kwargs)
        return SimpleNamespace(object_id="sb-fake")


class _FakeModal:
    App = _FakeApp
    Sandbox = _FakeSandbox
    Secret = _FakeSecret


def test_lifecycle_config_defaults_match_contract() -> None:
    lc = lifecycle_config({})
    # SOR-135: post-session idle retention defaults to the agreed 5 min —
    # deliberately independent of the sandbox's own native idle bound.
    assert lc.idle_timeout_s == IDLE_TIMEOUT_S == 300
    assert lc.sandbox_idle_timeout_s == SANDBOX_IDLE_TIMEOUT_S
    assert lc.turn_max_seconds == TURN_MAX_SECONDS
    assert lc.sandbox_timeout_s == SANDBOX_TIMEOUT_S
    assert lc.create_grace_s == CREATE_GRACE_S
    assert lc.run_grace_s == RUN_GRACE_S
    assert lc.run_stale_s == TURN_MAX_SECONDS + RUN_GRACE_S


def test_lifecycle_config_env_overrides() -> None:
    lc = lifecycle_config(
        {
            "SBX_IDLE_TIMEOUT_S": "3600",
            "SBX_SANDBOX_IDLE_TIMEOUT_S": "5400",
            "SBX_TURN_MAX_SECONDS": "1200",
            "SBX_SANDBOX_TIMEOUT_S": "28800",
            "SBX_CREATE_GRACE_S": "600",
            "SBX_RUN_GRACE_S": "120",
        }
    )
    assert lc.idle_timeout_s == 3600
    assert lc.sandbox_idle_timeout_s == 5400
    assert lc.turn_max_seconds == 1200
    assert lc.sandbox_timeout_s == 28800
    assert lc.create_grace_s == 600
    assert lc.run_grace_s == 120
    assert lc.run_stale_s == 1320


def test_lifecycle_config_post_idle_does_not_shrink_native_bound() -> None:
    """SOR-135: the 5-min post-session retention must not drag the native
    ``Sandbox.create(idle_timeout=)`` down with it — and SOR-134's floor
    keeps the native bound above the stranded-``running`` bound."""
    lc = lifecycle_config({"SBX_IDLE_TIMEOUT_S": "300"})
    assert lc.idle_timeout_s == 300
    assert lc.sandbox_idle_timeout_s == SANDBOX_IDLE_TIMEOUT_S
    # A long-turn deploy (SBX_TURN_MAX_SECONDS=2400) floors the native idle
    # bound above turn + run grace even when the operator never sets it.
    lc = lifecycle_config({"SBX_TURN_MAX_SECONDS": "2400"})
    assert lc.sandbox_idle_timeout_s == 2400 + RUN_GRACE_S
    # An explicit-but-too-low override clamps up the same way.
    lc = lifecycle_config({"SBX_SANDBOX_IDLE_TIMEOUT_S": "600", "SBX_TURN_MAX_SECONDS": "2400"})
    assert lc.sandbox_idle_timeout_s == 2700


def test_lifecycle_config_reads_process_env(monkeypatch) -> None:
    monkeypatch.setenv("SBX_IDLE_TIMEOUT_S", "60")
    assert lifecycle_config().idle_timeout_s == 60
    monkeypatch.delenv("SBX_IDLE_TIMEOUT_S")
    assert lifecycle_config().idle_timeout_s == IDLE_TIMEOUT_S
    monkeypatch.setenv("SBX_SANDBOX_IDLE_TIMEOUT_S", "7200")
    assert lifecycle_config().sandbox_idle_timeout_s == 7200


def test_remote_env_overlay_forwards_lifecycle_keys() -> None:
    env = {
        "SBX_IDLE_TIMEOUT_S": "3600",
        "SBX_SANDBOX_IDLE_TIMEOUT_S": "5400",
        "SBX_TURN_MAX_SECONDS": "1200",
        "SBX_SANDBOX_TIMEOUT_S": "28800",
        "SBX_CREATE_GRACE_S": "600",
        "SBX_RUN_GRACE_S": "120",
    }
    for key in env:
        assert key in REMOTE_ENV_KEYS
    out = remote_env_overlay(env, app_name="rc")
    for key, value in env.items():
        assert out[key] == value


def test_create_app_plane_uses_resolved_lifecycle(monkeypatch) -> None:
    monkeypatch.setenv("SBX_IDLE_TIMEOUT_S", "3600")
    monkeypatch.setenv("SBX_TURN_MAX_SECONDS", "1200")
    app = create_app(
        backend=LocalProcessBackend(),
        store=InMemoryStore(),
        runner_cmd=["python", "-m", "runtime.runner"],
    )
    assert app.state.plane.idle_timeout_s == 3600
    assert app.state.plane.turn_max_seconds == 1200


def test_reap_defaults_follow_resolved_idle_timeout(monkeypatch) -> None:
    """A reap() caller that passes no bounds must use the resolved value —
    1900s idle is past the 1800s contract default but inside a configured
    3600s bound."""
    monkeypatch.setenv("SBX_IDLE_TIMEOUT_S", "3600")
    backend = LocalProcessBackend()
    store = InMemoryStore()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    try:
        store.put(
            _record(
                session_id="s1",
                status="idle",
                handle_id=handle.id,
                last=_NOW - timedelta(seconds=1900),
            )
        )
        assert reap(store, backend, _NOW) == []
        # And the configured bound still fires.
        later = _NOW + timedelta(seconds=1800)
        actions = reap(store, backend, later)
        assert [a.kind for a in actions] == ["timed_out"]
    finally:
        backend.terminate(handle)


def test_reap_run_grace_follows_resolved_turn_max(monkeypatch) -> None:
    """The stranded-``running`` bound is the resolved turn max + grace, not
    the contract default: 1200s turn max means a 1000s-stale running record
    is still in-bounds."""
    monkeypatch.setenv("SBX_TURN_MAX_SECONDS", "1200")
    backend = LocalProcessBackend()
    store = InMemoryStore()
    handle = backend.create(SandboxSpec(tags={"session_id": "s1", "owner": "sbx"}))
    try:
        store.put(
            _record(
                session_id="s1",
                status="running",
                handle_id=handle.id,
                last=_NOW - timedelta(seconds=1000),
            )
        )
        assert reap(store, backend, _NOW) == []
        actions = reap(store, backend, _NOW + timedelta(seconds=600))
        assert [a.kind for a in actions] == ["lost"]
    finally:
        backend.terminate(handle)


def test_modal_sandbox_create_uses_resolved_timeouts(monkeypatch) -> None:
    """``Sandbox.create``'s native timers resolve through the chain —
    ``SBX_SANDBOX_IDLE_TIMEOUT_S`` drives the native bound while the
    post-session retention (``SBX_IDLE_TIMEOUT_S``) stays a control-plane
    knob that never reaches ``Sandbox.create`` (SOR-135)."""
    monkeypatch.setenv("SBX_SANDBOX_IDLE_TIMEOUT_S", "3600")
    monkeypatch.setenv("SBX_IDLE_TIMEOUT_S", "60")
    monkeypatch.setenv("SBX_SANDBOX_TIMEOUT_S", "28800")
    handle = ModalBackend()._create_with_image(_FakeModal, SandboxSpec(), image="img")
    assert handle.id == "sb-fake"
    assert _FakeSandbox.created_kwargs["timeout"] == 28800
    assert _FakeSandbox.created_kwargs["idle_timeout"] == 3600


def test_modal_sandbox_create_native_idle_floored_above_turn(monkeypatch) -> None:
    """SOR-134: a long-turn deploy must not let the native idle bound fall
    below ``turn_max + run_grace`` — that is what reclaimed long turns."""
    monkeypatch.delenv("SBX_SANDBOX_IDLE_TIMEOUT_S", raising=False)
    monkeypatch.setenv("SBX_TURN_MAX_SECONDS", "2400")
    ModalBackend()._create_with_image(_FakeModal, SandboxSpec(), image="img")
    assert _FakeSandbox.created_kwargs["idle_timeout"] == 2400 + RUN_GRACE_S


def test_modal_sandbox_create_defaults_match_contract(monkeypatch) -> None:
    monkeypatch.delenv("SBX_IDLE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("SBX_SANDBOX_IDLE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("SBX_SANDBOX_TIMEOUT_S", raising=False)
    ModalBackend()._create_with_image(_FakeModal, SandboxSpec(), image="img")
    assert _FakeSandbox.created_kwargs["timeout"] == SANDBOX_TIMEOUT_S
    assert _FakeSandbox.created_kwargs["idle_timeout"] == SANDBOX_IDLE_TIMEOUT_S
    assert SANDBOX_IDLE_TIMEOUT_S > TURN_MAX_SECONDS
