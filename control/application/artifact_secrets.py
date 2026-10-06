"""Credential values a Session's runtime may have materialized.

Sent (in the frame's ``secrets``, never the payload/journal) with capture and checkpoint
operations so the runtime's artifact policy can refuse them even after a runtime restart
lost its in-memory set. Historical values are filter-only; never stored in plaintext.
"""

from __future__ import annotations

from typing import Any

from control.domain.errors import DomainError


def _leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() if k != "username" for s in _leaves(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _leaves(v)]
    return []


def runtime_visible(credentials: Any, session: dict[str, Any] | None) -> dict[str, Any]:
    if credentials is None or session is None:
        return {}
    history = getattr(credentials, "artifact_material", None)
    if history is not None:
        values = _leaves(history(session))
        return {"redact": sorted({v for v in values if len(v) >= 8})} if values else {}
    values: list[str] = []
    for resolve in (lambda s: credentials.inference(s)[0], credentials.source):
        try:
            values.extend(_leaves(resolve(session)))
        except DomainError:
            continue  # revoked/missing: the runtime still holds what it was handed
    return {"redact": sorted({v for v in values if len(v) >= 8})} if values else {}
