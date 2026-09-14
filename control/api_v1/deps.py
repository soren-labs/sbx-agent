"""FastAPI dependencies for the ``/v1`` router.

Everything binds to ``request.app.state`` so tests (and P2-C, later) can
inject real implementations; absent attributes get in-memory defaults from
:mod:`control.api_v1.state`.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from control.api_v1.errors import V1ApiError
from control.api_v1.state import (
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
    InMemoryScheduler,
    V1State,
)
from control.auth_bearer import bearer_scheme, bearer_token, has_scope, lookup_key
from control.ports import AccountRegistry, ApiKey, ApiKeyStore, Scheduler


def get_plane(request: Request) -> Any:
    """The shared SessionService (P1 ``ControlPlane``), same as ``/api/*``."""
    return request.app.state.plane


def get_v1_state(request: Request) -> V1State:
    state = getattr(request.app.state, "v1_state", None)
    if state is None:
        state = V1State()
        request.app.state.v1_state = state
    return state


def get_registry(request: Request) -> AccountRegistry:
    registry = getattr(request.app.state, "account_registry", None)
    if registry is None:
        registry = InMemoryAccountRegistry()
        request.app.state.account_registry = registry
    return registry


def get_scheduler(request: Request) -> Scheduler:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        scheduler = InMemoryScheduler(get_registry(request))
        request.app.state.scheduler = scheduler
    return scheduler


def get_key_store(request: Request) -> ApiKeyStore:
    store = getattr(request.app.state, "api_key_store", None)
    if store is None:
        store = InMemoryApiKeyStore()
        request.app.state.api_key_store = store
    return store


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
