"""Canonical public error catalog for ``/v1`` (SOR-226).

Every ``error.code`` the API may emit is a row here with its canonical HTTP
status, whether the call is safe to retry unchanged (``retryable``), and a
stable client action hint (``action``). The catalog is the runtime source of
truth for the ``x-canonical.error_subcodes`` list in
``docs/contracts/api-v1.yaml`` and the error code enum in the generated
``/v1/openapi.json`` — a code not listed here must not be emitted.

``action`` vocabulary (stable):

* ``authenticate`` — supply or repair credentials, then resend.
* ``lookup`` — verify the referenced resource id, then resend.
* ``fix_request`` — change the request, then resend.
* ``wait`` — the named object is busy/settling; retry later (honor
  ``retry_after`` when present).
* ``retry`` — transient failure; the same request may be retried.
* ``configure`` — an operator must change deployment configuration first.
"""

from __future__ import annotations

from dataclasses import dataclass

ERROR_ACTIONS: tuple[str, ...] = (
    "authenticate",
    "lookup",
    "fix_request",
    "wait",
    "retry",
    "configure",
)


@dataclass(frozen=True)
class ErrorSpec:
    """One catalog row: canonical status + machine-readable client hints."""

    code: str
    status: int
    retryable: bool
    action: str
    description: str


def _row(status: int, retryable: bool, action: str, description: str) -> tuple[int, bool, str, str]:
    return (status, retryable, action, description)


