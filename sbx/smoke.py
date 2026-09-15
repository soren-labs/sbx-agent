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

from sbx.config import ResolvedConfig
from sbx.errors import BootstrapError
from sbx.httpapi import ApiError, V1Client
from sbx.keys import resolve_api_key

DEFAULT_PROMPT = "Reply with exactly: sbx-ok"
TERMINAL_OK = "FINISHED"
TERMINAL_STATUSES = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED", "UNKNOWN"}
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_POLL_S = 3.0


@dataclass(frozen=True)
class SmokeResult:
    agent_id: str
    run_id: str
    status: str
    elapsed_s: float


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
    provider = provider or cfg.config.providers[0]

    deadline = monotonic() + timeout_s
    agent_id = run_id = ""
    try:
        with V1Client(base_url, token, transport=transport, timeout=30.0) as client:
            try:
                created = client.create_agent(prompt=prompt, provider=provider)
            except ApiError as exc:
                raise BootstrapError(
                    f"cannot create smoke agent ({exc.code}: {exc.message})",
                    hint="check provider accounts via `sbx doctor` — "
                    "provider_exhausted means no seeded account has a free slot",
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
        raise BootstrapError(
            f"smoke run {run_id} ended {status}",
            hint="the run reached a terminal state but not FINISHED — "
            "check provider credentials and `sbx doctor` output",
            code="smoke_run_failed",
        )
    return SmokeResult(
        agent_id=agent_id,
        run_id=run_id,
        status=status,
        elapsed_s=round(timeout_s - max(0.0, deadline - monotonic()), 1),
    )
