"""FastAPI dependencies for the ``/v1`` router.

Everything binds to ``request.app.state`` so tests (and P2-C, later) can
inject real implementations; absent attributes get in-memory defaults from
:mod:`control.api_v1.state`.
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from control.api_v1.errors import V1ApiError
from control.api_v1.lifecycle import RUN_TERMINAL, RunStateStore
from control.api_v1.state import (
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
    V1State,
)
from control.auth_bearer import bearer_scheme, bearer_token, has_scope, lookup_key
from control.ports import AccountRegistry, ApiKey, ApiKeyStore, Scheduler
from control.scheduler import AccountScheduler


def get_plane(request: Request) -> Any:
    """The shared SessionService (P1 ``ControlPlane``), same as ``/api/*``."""
    return request.app.state.plane


def get_v1_state(request: Request) -> V1State:
    state = getattr(request.app.state, "v1_state", None)
    if state is None:
        state = V1State()
        request.app.state.v1_state = state
    return state


def get_run_states(request: Request) -> RunStateStore:
    """Run-state seam (SOR-82 A2): default in-memory; A1 (SOR-88) may install a
    durable ``RunStateStore`` on ``app.state.run_states`` without route changes."""
    store = getattr(request.app.state, "run_states", None)
    if store is None:
        store = get_v1_state(request).run_states
    return store


def get_registry(request: Request) -> AccountRegistry:
    registry = getattr(request.app.state, "account_registry", None)
    if registry is None:
        registry = InMemoryAccountRegistry()
        request.app.state.account_registry = registry
    return registry


def get_scheduler(request: Request) -> Scheduler:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        # SOR-63/D1 is the default scheduler even without bootstrap: atomic
        # acquire + cooldown/failover over whatever registry is installed.
        scheduler = AccountScheduler(get_registry(request))
        request.app.state.scheduler = scheduler
    return scheduler


def get_key_store(request: Request) -> ApiKeyStore:
    store = getattr(request.app.state, "api_key_store", None)
    if store is None:
        store = InMemoryApiKeyStore()
        request.app.state.api_key_store = store
    return store


# Structured run-error codes (SOR-82 taxonomy) that mark an account for
# cooldown/failover — reported verbatim to the scheduler's
# ``report_failure`` kind. ``auth_invalid`` is permanent (credential
# re-import needed); the others cool the account for ``retry_after`` / the
# scheduler's cooldown window. Control-side codes (``cancelled``,
# ``timeout``, ``runtime_error``, ``event_parse_error``,
# ``model_unavailable``) are not account health.
_ACCOUNT_HEALTH_CODES = frozenset(
    {
        "auth_invalid",
        "rate_limited",
        "quota_exhausted",
        "provider_unavailable",
        "model_capacity",
    }
)


class RunFailureReporter:
    """Feeds terminal provider run-errors back into the scheduler.

    The /v1 read path is where a finished turn's structured error first
    surfaces to the control plane; reportable provider failures
    (``rate_limited`` → cooling, ``auth_invalid`` → invalid, …) mark the
    run's account for cooldown/failover exactly once per run. No-ops when
    the scheduler lacks ``report_failure`` — the frozen ``Scheduler``
    protocol only requires ``decide``.
    """

    def __init__(self) -> None:
        self._reported: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    def report(
        self,
        *,
        scheduler: Any,
        agent_id: str,
        n: int,
        account_id: str | None,
        status: str | None,
        error: Any,
    ) -> None:
        if status not in RUN_TERMINAL or not isinstance(error, dict):
            return
        kind = error.get("code")
        if kind not in _ACCOUNT_HEALTH_CODES or not account_id or account_id == "auto":
            return
        key = (agent_id, n)
        with self._lock:
            if key in self._reported:
                return
            self._reported.add(key)
        report = getattr(scheduler, "report_failure", None)
        if not callable(report):
            return
        retry_after = error.get("retry_after")
        try:
            # AccountScheduler (SOR-63/D1): report_failure(account_id, kind, ...).
            report(account_id, kind, retry_after=retry_after)
        except TypeError:
            # Single-account pools take report_failure without account_id.
            try:
                report(kind, retry_after=retry_after)
            except Exception:
                pass
        except Exception:
            pass  # feedback is best-effort; never mask the API response


def get_run_reporter(request: Request) -> RunFailureReporter:
    reporter = getattr(request.app.state, "run_failure_reporter", None)
    if reporter is None:
        reporter = RunFailureReporter()
        request.app.state.run_failure_reporter = reporter
    return reporter


def api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    store: ApiKeyStore = Depends(get_key_store),
) -> ApiKey:
    """Any valid (unrevoked) ``sbx_`` key; 401 otherwise."""
    key = lookup_key(store, bearer_token(credentials))
    if key is None:
        raise V1ApiError(401, "unauthorized", "missing or invalid bearer token")
    return key


def agents_key(key: ApiKey = Depends(api_key)) -> ApiKey:
    """Key with the ``agents`` scope (agent / run / meta endpoints)."""
    if not has_scope(key, "agents"):
        raise V1ApiError(403, "forbidden", "api key lacks required scope 'agents'")
    return key


def admin_key(key: ApiKey = Depends(api_key)) -> ApiKey:
    """Key with the ``admin`` scope (accounts / api-keys / verify)."""
    if not has_scope(key, "admin"):
        raise V1ApiError(403, "forbidden", "api key lacks required scope 'admin'")
    return key