_ROWS: dict[str, tuple[int, bool, str, str]] = {
    # --- authentication / authorization ---------------------------------
    "unauthorized": _row(401, False, "authenticate", "missing or invalid API key"),
    "forbidden": _row(403, False, "authenticate", "key lacks the required scope"),
    "grant_invalid": _row(401, False, "authenticate", "grant is invalid, expired, or used"),
    "pair_invalid": _row(401, False, "authenticate", "pair ticket is invalid or expired"),
    "github_app_state": _row(403, False, "authenticate", "authorize/manifest state expired"),
    # --- missing resources ----------------------------------------------
    "not_found": _row(404, False, "lookup", "referenced resource does not exist"),
    "workspace_not_found": _row(404, False, "lookup", "workspace record does not exist"),
    "artifact_not_found": _row(404, False, "lookup", "artifact does not exist"),
    "revision_not_found": _row(404, False, "lookup", "revision does not exist"),
    "delivery_not_found": _row(404, False, "lookup", "delivery record does not exist"),
    "session_not_found": _row(404, False, "lookup", "connect session does not exist"),
    "account_not_found": _row(404, False, "lookup", "account does not exist"),
    # --- malformed requests ---------------------------------------------
    "invalid_request": _row(400, False, "fix_request", "request is malformed"),
    "invalid_provider": _row(400, False, "fix_request", "provider id is not a catalog provider"),
    "unknown_provider": _row(400, False, "fix_request", "provider has no onboarding descriptor"),
    "invalid_scope": _row(400, False, "fix_request", "api-key scope is not a known scope"),
    "invalid_compute": _row(400, False, "fix_request", "compute selection is invalid"),
    "invalid_resource": _row(400, False, "fix_request", "resource ref is unknown or disallowed"),
    "invalid_output_contract": _row(400, False, "fix_request", "output contract is unusable"),
    "unsupported": _row(400, False, "fix_request", "requested capability is unsupported"),
    "workspace_invalid": _row(400, False, "fix_request", "workspace declaration is invalid"),
    "artifact_invalid": _row(400, False, "fix_request", "artifact request is invalid"),
    "checksum_mismatch": _row(400, False, "fix_request", "declared checksum does not match"),
    "base_sha_mismatch": _row(400, False, "fix_request", "declared base sha does not match"),
    "head_sha_mismatch": _row(400, False, "fix_request", "declared head sha does not match"),
    "provider_mismatch": _row(400, False, "fix_request", "account belongs to another provider"),
    "invalid_account_id": _row(400, False, "fix_request", "account id is not a safe identifier"),
    "invalid_blob": _row(400, False, "fix_request", "credential blob is malformed"),
    "invalid_source": _row(400, False, "fix_request", "source declaration is invalid"),
    "schema_mismatch": _row(400, False, "fix_request", "credential does not match provider schema"),
    "unsafe_path": _row(400, False, "fix_request", "credential path is unsafe"),
    "account_exists": _row(400, False, "fix_request", "an account with that id already exists"),
    "github_app_invalid": _row(400, False, "fix_request", "github app request is malformed"),
    # --- state conflicts -------------------------------------------------
    "idempotency_conflict": _row(409, False, "fix_request", "key replayed with a different body"),
    "idempotency_in_progress": _row(409, True, "wait", "keyed request still in flight"),
    "turn_in_progress": _row(409, True, "wait", "a run is already in progress"),
    "session_not_runnable": _row(409, True, "wait", "agent is not in a runnable state"),
    "session_active": _row(409, True, "wait", "connect session is already running"),
    "account_busy": _row(409, True, "wait", "named account has no free slot"),
    "account_unavailable": _row(409, True, "wait", "named account is not active"),
    "task_active": _row(409, True, "wait", "task has a run in flight"),
    "task_not_retryable": _row(409, False, "fix_request", "task has nothing to retry"),
    "revision_not_ready": _row(409, True, "wait", "revision is not ready yet"),
    "review_stale": _row(409, False, "fix_request", "review targets an outdated revision"),
    "review_required": _row(409, False, "fix_request", "an approving review is required first"),
    "independence_violation": _row(409, False, "fix_request", "reviewer not independent of author"),
    "artifact_secret": _row(409, False, "fix_request", "artifact contains secrets"),
    "github_app_configured": _row(409, False, "configure", "a github app is already configured"),
    "delivery_failed": _row(409, True, "retry", "delivery attempt failed"),
    "workspace_unavailable": _row(409, True, "retry", "workspace service is unavailable"),
    # --- capacity / transient --------------------------------------------
    "provider_exhausted": _row(429, True, "retry", "no free account for the provider pick"),
    "concurrency_limit": _row(429, True, "retry", "global concurrency cap reached"),
    "unavailable": _row(503, True, "retry", "required service is unavailable"),
    "repo_unavailable": _row(502, True, "retry", "repository could not be reached"),
    "checkout_failed": _row(502, True, "retry", "repo checkout failed"),
    "github_app_upstream": _row(502, True, "retry", "a github api call failed"),
    "github_app_unconfigured": _row(503, False, "configure", "no github app identity configured"),
    "internal": _row(500, True, "retry", "internal error"),
    "connect_failed": _row(400, True, "retry", "connect attempt failed"),
}

ERROR_CATALOG: dict[str, ErrorSpec] = {
    code: ErrorSpec(code=code, status=status, retryable=retryable, action=action, description=desc)
    for code, (status, retryable, action, desc) in _ROWS.items()
}

# Ordered code list for the canonical contract (``x-canonical.error_subcodes``)
# and the OpenAPI enum — sorted for a stable diff.
ERROR_SUBCODES: tuple[str, ...] = tuple(sorted(ERROR_CATALOG))


def spec_for(code: str, status: int | None = None) -> ErrorSpec:
    """Catalog row for ``code``; a synthesized row for uncatalogued codes.

    Unknown codes keep their emitted ``status`` and get conservative hints:
    5xx/429 retryable ``retry``, 4xx ``fix_request`` — the shape is always
    complete even when a brand-new code ships before the catalog is updated.
    """
    spec = ERROR_CATALOG.get(code)
    if spec is not None:
        return spec
    fallback_status = status or 400
    retryable = fallback_status in (429,) or fallback_status >= 500
    return ErrorSpec(
        code=code,
        status=fallback_status,
        retryable=retryable,
        action="retry" if retryable else "fix_request",
        description="uncatalogued error",
    )
