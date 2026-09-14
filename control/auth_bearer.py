"""Bearer ``sbx_<key>`` helpers for the public ``/v1`` API (SOR-64).

``Authorization: Bearer sbx_<key>`` is resolved through ``ports.ApiKeyStore``,
which stores only ``sha256(key)``; plaintext tokens are never persisted or
logged. Scopes are literal memberships on the key record: ``agents`` for the
agent/run/meta endpoints, ``admin`` for ``/v1/accounts*`` and ``/v1/api-keys*``.

This module intentionally stays free of HTTP response types; ``api_v1.deps``
maps the None/False results onto canonical 401/403 errors.
"""

from __future__ import annotations

from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from control.ports import ApiKey, ApiKeyStore

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="bearerAuth")


def bearer_token(credentials: HTTPAuthorizationCredentials | None) -> str | None:
    """Return the bearer token, or None when absent / wrong scheme."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    return credentials.credentials or None


def lookup_key(store: ApiKeyStore, token: str | None) -> ApiKey | None:
    """Resolve a Bearer token via the key store (sha256 lookup inside)."""
    if not token:
        return None
    return store.lookup(token)


def has_scope(key: ApiKey, scope: str) -> bool:
    """Literal scope membership check on the key record."""
    return scope in key.scopes
