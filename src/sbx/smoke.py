"""``sbx smoke`` — exercise ``/v1`` with a minimal agent run (SOR-98).

Creates one agent with a trivial prompt (no repo, no workspace), polls its
first run to a terminal status, then deletes the agent. Proves the deployed
stack authenticates, schedules, and tears down without needing a real
project.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from control.run_errors import RunError, clip_message

from sbx.config import ResolvedConfig
from sbx.errors import BootstrapError
from sbx.httpapi import ApiError, V1Client
from sbx.keys import resolve_api_key

DEFAULT_PROMPT = "Reply with exactly: sbx-ok"
TERMINAL_OK = "FINISHED"
TERMINAL_STATUSES = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED", "UNKNOWN"}
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_POLL_S = 3.0

# Canonical run-error code → remediation line (``docs/contracts/api-v1.yaml``
# ``x-canonical.run_error_codes``). Codes not listed fall back to the
# generic terminal hint.
_RUN_ERROR_HINTS: dict[str, str] = {
    "auth_invalid": (
        "provider credential rejected (auth_invalid) — re-import the account "
        "(`python -m control.onboarding --modal import`) or probe it with "
        "`POST /v1/accounts/{id}/verify`"
    ),
    "timeout": "the turn exceeded its time budget — check provider latency",
    "event_parse_error": ("the provider event stream was unparseable — check the provider CLI pin"),
}

_GENERIC_FAILED_HINT = (
    "the run reached a terminal state but not FINISHED — "
    "check provider credentials and `sbx doctor` output"
)


def _run_failure(run_id: str, status: str, run: Mapping[str, Any]) -> BootstrapError:
    """Surface the canonical ``run.error`` fields for a non-FINISHED terminal.

    ``run.error`` is the structured RunError the control plane persists
    (``code``/``source``/``message``/``retryable``/``retry_after``); a
    missing or non-canonical payload falls back to the bare status line.
    The message is re-clipped here — the third redaction seam — so a
    malformed upstream can never echo credential material to the terminal.
    """
    err = RunError.from_dict(run.get("error"))
    if err is None:
        return BootstrapError(
            f"smoke run {run_id} ended {status}",
            hint=_GENERIC_FAILED_HINT,
            code="smoke_run_failed",
        )
    detail = f"smoke run {run_id} ended {status} — {err.code} ({err.source})"
    if err.message:
        detail += f": {clip_message(err.message)}"
    hint = _RUN_ERROR_HINTS.get(err.code, _GENERIC_FAILED_HINT)
    if err.retryable:
        retry = "retryable — rerun `sbx smoke`"
        if err.retry_after is not None:
            retry = f"retryable — wait ~{err.retry_after:g}s, then rerun `sbx smoke`"
        hint = f"{hint}; {retry}"
    return BootstrapError(detail, hint=hint, code="smoke_run_failed")


@dataclass(frozen=True)
class SmokeResult:
    agent_id: str
    run_id: str
    status: str
    elapsed_s: float
    provider: str = ""


def _resolve_base(cfg: ResolvedConfig) -> str:
    base = cfg.config.api_base_url
    if not base:
        raise BootstrapError(
            "api.base_url is not configured",
            hint="run `sbx deploy`, or set api.base_url / SBX_BASE_URL",
            code="config_missing",
        )
    return base


def run_smoke(
    cfg: ResolvedConfig,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    provider: str | None = None,
    prompt: str = DEFAULT_PROMPT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_s: float = DEFAULT_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SmokeResult:
    env = os.environ if env is None else env
    base_url = _resolve_base(cfg)
    token = resolve_api_key(env)
    if token is None:
        raise BootstrapError(
            "no bootstrap API key available",
            hint="run `sbx deploy` to mint one, or export SBX_API_KEY",
            code="api_key_missing",
        )
    provider = provider or (cfg.config.providers[0] if cfg.config.providers else None)
    if provider is None:
        raise BootstrapError(
            "no provider configured",
            hint="set deploy.providers in the config or pass `sbx smoke --provider`",
            code="config_missing",
        )

    deadline = monotonic() + timeout_s
    agent_id = run_id = ""
    try:
        with V1Client(base_url, token, transport=transport, timeout=30.0) as client:
            # SOR-217: a smoke run occupies a real account slot and consumes
            # provider quota — the provider catalog must show a schedulable
            # (verified) account for the provider before creating anything.
            # A pre-catalog deployment (no /v1/providers) falls through and
            # lets ``create_agent``'s own errors decide.
            try:
                rows = client.providers().get("providers") or []
            except ApiError:
                rows = None
            if rows is not None:
                row = next(
                    (r for r in rows if isinstance(r, Mapping) and r.get("provider") == provider),
                    None,
                )
                conn = (row or {}).get("connection") or {}
                runtime = (row or {}).get("runtime") or {}
                if row is None or conn.get("status") != "connected":
                    if row is None:
                        detail = f"provider {provider!r} is not in the deployment catalog"
                    else:
                        detail = (
                            f"provider {provider!r} connection is "
                            f"{conn.get('status') or 'unknown'} "
                            f"({conn.get('accounts_available', 0)}/"
                            f"{conn.get('accounts_total', 0)} accounts schedulable)"
                        )
                        if runtime.get("status") in ("degraded", "disabled"):
                            detail += f"; runtime {runtime['status']}" + (
                                f" ({runtime['detail']})" if runtime.get("detail") else ""
                            )
                    raise BootstrapError(
                        f"smoke needs a verified provider account — {detail}",
                        hint="verify one with `sbx auth verify <account_id>` (or import "
                        "a credential and rerun `sbx deploy`); `sbx status` lists "
                        "account state. A smoke run consumes provider quota.",
                        code="provider_not_ready",
                    )
            try:
                created = client.create_agent(prompt=prompt, provider=provider)
            except ApiError as exc:
                if exc.code == "concurrency_limit":
                    hint = (
                        "concurrency_limit: the live-agent cap "
                        "(SBX_MAX_CONCURRENT) is reached — close an idle agent "
                        "(DELETE /v1/agents/{id}), run scoped cleanup "
                        "(DELETE /v1/workflows/{id}), or raise "
                        "deploy.max_concurrent / SBX_MAX_CONCURRENT and `sbx deploy`"
                    )
                else:
                    hint = (
                        "check provider accounts via `sbx doctor` — "
                        "provider_exhausted means no seeded account has a free slot"
                    )
                raise BootstrapError(
                    f"cannot create smoke agent ({exc.code}: {exc.message})",
                    hint=hint,
                    code="smoke_create_failed",
                ) from exc
            agent = created.get("agent") or {}
            run = created.get("run") or {}
            agent_id = str(agent.get("id") or "")
            run_id = str(run.get("id") or "")
            if not agent_id or not run_id:
                raise BootstrapError(
                    "create_agent returned an unexpected payload",
                    hint="check the /v1 contract version against docs/contracts/api-v1.yaml",
                    code="smoke_bad_payload",
                )

            status = ""
            last_error: Exception | None = None
            last_run: dict[str, Any] = {}
            while monotonic() < deadline:
                try:
                    payload: dict[str, Any] = client.get_run(agent_id, run_id)
                except ApiError as exc:
                    if exc.status == 401:
                        raise BootstrapError(
                            "smoke run lost auth mid-poll (401)",
                            hint="the bootstrap key was rejected — rerun `sbx doctor`",
                            code="smoke_auth_failed",
                        ) from exc
                    last_error = exc
                except httpx.HTTPError as exc:
                    last_error = exc
                else:
                    last_error = None
                    last_run = payload
                    status = str(payload.get("status") or "")
                    if status in TERMINAL_STATUSES:
                        break
                sleep(poll_s)
            else:
                detail = f" (last poll error: {last_error})" if last_error else ""
                raise BootstrapError(
                    f"smoke run {run_id} did not reach a terminal status "
                    f"in {timeout_s:.0f}s{detail}",
                    hint="check agent logs / provider credentials; "
                    "`sbx doctor` lists account health",
                    code="smoke_timeout",
                )
    finally:
        if agent_id:
            try:
                with V1Client(base_url, token, transport=transport, timeout=15.0) as client:
                    client.delete_agent(agent_id)
            except Exception:
                pass

    if status != TERMINAL_OK:
        raise _run_failure(run_id, status, last_run)
    return SmokeResult(
        agent_id=agent_id,
        run_id=run_id,
        status=status,
        elapsed_s=round(timeout_s - max(0.0, deadline - monotonic()), 1),
        provider=provider,
    )
