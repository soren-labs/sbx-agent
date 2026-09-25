"""Formal typed SDK for the sbx-browser public ``/v1`` API (SOR-226).

``SbxClient`` covers every Task/Run/Revision/Delivery/Review operation —
``tasks`` / ``agents`` / ``runs`` / ``artifacts`` namespaces — plus the
flat run/watch/wait/recover verbs shared with ``examples/sbx_client.py``.
Core workflows never need raw HTTP:

* ``client.tasks.create(...)`` → :class:`TaskCreated` (task + agent + run),
  ``tasks.get``/``tasks.list``/``tasks.wait`` → :class:`Task` /
  :class:`TaskDetail`, ``tasks.followup`` → :class:`Run`,
  ``tasks.cancel``/``retry``/``delivery`` → :class:`TaskDetail`,
  ``tasks.deliver``/``merge`` → :class:`Revision`,
  ``tasks.reviews``/``review`` → :class:`Review`.
* ``client.runs.get/list/cancel/wait`` → :class:`Run`;
  ``runs.watch``/``stream``/``resume`` yield :class:`SseEvent` with
  automatic ``Last-Event-ID`` reconnect and a bounded budget — never an
  infinite retry.
* ``client.agents.create/get/list/close/handoff/revisions`` — handoff is
  the SOR-83 cross-agent seam (``POST /v1/agents/{id}/handoff``).
* ``client.recover(workflow_id)`` → :class:`WorkflowRecovery` re-attaches
  with only an API key and the workflow id.

Idempotency is automatic (SOR-226): every mutating call carries a fresh
``Idempotency-Key`` unless the caller supplies one, and a transport-failed
keyed mutation is retried once — the server replays the pinned response
instead of double-applying. Pass ``auto_idempotency=False`` or
``idempotency_key=False`` to opt out per call.

Errors decode the canonical ``{error:{code,message,retryable,action,
retry_after?,details?}}`` body into :class:`SbxApiError`.

Every normal verb runs under a finite connect/read/write/pool timeout
(SOR-118): a stalled server surfaces as ``SbxTransportError`` — never an
unbounded wait. ``watch``/SSE keep their own long-lived read semantics.
``SBX_HTTP_TIMEOUT_S`` overrides the default budget with a single seconds
value.

Only dependency: httpx.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import httpx

from sbx.sdk.errors import SbxApiError, SbxTransportError
from sbx.sdk.models import (
    RUN_DONE,
    RUN_END_EVENTS,
    TASK_TERMINAL,
    Agent,
    Review,
    Revision,
    Run,
    SseEvent,
    Task,
    TaskCreated,
    TaskDetail,
    WorkflowRecovery,
)

DEFAULT_BASE_URL = "https://sbx.sorenforge.com"

# Finite transport budget for every non-stream verb (SOR-118). ``read`` is
# the max inactivity gap between bytes, not a total budget. ``watch``/SSE
# reuse the connect/write/pool bounds with their own longer read gap.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=10.0)
TIMEOUT_ENV = "SBX_HTTP_TIMEOUT_S"


def _resolve_timeout(
    timeout: httpx.Timeout | float | None, client: httpx.Client | None
) -> httpx.Timeout:
    """Finite ``httpx.Timeout`` for the normal verbs — never ``None``.

    ``timeout`` wins, then ``SBX_HTTP_TIMEOUT_S`` (one seconds value for all
    four phases), then an injected client's own timeout when it is finite,
    then ``DEFAULT_TIMEOUT``.
    """
    if isinstance(timeout, httpx.Timeout):
        return timeout
    if timeout is not None:
        return httpx.Timeout(float(timeout))
    raw = os.environ.get(TIMEOUT_ENV)
    if raw:
        try:
            return httpx.Timeout(float(raw))
        except ValueError:
            pass
    if client is not None:
        inherited = client.timeout
        if isinstance(inherited, httpx.Timeout) and any(
            getattr(inherited, part) is not None for part in ("connect", "read", "write", "pool")
        ):
            return inherited
    return DEFAULT_TIMEOUT


class _StreamRetry(Exception):
    """Internal: reopen the SSE stream (bounded by ``max_reconnects``)."""


def _decode_data(data_lines: list[str]) -> Any:
    text = "\n".join(data_lines)
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text}


def _sse_frames(lines: Iterable[str]) -> Iterator[SseEvent | str]:
    """Parse ``text/event-stream`` lines into frames.

    Yields ``SseEvent`` for each dispatched frame and comment lines
    (``: keepalive``) as ``str`` — watchers use comments as liveness ticks.
    """
    event_id: str | None = None
    event_type = "message"
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data_lines:
                yield SseEvent(id=event_id, type=event_type, data=_decode_data(data_lines))
            event_id, event_type, data_lines = None, "message", []
            continue
        if line.startswith(":"):
            yield line
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "id":
            event_id = value or None
        elif name == "event":
            event_type = value or "message"
        elif name == "data":
            data_lines.append(value)


class SbxClient:
    """Public ``/v1`` client. Pass ``client=`` to inject a transport.

    ``timeout`` bounds every non-stream call (default ``DEFAULT_TIMEOUT`` /
    ``SBX_HTTP_TIMEOUT_S``); streams keep their own long-lived read gap.
    ``auto_idempotency`` (default on) keys every mutating call so retries
    replay safely — see module docstring.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        *,
        timeout: httpx.Timeout | float | None = None,
        auto_idempotency: bool = True,
    ) -> None:
        self._owns = client is None
        self._timeout = _resolve_timeout(timeout, client)
        self._auto_idempotency = auto_idempotency
        self.http = client or httpx.Client(
            base_url=(base_url or os.environ.get("SBX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            headers={"Authorization": f"Bearer {api_key or os.environ['SBX_API_KEY']}"},
            timeout=self._timeout,
        )
        self.artifacts = _Artifacts(self)
        self.tasks = _Tasks(self)
        self.agents = _Agents(self)
        self.runs = _Runs(self)

    def close(self) -> None:
        """Close the owned HTTP client. Never touches remote state."""
        if self._owns:
            self.http.close()

    def __enter__(self) -> SbxClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------- transport

    def _check(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            try:
                error = resp.json().get("error", {})
            except ValueError:
                error = {}
            raise SbxApiError(
                resp.status_code,
                error.get("code") or f"http_{resp.status_code}",
                error.get("message") or resp.text[:200],
                error.get("retry_after"),
                retryable=error.get("retryable"),
                action=error.get("action"),
                details=error.get("details"),
            )
        return resp.json() if resp.content else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        check: str | None = None,
        idempotent: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """One bounded non-stream call.

        Transport failures (timeout, drop, refused connect) become
        ``SbxTransportError`` carrying ``check`` — the durable GET to run
        before retrying — so a caller never re-POSTs a mutation that may
        already be persisted.
        """
        try:
            return self.http.request(method, path, timeout=self._timeout, **kwargs)
        except httpx.TransportError as exc:
            raise SbxTransportError(
                method, path, check=check, idempotent=idempotent, original=exc
            ) from exc

    def _mutate(
        self,
        method: str,
        path: str,
        *,
        check: str | None = None,
        idempotent: bool = False,
        idempotency_key: str | bool | None = None,
        keyed: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Mutating call with automatic idempotency (SOR-226).

        ``keyed`` marks routes whose server honors ``Idempotency-Key``
        (``POST /v1/agents``, ``POST /v1/agents/{id}/runs``,
        ``POST /v1/tasks``, ``POST /v1/tasks/{id}/runs``). There a fresh
        key is attached automatically (unless the caller passes one, or
        ``False`` to opt out) and a transport-failed request is retried
        once — the server replays the pinned response rather than
        double-applying the mutation. On unkeyed routes an explicit
        ``idempotency_key`` is still sent, but no blind retry happens:
        ``SbxTransportError.check`` names the durable read instead.
        """
        headers = dict(kwargs.pop("headers", None) or {})
        key: str | None
        if idempotency_key is False:
            key = None
        elif idempotency_key is not None:
            key = str(idempotency_key)
        elif keyed and self._auto_idempotency and method in ("POST", "PUT", "PATCH"):
            key = f"sdk-{secrets.token_hex(16)}"
        else:
            key = None
        if key:
            headers["Idempotency-Key"] = key
        attempts = 2 if key and keyed else 1
        for attempt in range(attempts):
            try:
                return self._request(
                    method,
                    path,
                    headers=headers or None,
                    check=check,
                    idempotent=idempotent or (key is not None and keyed),
                    **kwargs,
                )
            except SbxTransportError:
                if attempt + 1 >= attempts:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _stream_timeout(self, read_s: float | None) -> httpx.Timeout:
        """Stream budget: the normal connect/write/pool bounds plus the
        caller's long-lived SSE read gap (``None`` = unbounded reads)."""
        base = self._timeout
        return httpx.Timeout(connect=base.connect, write=base.write, pool=base.pool, read=read_s)

    # ------------------------------------------------------------- agents

    def create_agent(
        self,
        text: str,
        provider: str = "codex",
        account_id: str = "auto",
        model: str | None = None,
        name: str | None = None,
        *,
        idle_timeout_s: int | None = None,
        metadata: dict[str, Any] | None = None,
        workspace: dict[str, Any] | None = None,
        handoff: dict[str, Any] | None = None,
        output_contract: dict[str, Any] | None = None,
        idempotency_key: str | bool | None = None,
    ) -> dict[str, Any]:
        """Create an agent and queue its first run (SOR-82: may return while
        the run is still ``CREATING`` — poll ``wait`` for the terminal state).

        ``idempotency_key`` maps to the ``Idempotency-Key`` contract — a
        retried key replays the original response instead of double-creating.
        With ``auto_idempotency`` (default) a key is generated automatically,
        so transport-failed creates are retried once safely.

        ``output_contract`` (SOR-130): ``{"schema": <JSON Schema>,
        "enforcement": "strict"|"warn"}`` — the run's final message must be
        one JSON value validating against the schema. ``strict`` (default)
        fails invalid output as ``ERROR``/``contract_violation``; ``warn``
        keeps ``FINISHED`` with the same diagnostic. The extracted value and
        the verdict land on the run as ``structured_output`` /
        ``output_contract``.
        """
        body: dict[str, Any] = {
            "prompt": {"text": text},
            "agent": {"provider": provider, "account_id": account_id},
        }
        if model:
            body["agent"]["model"] = model
        if name:
            body["name"] = name
        if idle_timeout_s is not None:
            body["idle_timeout_s"] = idle_timeout_s
        if metadata:
            body["metadata"] = dict(metadata)
        if workspace:
            body["workspace"] = dict(workspace)
        if handoff:
            body["handoff"] = dict(handoff)
        if output_contract:
            body["output_contract"] = dict(output_contract)
        return self._check(
            self._mutate(
                "POST",
                "/v1/agents",
                json=body,
                check="GET /v1/agents",
                keyed=True,
                idempotency_key=idempotency_key,
            )
        )

    def create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Orchestration alias for ``create_agent``."""
        return self.create_agent(*args, **kwargs)

    def list_agents(self, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._check(self._request("GET", "/v1/agents", params=clean))

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._check(self._request("GET", f"/v1/agents/{agent_id}"))

    def delete_agent(self, agent_id: str) -> dict[str, Any]:
        return self._check(
            self._request(
                "DELETE",
                f"/v1/agents/{agent_id}",
                check=f"GET /v1/agents/{agent_id}",
                idempotent=True,  # re-close returns the closed record
            )
        )

    def close_agent(self, agent_id: str) -> dict[str, Any]:
        """Close an agent — reclaim the sandbox; history stays read-only."""
        return self.delete_agent(agent_id)

    # --------------------------------------------------------------- runs

    def followup(
        self,
        agent_id: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
        output_contract: dict[str, Any] | None = None,
        idempotency_key: str | bool | None = None,
    ) -> dict[str, Any]:
        """Queue a follow-up run on an existing agent (``POST .../runs``).

        ``output_contract`` (SOR-130) attaches a per-run JSON Schema output
        contract — see ``create_agent`` for the shape and semantics.
        """
        body: dict[str, Any] = {"prompt": {"text": text}}
        if metadata:
            body["metadata"] = dict(metadata)
        if output_contract:
            body["output_contract"] = dict(output_contract)
        return self._check(
            self._mutate(
                "POST",
                f"/v1/agents/{agent_id}/runs",
                json=body,
                check=f"GET /v1/agents/{agent_id}/runs",
                keyed=True,
                idempotency_key=idempotency_key,
            )
        )

    def create_run(self, agent_id: str, text: str, **kwargs: Any) -> dict[str, Any]:
        return self.followup(agent_id, text, **kwargs)

    def list_runs(self, agent_id: str) -> list[dict[str, Any]]:
        return self._check(self._request("GET", f"/v1/agents/{agent_id}/runs"))["runs"]

    def get_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return self._check(self._request("GET", f"/v1/agents/{agent_id}/runs/{run_id}"))

    def cancel_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return self._check(
            self._request(
                "POST",
                f"/v1/agents/{agent_id}/runs/{run_id}/cancel",
                check=f"GET /v1/agents/{agent_id}/runs/{run_id}",
                idempotent=True,  # cancel on a terminal run returns latest state
            )
        )

    def cancel(self, agent_id: str, run_id: str) -> dict[str, Any]:
        """Explicitly cancel a run — the only cancel path; ``watch`` never does."""
        return self.cancel_run(agent_id, run_id)

    def usage(self, agent_id: str) -> dict[str, Any]:
        return self._check(self._request("GET", f"/v1/agents/{agent_id}/usage"))

    # --- SOR-83: workspaces, handoffs, artifacts ---------------------------

    def get_workspace(self, agent_id: str) -> dict:
        return self._check(self._request("GET", f"/v1/agents/{agent_id}/workspace"))["workspace"]

    def review_workspace(self, agent_id: str, head_sha: str | None = None) -> dict:
        body = {"head_sha": head_sha} if head_sha else {}
        return self._check(
            self._mutate(
                "POST",
                f"/v1/agents/{agent_id}/workspace/review",
                json=body,
                # Pinning the same reviewed head twice is a no-op — durable
                # truth is the record's ``reviewed_head_sha``.
                check=f"GET /v1/agents/{agent_id}/workspace (inspect reviewed_head_sha)",
                idempotent=True,
            )
        )["workspace"]

    def apply_handoff(
        self,
        agent_id: str,
        *,
        artifact_id: str | None = None,
        head_sha: str | None = None,
        workspace: dict | None = None,
    ) -> dict:
        body: dict = {}
        if artifact_id:
            body["artifact_id"] = artifact_id
        if head_sha:
            body["head_sha"] = head_sha
        if workspace:
            body["workspace"] = workspace
        return self._check(
            self._mutate(
                "POST",
                f"/v1/agents/{agent_id}/handoff",
                json=body,
                check=f"GET /v1/agents/{agent_id}/workspace",
            )
        )["workspace"]

    # Handoff is the same seam under its task-listed name.
    handoff = apply_handoff

    def create_artifact(
        self, agent_id: str, run_id: str | None = None, test_command: str | None = None
    ) -> dict:
        body = {k: v for k, v in {"run_id": run_id, "test_command": test_command}.items() if v}
        return self._check(
            self._mutate(
                "POST",
                f"/v1/agents/{agent_id}/artifacts",
                json=body,
                # A retry would snapshot a second artifact — list first.
                check=f"GET /v1/artifacts?agent_id={agent_id}",
            )
        )["artifact"]

    def list_artifacts(self, agent_id: str | None = None) -> list[dict]:
        params = {"agent_id": agent_id} if agent_id else {}
        return self._check(self._request("GET", "/v1/artifacts", params=params))["artifacts"]

    def get_artifact(self, artifact_id: str) -> dict:
        return self._check(self._request("GET", f"/v1/artifacts/{artifact_id}"))

    def download_artifact(self, artifact_id: str, member: str = "patch.diff") -> bytes:
        resp = self._request(
            "GET", f"/v1/artifacts/{artifact_id}/download", params={"member": member}
        )
        if resp.status_code >= 400:
            self._check(resp)
        return resp.content

    def models(self) -> list[dict[str, Any]]:
        return self._check(self._request("GET", "/v1/models"))["models"]

    def me(self) -> dict[str, Any]:
        return self._check(self._request("GET", "/v1/me"))

    def openapi(self) -> dict[str, Any]:
        """The runtime-derived public OpenAPI document (SOR-226)."""
        return self._check(self._request("GET", "/v1/openapi.json"))

    # ------------------------------------------------------------ waiting

    def wait(
        self,
        agent_id: str,
        run_id: str,
        *,
        timeout_s: float = 300.0,
        poll_s: float = 2.0,
    ) -> dict[str, Any]:
        """Poll ``GET .../runs/{runId}`` until a persisted terminal status.

        Returns the last observed run payload. When ``timeout_s`` elapses the
        status may still be non-terminal — it is returned as observed, never
        inferred; callers inspect ``run["status"]``. A transient transport
        failure costs one poll, not the whole wait; when no GET ever
        succeeded the last transport error is raised.
        """
        deadline = time.monotonic() + timeout_s
        last_run: dict[str, Any] | None = None
        last_error: SbxTransportError | None = None
        while True:
            try:
                run = self.get_run(agent_id, run_id)
            except SbxTransportError as exc:
                last_error = exc
            else:
                last_run = run
                if run.get("status") in RUN_DONE:
                    return run
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last_run is not None:
                    return last_run
                assert last_error is not None
                raise last_error
            time.sleep(min(poll_s, remaining))

    def wait_many(
        self,
        runs: Iterable[Any],
        *,
        timeout_s: float = 300.0,
        poll_s: float = 2.0,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Wait on many runs under one total ``timeout_s`` budget.

        ``runs`` accepts ``(agent_id, run_id)`` pairs, run dicts or
        :class:`Run` models (anything with ``agent_id`` + ``id``). Runs
        finish in any order; results are keyed by ``(agent_id, run_id)`` so
        an ``ERROR`` or ``CANCELLED`` outcome is reported independently for
        that run. On timeout the mapping carries each unfinished run's last
        observed status; a run whose GET never succeeded is absent.
        """
        pending = {self._run_key(r) for r in runs}
        deadline = time.monotonic() + timeout_s
        done: dict[tuple[str, str], dict[str, Any]] = {}
        while pending:
            for handle in sorted(pending):
                try:
                    run = self.get_run(*handle)
                except (SbxApiError, SbxTransportError):
                    continue
                done[handle] = run
                if run.get("status") in RUN_DONE:
                    pending.discard(handle)
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_s, remaining))
        return done

    @staticmethod
    def _run_key(run: Any) -> tuple[str, str]:
        if isinstance(run, dict):
            return str(run["agent_id"]), str(run["id"])
        if isinstance(run, (tuple, list)) and len(run) == 2:
            return str(run[0]), str(run[1])
        return str(run.agent_id), str(run.id)

    def _run_done(self, agent_id: str, run_id: str) -> bool:
        try:
            return self.get_run(agent_id, run_id).get("status") in RUN_DONE
        except Exception:
            return False

    # ------------------------------------------------------------- stream

    def stream_run(
        self,
        agent_id: str,
        run_id: str,
        last_event_id: str | int | None = None,
        *,
        read_timeout_s: float | None = 90.0,
    ) -> Iterator[SseEvent]:
        """Single SSE pass yielding ``SseEvent`` until the stream ends.

        No reconnect — ``watch`` adds ``Last-Event-ID`` resume and the
        terminal fallback. ``last_event_id`` resumes after that line.
        ``read_timeout_s`` is the inactivity gap bound (server keepalives
        reset it); ``None`` opts back into unbounded reads.
        """
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        with self.http.stream(
            "GET",
            f"/v1/agents/{agent_id}/runs/{run_id}/stream",
            headers=headers,
            timeout=self._stream_timeout(read_timeout_s),
        ) as resp:
            resp.raise_for_status()
            for frame in _sse_frames(resp.iter_lines()):
                if isinstance(frame, SseEvent):
                    yield frame

    def watch(
        self,
        agent_id: str,
        run_id: str,
        *,
        last_event_id: str | int | None = None,
        max_reconnects: int = 8,
        backoff_s: float = 0.5,
        status_poll_s: float | None = 30.0,
        read_timeout_s: float | None = 90.0,
    ) -> Iterator[SseEvent]:
        """Yield a run's events, resuming with ``Last-Event-ID`` after drops.

        Stop conditions — whichever comes first: a ``sbx.turn_finished`` /
        ``sbx.error`` frame; the run reporting a persisted terminal status on
        a periodic GET check (after ``status_poll_s`` of event silence, and
        on every reconnect); or ``max_reconnects`` consecutive failed
        reconnects — retention expiry or an unreachable stream is never
        retried forever. When the iterator ends without a terminal frame,
        ``wait``/``get_run`` is the fallback for the persisted outcome.

        Closing the generator (``break``, ``.close()``, KeyboardInterrupt)
        only closes the local HTTP stream — it never cancels the remote run.
        """
        path = f"/v1/agents/{agent_id}/runs/{run_id}/stream"
        last_id = str(last_event_id) if last_event_id is not None else None
        timeout = self._stream_timeout(read_timeout_s)
        attempts = 0
        while True:
            try:
                headers = {"Accept": "text/event-stream"}
                if last_id is not None:
                    headers["Last-Event-ID"] = last_id
                with self.http.stream("GET", path, headers=headers, timeout=timeout) as resp:
                    if resp.status_code >= 400:
                        # 4xx is definitive unless the run already went
                        # terminal; 5xx is transient → bounded retry.
                        if resp.status_code < 500 and not self._run_done(agent_id, run_id):
                            resp.read()
                            self._check(resp)
                        raise _StreamRetry(f"stream status {resp.status_code}")
                    last_event_at = time.monotonic()
                    for frame in _sse_frames(resp.iter_lines()):
                        if isinstance(frame, str):
                            # Keepalive tick: poll the persisted status when
                            # the stream has gone event-silent — a terminal
                            # run with rotated/lost events.jsonl would
                            # otherwise keepalive forever.
                            if (
                                status_poll_s is not None
                                and time.monotonic() - last_event_at >= status_poll_s
                            ):
                                if self._run_done(agent_id, run_id):
                                    return
                                last_event_at = time.monotonic()
                            continue
                        attempts = 0
                        last_event_at = time.monotonic()
                        if frame.id is not None:
                            last_id = frame.id
                        yield frame
                        if frame.type in RUN_END_EVENTS:
                            return
            except (_StreamRetry, httpx.TransportError):
                pass
            # Stream ended or failed: persisted-terminal check, then a
            # bounded reconnect carrying Last-Event-ID.
            if self._run_done(agent_id, run_id):
                return
            attempts += 1
            if attempts > max_reconnects:
                return
            time.sleep(min(backoff_s * attempts, 5.0))

    def resume(
        self,
        agent_id: str,
        run_id: str | None = None,
        *,
        last_event_id: str | int | None = None,
        **watch_kwargs: Any,
    ) -> Iterator[SseEvent]:
        """Re-attach to a run's event stream — the post-``recover`` seam.

        ``run_id=None`` selects the agent's latest run. ``last_event_id``
        continues from a checkpoint saved before a client restart; without
        one the server replays the run's slice from the start.
        """
        if run_id is None:
            runs = self.list_runs(agent_id)
            if not runs:
                raise SbxApiError(404, "not_found", f"agent {agent_id} has no runs")
            run_id = runs[-1]["id"]
        return self.watch(agent_id, run_id, last_event_id=last_event_id, **watch_kwargs)

    # ----------------------------------------------------------- workflow

    def recover(self, workflow_id: str) -> WorkflowRecovery:
        """Re-discover a workflow from ``API key + workflow_id`` alone.

        Lists agents filtered by ``workflow_id`` and keeps only those whose
        persisted ``metadata.workflow_id`` matches (SOR-84/C1) — an agent
        without matching metadata is never claimed, so recovery against a
        pre-metadata server safely finds nothing rather than everything.
        """
        agents: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self.list_agents(workflow_id=workflow_id, cursor=cursor)
            agents.extend(page.get("agents") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        mine = [
            agent
            for agent in agents
            if (agent.get("metadata") or {}).get("workflow_id") == workflow_id
        ]
        runs: dict[str, list[dict[str, Any]]] = {}
        latest: dict[str, dict[str, Any]] = {}
        for agent in mine:
            try:
                agent_runs = self.list_runs(agent["id"])
            except SbxApiError:
                # The agent disappeared between listing and detail — recovery
                # reports what is still there rather than failing wholesale.
                agent_runs = []
            runs[agent["id"]] = agent_runs
            if agent_runs:
                latest[agent["id"]] = agent_runs[-1]
        return WorkflowRecovery(workflow_id=workflow_id, agents=mine, runs=runs, latest_runs=latest)

    def close_workflow(self, workflow_id: str) -> list[dict[str, Any]]:
        """Scoped cleanup: close only the agents recovered for ``workflow_id``.

        Idempotent — ``DELETE`` on an already-closed agent returns its closed
        record. Agents outside the workflow are never touched.
        """
        return [self.delete_agent(a["id"]) for a in self.recover(workflow_id).agents]


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


class _Tasks:
    """Typed task operations (SOR-222-226) — ``client.tasks``.

    Every method wraps a dedicated ``/v1/tasks`` route; no raw ``http``
    access is needed for the task lifecycle: create → run → review →
    deliver → merge.
    """

    def __init__(self, client: SbxClient) -> None:
        self._client = client

    @staticmethod
    def _task_body(
        prompt: str | dict[str, Any],
        *,
        name: str | None = None,
        source: dict[str, Any] | None = None,
        execution: dict[str, Any] | None = None,
        delivery: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        output_contract: dict[str, Any] | None = None,
        resources: dict[str, Any] | None = None,
        compute: dict[str, Any] | None = None,
        idle_timeout_s: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"prompt": {"text": prompt} if isinstance(prompt, str) else prompt}
        if name is not None:
            body["name"] = name
        if source is not None:
            body["source"] = dict(source)
        if execution is not None:
            body["execution"] = dict(execution)
        if delivery is not None:
            body["delivery"] = dict(delivery)
        if metadata is not None:
            body["metadata"] = dict(metadata)
        if output_contract is not None:
            body["output_contract"] = dict(output_contract)
        if resources is not None:
            body["resources"] = dict(resources)
        if compute is not None:
            body["compute"] = dict(compute)
        if idle_timeout_s is not None:
            body["idle_timeout_s"] = idle_timeout_s
        return body

    def create(
        self,
        prompt: str | dict[str, Any],
        *,
        idempotency_key: str | bool | None = None,
        **kwargs: Any,
    ) -> TaskCreated:
        """Create a task — ``POST /v1/tasks``.

        Returns :class:`TaskCreated` (``task`` + ``agent`` + first ``run``).
        A fresh ``Idempotency-Key`` is generated automatically so a
        transport-failed create retries safely; pass ``idempotency_key=``
        to pin your own or ``False`` to opt out.
        """
        c = self._client
        data = c._check(
            c._mutate(
                "POST",
                "/v1/tasks",
                json=self._task_body(prompt, **kwargs),
                check="GET /v1/tasks",
                keyed=True,
                idempotency_key=idempotency_key,
            )
        )
        return TaskCreated.from_dict(data)

    def preflight(self, prompt: str | dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Advisory resolution — ``POST /v1/tasks/preflight`` (no side effects)."""
        c = self._client
        return c._check(
            c._request("POST", "/v1/tasks/preflight", json=self._task_body(prompt, **kwargs))
        )

    def get(self, task_id: str) -> TaskDetail:
        """Task detail — ``GET /v1/tasks/{id}`` (task + agent + run views)."""
        c = self._client
        return TaskDetail.from_dict(c._check(c._request("GET", f"/v1/tasks/{task_id}")))

    def list(self, **params: Any) -> list[Task]:
        """The caller's tasks — ``GET /v1/tasks``."""
        c = self._client
        data = c._check(c._request("GET", "/v1/tasks", params=_clean(params)))
        return [Task(raw=dict(t)) for t in data.get("tasks") or []]

    def wait(self, task_id: str, *, timeout_s: float = 300.0, poll_s: float = 2.0) -> Task:
        """Poll ``GET /v1/tasks/{id}`` until the task reaches a terminal status.

        Same contract as ``wait`` on runs: returns the last observed task —
        on timeout it may still be non-terminal; inspect ``task.status``
        (``task.terminal``) rather than assuming success. A transient
        transport failure costs one poll; when no GET ever succeeded the
        last transport error is raised.
        """
        deadline = time.monotonic() + timeout_s
        last_task: Task | None = None
        last_error: SbxTransportError | None = None
        while True:
            try:
                task = self.get(task_id).task
            except SbxTransportError as exc:
                last_error = exc
            else:
                last_task = task
                if task.status in TASK_TERMINAL:
                    return task
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last_task is not None:
                    return last_task
                assert last_error is not None
                raise last_error
            time.sleep(min(poll_s, remaining))

    def followup(
        self,
        task_id: str,
        text: str,
        *,
        on_busy: str | None = None,
        metadata: dict[str, Any] | None = None,
        output_contract: dict[str, Any] | None = None,
        idempotency_key: str | bool | None = None,
    ) -> Run:
        """Queue a follow-up run on the task's agent — ``POST /v1/tasks/{id}/runs``."""
        c = self._client
        body: dict[str, Any] = {"prompt": {"text": text}}
        if on_busy is not None:
            body["on_busy"] = on_busy
        if metadata is not None:
            body["metadata"] = dict(metadata)
        if output_contract is not None:
            body["output_contract"] = dict(output_contract)
        data = c._check(
            c._mutate(
                "POST",
                f"/v1/tasks/{task_id}/runs",
                json=body,
                check=f"GET /v1/tasks/{task_id}/runs",
                keyed=True,
                idempotency_key=idempotency_key,
            )
        )
        return Run(raw=dict(data.get("run") or {}))

    def runs(self, task_id: str) -> list[Run]:
        """Every run the task has allocated — ``GET /v1/tasks/{id}/runs``."""
        c = self._client
        data = c._check(c._request("GET", f"/v1/tasks/{task_id}/runs"))
        return [Run(raw=dict(r)) for r in data.get("runs") or []]

    def cancel(self, task_id: str) -> TaskDetail:
        """Cancel the task's outstanding work — ``POST /v1/tasks/{id}/cancel``.

        Idempotent: an already-cancelled or terminal task replays its state.
        """
        c = self._client
        data = c._check(
            c._request(
                "POST",
                f"/v1/tasks/{task_id}/cancel",
                check=f"GET /v1/tasks/{task_id}",
                idempotent=True,
            )
        )
        return TaskDetail.from_dict(data)

    def retry(
        self,
        task_id: str,
        *,
        mode: str | None = None,
        prompt: str | dict[str, Any] | None = None,
        on_busy: str | None = None,
        idempotency_key: str | bool | None = None,
    ) -> TaskDetail:
        """Re-run a failed/deliverable task — ``POST /v1/tasks/{id}/retry``.

        ``mode`` picks the retry lane (``"run"`` / ``"delivery"``).
        """
        c = self._client
        body: dict[str, Any] = {}
        if mode is not None:
            body["mode"] = mode
        if prompt is not None:
            body["prompt"] = {"text": prompt} if isinstance(prompt, str) else prompt
        if on_busy is not None:
            body["on_busy"] = on_busy
        data = c._check(
            c._mutate(
                "POST",
                f"/v1/tasks/{task_id}/retry",
                json=body,
                check=f"GET /v1/tasks/{task_id}",
                idempotency_key=idempotency_key,
            )
        )
        return TaskDetail.from_dict(data)

    def delivery(self, task_id: str) -> TaskDetail:
        """Execute the task's delivery policy now — ``POST /v1/tasks/{id}/delivery``.

        Create-or-update idempotent: replaying an already-delivered task
        returns its state.
        """
        c = self._client
        data = c._check(
            c._request(
                "POST",
                f"/v1/tasks/{task_id}/delivery",
                check=f"GET /v1/tasks/{task_id}",
                idempotent=True,
            )
        )
        return TaskDetail.from_dict(data)

    def deliver(
        self,
        task_id: str,
        *,
        revision: str | None = None,
        branch: str | None = None,
        pull_request: dict[str, Any] | None = None,
        idempotency_key: str | bool | None = None,
    ) -> Revision:
        """Deliver a revision (push + open/update PR) — ``POST /v1/tasks/{id}/deliver``."""
        c = self._client
        body: dict[str, Any] = {}
        if revision is not None:
            body["revision"] = revision
        if branch is not None:
            body["branch"] = branch
        if pull_request is not None:
            body["pull_request"] = dict(pull_request)
        data = c._check(
            c._mutate(
                "POST",
                f"/v1/tasks/{task_id}/deliver",
                json=body,
                check=f"GET /v1/tasks/{task_id}/revisions",
                idempotency_key=idempotency_key,
            )
        )
        return Revision(raw=dict(data.get("revision") or {}))

    def revisions(self, task_id: str) -> list[Revision]:
        """The task's durable revisions — ``GET /v1/tasks/{id}/revisions``."""
        c = self._client
        data = c._check(c._request("GET", f"/v1/tasks/{task_id}/revisions"))
        return [Revision(raw=dict(r)) for r in data.get("revisions") or []]

    def revision(self, task_id: str, ref: str = "latest") -> Revision:
        """One revision by id/n/``latest`` — ``GET /v1/tasks/{id}/revisions/{ref}``."""
        c = self._client
        data = c._check(c._request("GET", f"/v1/tasks/{task_id}/revisions/{ref}"))
        return Revision(raw=dict(data.get("revision") or {}))

    def reviews(self, task_id: str, *, revision: str | None = None) -> list[Review]:
        """Durable reviews on the task's revisions — ``GET /v1/tasks/{id}/reviews``."""
        c = self._client
        data = c._check(
            c._request("GET", f"/v1/tasks/{task_id}/reviews", params=_clean({"revision": revision}))
        )
        return [Review(raw=dict(r)) for r in data.get("reviews") or []]

    def review(
        self,
        task_id: str,
        *,
        verdict: str,
        revision: str | None = None,
        findings: list[dict[str, Any]] | None = None,
        reviewer: dict[str, Any] | None = None,
        comment: str | None = None,
    ) -> Review:
        """Record a durable review — ``POST /v1/tasks/{id}/reviews``.

        ``verdict`` ∈ ``approve`` | ``request_changes`` | ``comment``;
        ``reviewer`` = ``{"identity"?, "agent_id"?, "run_id"?}`` (identity
        defaults to the calling key). ``comment`` also posts on the
        delivered pull request.
        """
        c = self._client
        body: dict[str, Any] = {"verdict": verdict}
        if revision is not None:
            body["revision"] = revision
        if findings is not None:
            body["findings"] = list(findings)
        if reviewer is not None:
            body["reviewer"] = dict(reviewer)
        if comment is not None:
            body["comment"] = comment
        data = c._check(
            c._mutate(
                "POST",
                f"/v1/tasks/{task_id}/reviews",
                json=body,
                check=f"GET /v1/tasks/{task_id}/reviews",
            )
        )
        return Review(raw=dict(data.get("review") or {}))

    def merge(
        self,
        task_id: str,
        *,
        revision: str | None = None,
    ) -> Revision:
        """Merge the revision's delivered PR — ``POST /v1/tasks/{id}/merge``.

        Review-gated: requires a non-stale independent ``approve`` pinned to
        the exact revision head.
        """
        c = self._client
        body: dict[str, Any] = {}
        if revision is not None:
            body["revision"] = revision
        data = c._check(
            c._mutate(
                "POST",
                f"/v1/tasks/{task_id}/merge",
                json=body,
                check=f"GET /v1/tasks/{task_id}/revisions",
            )
        )
        return Revision(raw=dict(data.get("revision") or {}))


class _Agents:
    """Typed agent operations — ``client.agents``."""

    def __init__(self, client: SbxClient) -> None:
        self._client = client

    def create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Create an agent + first run — see :meth:`SbxClient.create_agent`."""
        return self._client.create_agent(*args, **kwargs)

    def get(self, agent_id: str) -> Agent:
        return Agent(raw=self._client.get_agent(agent_id))

    def list(self, **params: Any) -> list[Agent]:
        data = self._client.list_agents(**params)
        return [Agent(raw=dict(a)) for a in data.get("agents") or []]

    def close(self, agent_id: str) -> Agent:
        return Agent(raw=self._client.close_agent(agent_id))

    def usage(self, agent_id: str) -> dict[str, Any]:
        return self._client.usage(agent_id)

    def workspace(self, agent_id: str) -> dict[str, Any]:
        return self._client.get_workspace(agent_id)

    def review_workspace(self, agent_id: str, head_sha: str | None = None) -> dict[str, Any]:
        return self._client.review_workspace(agent_id, head_sha)

    def handoff(
        self,
        agent_id: str,
        *,
        artifact_id: str | None = None,
        head_sha: str | None = None,
        pull_request: dict[str, Any] | None = None,
        task_id: str | None = None,
        revision: str | None = None,
        pr_url: str | None = None,
        workspace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Attach a cross-agent handoff — ``POST /v1/agents/{id}/handoff``.

        Carries the :class:`HandoffRef` fields verbatim: a source
        ``artifact_id``/``head_sha`` (or PR/task/revision pin) to base this
        agent's workspace on. Returns the updated workspace record.
        """
        c = self._client
        body = _clean(
            {
                "artifact_id": artifact_id,
                "head_sha": head_sha,
                "pull_request": pull_request,
                "task_id": task_id,
                "revision": revision,
                "pr_url": pr_url,
                "workspace": workspace,
            }
        )
        return c._check(
            c._mutate(
                "POST",
                f"/v1/agents/{agent_id}/handoff",
                json=body,
                check=f"GET /v1/agents/{agent_id}/workspace",
            )
        )["workspace"]

    def revisions(self, agent_id: str) -> list[Revision]:
        """Durable revisions from this agent — ``GET /v1/agents/{id}/revisions``."""
        c = self._client
        data = c._check(c._request("GET", f"/v1/agents/{agent_id}/revisions"))
        return [Revision(raw=dict(r)) for r in data.get("revisions") or []]


class _Runs:
    """Typed run operations — ``client.runs``."""

    def __init__(self, client: SbxClient) -> None:
        self._client = client

    def get(self, agent_id: str, run_id: str) -> Run:
        return Run(raw=self._client.get_run(agent_id, run_id))

    def list(self, agent_id: str) -> list[Run]:
        return [Run(raw=dict(r)) for r in self._client.list_runs(agent_id)]

    def create(self, agent_id: str, text: str, **kwargs: Any) -> Run:
        """Queue a run on the agent — ``POST /v1/agents/{id}/runs``."""
        return Run(raw=self._client.followup(agent_id, text, **kwargs))

    def cancel(self, agent_id: str, run_id: str) -> Run:
        return Run(raw=self._client.cancel_run(agent_id, run_id))

    def wait(self, agent_id: str, run_id: str, **kwargs: Any) -> Run:
        return Run(raw=self._client.wait(agent_id, run_id, **kwargs))

    def watch(self, agent_id: str, run_id: str, **kwargs: Any) -> Iterator[SseEvent]:
        return self._client.watch(agent_id, run_id, **kwargs)

    def stream(self, agent_id: str, run_id: str, **kwargs: Any) -> Iterator[SseEvent]:
        return self._client.stream_run(agent_id, run_id, **kwargs)

    def resume(self, agent_id: str, run_id: str | None = None, **kwargs: Any) -> Iterator[SseEvent]:
        return self._client.resume(agent_id, run_id, **kwargs)

    def wait_many(self, runs: Iterable[Any], **kwargs: Any) -> dict[tuple[str, str], Run]:
        return {
            handle: Run(raw=run) for handle, run in self._client.wait_many(runs, **kwargs).items()
        }


class _Artifacts:
    """SOR-83 artifact seam (``client.artifacts``) over the wired routes.

    ``GET /v1/artifacts`` is owner-scoped and filtered by ``agent_id``; a
    ``run_id`` filter applies client-side on ``producer.run_id``. Downloads
    are top-level (``GET /v1/artifacts/{id}/download``) since artifacts
    outlive the producing sandbox.
    """

    def __init__(self, client: SbxClient) -> None:
        self._client = client

    def list(self, agent_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        """Artifact descriptors for an agent (optionally scoped to a run)."""
        body = self._client._check(
            self._client._request("GET", "/v1/artifacts", params={"agent_id": agent_id})
        )
        artifacts = list(body.get("artifacts") or [])
        if run_id is not None:
            artifacts = [a for a in artifacts if (a.get("producer") or {}).get("run_id") == run_id]
        return artifacts

    def get(self, artifact_id: str) -> dict[str, Any]:
        return self._client.get_artifact(artifact_id)

    def create(
        self, agent_id: str, run_id: str | None = None, test_command: str | None = None
    ) -> dict[str, Any]:
        return self._client.create_artifact(agent_id, run_id=run_id, test_command=test_command)

    def download(
        self,
        agent_id: str,
        artifact_id: str,
        dest: str | Path | None = None,
        member: str = "patch.diff",
    ) -> bytes:
        """Download an artifact's bytes, optionally writing them to ``dest``.

        Artifacts persist independently of the sandbox — download does not
        require the producing agent's sandbox to still exist. ``agent_id``
        is accepted for call-site symmetry; the route is artifact-scoped.
        """
        data = self._client.download_artifact(artifact_id, member=member)
        if dest is not None:
            Path(dest).write_bytes(data)
        return data


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Create hello.txt containing 'hi'."
    client = SbxClient()
    try:
        created = client.create_agent(prompt, provider="codex")
        agent, run = created["agent"], created["run"]
        print(f"agent {agent['id']} ({agent['status']}); first run {run['id']} ({run['status']})")
        for event in client.watch(agent["id"], run["id"]):
            if event.type.startswith("sbx."):
                print(f"  event {event.id} {event.type}")
        run = client.wait(agent["id"], run["id"])
        result_text = (run.get("result") or {}).get("text", "")[:80]
        print(f"run {run['id']} -> {run['status']}: {result_text}")
        follow = client.followup(agent["id"], "Now append a second line.")
        print(f"follow-up {follow['id']} ({follow['status']}); cancelling")
        cancelled = client.cancel(agent["id"], follow["id"])
        print(f"cancelled -> {cancelled['status']}")
        print("usage:", json.dumps(client.usage(agent["id"])))
        closed = client.close_agent(agent["id"])
        print(f"agent {closed['id']} -> {closed['status']}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
