"""SOR-82/A4 recovery-acceptance harness.

Reusable by a future real-Modal gate: ``FaultBackend`` wraps
``LocalProcessBackend`` with deterministic failure knobs, ``RecoveryEnv``
models one control-plane instance whose durable state (session store,
backend inventory, account registry, key store) survives ``restart()``, and
the assertion helpers express the SOR-82 contract — durable terminal state,
structured errors, idempotent create, never-fallback-to-FINISHED —
independently of which backend produced the run.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from control.app import create_app
from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.ports import Account
from control.service import ControlPlane
from control.store import InMemoryStore
from fastapi.testclient import TestClient
from tests.fakes.fake_ports import (
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
    InMemoryScheduler,
)

# RunStatus enum from docs/contracts/api-v1.yaml.
TERMINAL_RUN_STATUSES = frozenset({"FINISHED", "ERROR", "CANCELLED", "EXPIRED"})
LIVE_RUN_STATUSES = frozenset({"CREATING", "RUNNING"})

# SOR-82/A3 structured-error contract: Run.error.code values.
CANONICAL_ERROR_CODES = frozenset(
    {
        "auth_invalid",
        "rate_limited",
        "quota_exhausted",
        "model_unavailable",
        "model_capacity",
        "provider_unavailable",
        "runtime_error",
        "event_parse_error",
        "timeout",
        "cancelled",
    }
)
# Run.error.source distinguishes which layer failed.
ERROR_SOURCES = frozenset({"provider", "runtime", "control", "telemetry"})


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


class FaultBackend(LocalProcessBackend):
    """``LocalProcessBackend`` with deterministic fault knobs.

    All knobs are plain attributes so a test can flip them between phases:

    * ``handles`` / ``create_calls`` — every successful ``create``;
      ``len(handles)`` is the number of workers actually allocated.
    * ``create_failures`` — exceptions raised by upcoming ``create`` calls
      (one popped per attempt).
    * ``create_delay_s`` — sleep inside ``create`` (slow worker startup).
    * ``create_gate`` — when set to a cleared ``threading.Event``, every
      ``create`` blocks until it fires (or the safety timeout elapses).
    * ``create_started`` — fires once the first ``create`` call arrives.
    * ``exec_fail_for`` — ``exec`` raises when argv contains any listed
      token (``"init"`` fails runner init, ``"turn"`` fails turns).
    * ``terminated_ids`` — every handle id passed to ``terminate``.
    """

    _GATE_TIMEOUT_S = 30.0

    def __init__(self) -> None:
        super().__init__()
        self.create_calls: list[SandboxSpec] = []
        self.handles: list[SandboxHandle] = []
        self.create_failures: deque[BaseException] = deque()
        self.create_delay_s: float = 0.0
        self.create_gate: threading.Event | None = None
        self.create_started = threading.Event()
        self.exec_fail_for: set[str] = set()
        self.exec_calls: list[list[str]] = []
        self.terminated_ids: list[str] = []
        self._fault_lock = threading.Lock()

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.create_started.set()
        gate = self.create_gate
        if gate is not None:
            gate.wait(timeout=self._GATE_TIMEOUT_S)
        if self.create_delay_s:
            time.sleep(self.create_delay_s)
        with self._fault_lock:
            if self.create_failures:
                raise self.create_failures.popleft()
        handle = super().create(spec)
        with self._fault_lock:
            self.create_calls.append(spec)
            self.handles.append(handle)
        return handle

    def exec(
        self,
        handle: SandboxHandle,
        argv: list[str],
        env: dict[str, str] | None = None,
    ) -> Any:
        with self._fault_lock:
            self.exec_calls.append(list(argv))
            fail = bool(self.exec_fail_for & set(argv))
        if fail:
            raise RuntimeError(f"injected exec failure: {argv!r}")
        return super().exec(handle, argv, env)

    def terminate(self, handle: SandboxHandle) -> None:
        with self._fault_lock:
            self.terminated_ids.append(handle.id)
        return super().terminate(handle)


@dataclass
class RecoveryEnv:
    """One control-plane instance plus the durable state a restart shares.

    ``store`` / ``backend`` / ``registry`` / ``keys`` model the durable
    layer (Modal Dicts and the Modal sandbox inventory in production);
    ``app.state.plane`` / ``v1_state`` / ``scheduler`` are rebuilt fresh by
    ``restart()`` — whatever must survive a restart has to live in the
    durable set.
    """

    app: Any
    client: TestClient
    backend: FaultBackend
    store: InMemoryStore
    registry: InMemoryAccountRegistry
    keys: InMemoryApiKeyStore
    runner_cmd: list[str]
    agents_token: str
    admin_token: str
    keepalive_s: float = 0.2
    max_concurrent: int = 8
    turn_max_seconds: int | None = None
    _children: list[RecoveryEnv] = field(default_factory=list)

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.agents_token}"}

    @property
    def admin_auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}

    # ------------------------------------------------------------- HTTP

    def post_agent(
        self,
        *,
        text: str = "Create hello.txt in the workspace.",
        name: str | None = "recovery-sample",
        provider: str = "codex",
        account_id: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        agent: dict[str, Any] = {"provider": provider}
        if account_id:
            agent["account_id"] = account_id
        body: dict[str, Any] = {"prompt": {"text": text}, "agent": agent}
        if name is not None:
            body["name"] = name
        request_headers = dict(self.auth)
        if headers:
            request_headers.update(headers)
        if idempotency_key is not None:
            # SOR-82 freezes ONE of Idempotency-Key / client_request_id; send
            # both so the suite holds whichever the contract keeps. Unknown
            # body fields are ignored by the pydantic schema.
            request_headers["Idempotency-Key"] = idempotency_key
            body["client_request_id"] = idempotency_key
            agent["client_request_id"] = idempotency_key
        return self.client.post("/v1/agents", json=body, headers=request_headers)

    def get_agent(self, agent_id: str) -> httpx.Response:
        return self.client.get(f"/v1/agents/{agent_id}", headers=self.auth)

    def list_agents(self) -> httpx.Response:
        return self.client.get("/v1/agents", headers=self.auth)

    def get_run(self, agent_id: str, run_id: str = "run-1") -> httpx.Response:
        return self.client.get(f"/v1/agents/{agent_id}/runs/{run_id}", headers=self.auth)

    def list_runs(self, agent_id: str) -> httpx.Response:
        return self.client.get(f"/v1/agents/{agent_id}/runs", headers=self.auth)

    def cancel_run(self, agent_id: str, run_id: str = "run-1") -> httpx.Response:
        return self.client.post(f"/v1/agents/{agent_id}/runs/{run_id}/cancel", headers=self.auth)

    # ------------------------------------------------------------ polls

    def wait_run(
        self,
        agent_id: str,
        run_id: str = "run-1",
        *,
        want: frozenset[str] = TERMINAL_RUN_STATUSES,
        timeout: float = 15.0,
        interval: float = 0.1,
    ) -> dict[str, Any]:
        """Poll ``GET run`` until its status is in ``want``; fail otherwise."""
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            resp = self.get_run(agent_id, run_id)
            if resp.status_code == 200:
                last = resp.json()
                if last.get("status") in want:
                    return last
            else:
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            time.sleep(interval)
        raise AssertionError(f"run {run_id} did not reach {sorted(want)}; last={last!r}")

    def observe_run_statuses(
        self,
        agent_id: str,
        run_id: str = "run-1",
        *,
        timeout: float = 20.0,
        interval: float = 0.05,
    ) -> list[str]:
        """Statuses seen while polling until a terminal status (``HTTP<code>``
        entries for non-200 reads). Consecutive duplicates collapse."""
        seen: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self.get_run(agent_id, run_id)
            status = (
                str(resp.json().get("status"))
                if resp.status_code == 200
                else f"HTTP{resp.status_code}"
            )
            if not seen or seen[-1] != status:
                seen.append(status)
            if status in TERMINAL_RUN_STATUSES:
                return seen
            time.sleep(interval)
        return seen

    # ---------------------------------------------------------- faults

    def teardown(self, agent_id: str | None = None) -> None:
        """Destroy sandboxes like a crash/reaper would (no record update)."""
        for handle in self.backend.list():
            if agent_id is None or handle.tags.get("session_id") == agent_id:
                self.backend.terminate(handle)

    def sandbox_handle(self, agent_id: str) -> SandboxHandle | None:
        """The handle allocated for ``agent_id`` (valid even after teardown)."""
        for handle in self.backend.handles:
            if handle.tags.get("session_id") == agent_id:
                return handle
        return None

    # --------------------------------------------------------- lifecycle

    def restart(self) -> RecoveryEnv:
        """Simulate a control-plane restart.

        A new app/plane/v1-state/scheduler is built over the same durable
        store, backend inventory, account registry and key store, so only
        state held by the durable layer survives.
        """
        env = build_env(
            runner_cmd=self.runner_cmd,
            backend=self.backend,
            store=self.store,
            registry=self.registry,
            keys=self.keys,
            agents_token=self.agents_token,
            admin_token=self.admin_token,
            keepalive_s=self.keepalive_s,
            max_concurrent=self.max_concurrent,
            turn_max_seconds=self.turn_max_seconds,
        )
        self._children.append(env)
        return env

    def close(self) -> None:
        for child in self._children:
            child.close()
        for handle in self.backend.list():
            try:
                self.backend.terminate(handle)
            except Exception:
                pass
        try:
            self.client.close()
        except Exception:
            pass


def build_env(
    *,
    runner_cmd: list[str],
    backend: FaultBackend | None = None,
    store: InMemoryStore | None = None,
    registry: InMemoryAccountRegistry | None = None,
    keys: InMemoryApiKeyStore | None = None,
    agents_token: str | None = None,
    admin_token: str | None = None,
    keepalive_s: float = 0.2,
    max_concurrent: int = 8,
    turn_max_seconds: int | None = None,
) -> RecoveryEnv:
    """Build one control-plane instance over the given (or fresh) state."""
    backend = backend or FaultBackend()
    store = store or InMemoryStore()
    app = create_app(
        backend=backend,
        store=store,
        runner_cmd=list(runner_cmd),
        basic_user="sbx",
        basic_password="sbx",
        keepalive_s=keepalive_s,
        max_concurrent=max_concurrent,
    )
    if turn_max_seconds is not None:
        app.state.plane = ControlPlane(
            backend,
            store,
            list(runner_cmd),
            turn_max_seconds=turn_max_seconds,
            max_concurrent=max_concurrent,
        )
    registry = registry or InMemoryAccountRegistry()
    keys = keys or InMemoryApiKeyStore()
    app.state.account_registry = registry
    app.state.scheduler = InMemoryScheduler(registry)
    app.state.api_key_store = keys
    if registry.get("acct-codex-1") is None:
        registry.put(
            Account(
                id="acct-codex-1",
                provider="codex",
                label="codex account",
                max_concurrent=8,
                models=("gpt-5.6-luna",),
                created_at=_iso_now(),
            )
        )
    if agents_token is None:
        _, agents_token = keys.create(label="agents-key", scopes=("agents",))
    if admin_token is None:
        _, admin_token = keys.create(label="admin-key", scopes=("admin", "agents"))
    client = TestClient(app, raise_server_exceptions=False)
    return RecoveryEnv(
        app=app,
        client=client,
        backend=backend,
        store=store,
        registry=registry,
        keys=keys,
        runner_cmd=list(runner_cmd),
        agents_token=agents_token,
        admin_token=admin_token,
        keepalive_s=keepalive_s,
        max_concurrent=max_concurrent,
        turn_max_seconds=turn_max_seconds,
    )


# ------------------------------------------------------- contract asserts


def assert_structured_error(
    run: dict[str, Any],
    *,
    code: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """SOR-82 structured-error contract: ``GET run`` alone diagnoses failure."""
    err = run.get("error")
    assert isinstance(err, dict), (
        "GET run must expose a structured error object; "
        "a harness must not need SSE to learn why a run failed"
    )
    actual_code = err.get("code")
    assert actual_code in CANONICAL_ERROR_CODES, f"non-canonical error code {actual_code!r}"
    actual_source = err.get("source")
    assert actual_source in ERROR_SOURCES, f"non-canonical error source {actual_source!r}"
    assert isinstance(err.get("message"), str) and err["message"], "error.message required"
    assert isinstance(err.get("retryable"), bool), "error.retryable (bool) required"
    retry_after = err.get("retry_after")
    if retry_after is not None:
        assert isinstance(retry_after, int | float) and retry_after >= 0
    if code is not None:
        assert actual_code == code, f"expected error.code {code!r}, got {actual_code!r}"
    if source is not None:
        assert actual_source == source, f"expected error.source {source!r}, got {actual_source!r}"
    return err


def assert_agent_identity_stable(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Provider/account/model survive a control-plane restart."""
    for field_name in ("id", "name", "provider", "account_id", "model", "status"):
        assert after.get(field_name) == before.get(field_name), (
            f"agent.{field_name} drifted across restart: "
            f"{before.get(field_name)!r} -> {after.get(field_name)!r}"
        )


def assert_run_durable(
    env: RecoveryEnv,
    agent_id: str,
    run_id: str = "run-1",
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Terminal snapshot must not drift across teardown and restart.

    Returns the terminal run payload. SOR-82: once a run is terminal its
    status/result/error/usage/timestamps are durable facts — sandbox
    reclamation and control-plane restarts cannot change them.
    """
    snap = env.wait_run(agent_id, run_id, timeout=timeout)
    agent_before = env.get_agent(agent_id).json()

    env.teardown(agent_id)
    resp = env.get_run(agent_id, run_id)
    assert resp.status_code == 200, "historical run must survive sandbox teardown"
    assert resp.json() == snap, (
        f"terminal run drifted after sandbox teardown:\n{snap!r}\n->\n{resp.json()!r}"
    )

    env2 = env.restart()
    resp = env2.get_run(agent_id, run_id)
    assert resp.status_code == 200, "historical run must survive control-plane restart"
    assert resp.json() == snap, (
        f"terminal run drifted after control-plane restart:\n{snap!r}\n->\n{resp.json()!r}"
    )
    assert_agent_identity_stable(agent_before, env2.get_agent(agent_id).json())
    return snap
