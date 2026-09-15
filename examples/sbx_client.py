#!/usr/bin/env python3
"""sbx-browser public ``/v1`` client with recoverable orchestration helpers.

Thin HTTP surface plus the SOR-84/C2 SDK verbs:

* ``SseEvent`` — stable SSE frame object preserving ``id`` / ``type`` /
  ``data`` (the raw ``id:`` field is the ``events.jsonl`` line number; feed
  it back via ``last_event_id`` / ``Last-Event-ID`` to resume after it).
* ``watch`` — yields ``SseEvent`` with automatic ``Last-Event-ID`` reconnect,
  a bounded reconnect budget, then a persisted-terminal GET fallback — never
  an infinite retry. Interrupting ``watch`` only detaches the local stream;
  it never cancels remote work.
* ``create`` / ``followup`` / ``wait`` / ``wait_many`` / ``resume`` /
  ``cancel`` / ``close_agent`` / ``close_workflow``.
* ``recover(workflow_id)`` — client-side seam over the SOR-84/C1 workflow
  metadata: agents whose persisted ``metadata.workflow_id`` matches, plus
  their latest runs and artifact refs, so a fresh process can re-attach with
  only an API key and the workflow id.
* ``client.artifacts.list`` / ``client.artifacts.download`` — SOR-83 seam
  over ``GET /v1/artifacts`` and ``GET /v1/artifacts/{id}/download``.

Usage:
    SBX_API_KEY=sbx_... SBX_BASE_URL=https://sbx.sorenforge.com \
        python examples/sbx_client.py "Write hello.txt containing hi"

Only dependency: httpx.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://sbx.sorenforge.com"

# ``RunStatus`` from docs/contracts/api-v1.yaml: FINISHED/ERROR/CANCELLED/
# EXPIRED are persisted terminal states that never change once written.
RUN_TERMINAL = frozenset({"FINISHED", "ERROR", "CANCELLED", "EXPIRED"})
# UNKNOWN = outcome unavailable (no persisted terminal state, no readable
# evidence): a final answer waiting cannot improve, never inferred success.
RUN_DONE = RUN_TERMINAL | {"UNKNOWN"}
# Canonical runner events that close a run's SSE slice.
RUN_END_EVENTS = frozenset({"sbx.turn_finished", "sbx.error"})


class SbxApiError(RuntimeError):
    """Canonical ``{error:{code,message,retry_after?}}`` body as an exception."""

    def __init__(
        self,
        status: int,
        code: str | None,
        message: str | None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.retry_after = retry_after


class _StreamRetry(Exception):
    """Internal: reopen the SSE stream (bounded by ``max_reconnects``)."""


@dataclass(frozen=True)
class SseEvent:
    """One SSE frame from ``GET .../runs/{runId}/stream``.

    ``id`` is the verbatim ``id:`` field, ``type`` the ``event:`` field and
    ``data`` the decoded ``data:`` JSON payload.
    """

    id: str | None
    type: str
    data: Any


@dataclass
class WorkflowRecovery:
    """``SbxClient.recover`` result: a workflow's agents and their runs.

    ``runs`` maps agent id → ordered run payloads; ``latest_runs`` holds the
    last run per agent — the one ``resume``/``wait`` would re-attach to.
    """

    workflow_id: str
    agents: list[dict[str, Any]]
    runs: dict[str, list[dict[str, Any]]]
    latest_runs: dict[str, dict[str, Any]]

    def handles(self, *, latest_only: bool = True) -> list[tuple[str, str]]:
        """``(agent_id, run_id)`` pairs ready for ``wait_many``/``resume``."""
        if latest_only:
            return [(agent_id, run["id"]) for agent_id, run in self.latest_runs.items()]
        return [(agent_id, run["id"]) for agent_id, runs in self.runs.items() for run in runs]

    def artifact_refs(self) -> dict[str, list[str]]:
        """Latest-run ``artifact_refs`` per agent (SOR-83 handoff inputs)."""
        return {
            agent_id: list(run.get("artifact_refs") or [])
            for agent_id, run in self.latest_runs.items()
        }


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
    """Public ``/v1`` client. Pass ``client=`` to inject a transport."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns = client is None
        self.http = client or httpx.Client(
            base_url=(base_url or os.environ.get("SBX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            headers={"Authorization": f"Bearer {api_key or os.environ['SBX_API_KEY']}"},
            timeout=None,
        )
        self.artifacts = _Artifacts(self)

    def close(self) -> None:
        """Close the owned HTTP client. Never touches remote state."""
        if self._owns:
            self.http.close()

    def __enter__(self) -> SbxClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

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
            )
        return resp.json() if resp.content else {}

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
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create an agent and queue its first run (SOR-82: may return while
        the run is still ``CREATING`` — poll ``wait`` for the terminal state).

        ``metadata`` (``workflow_id`` / ``task_id`` / ``role`` /
        ``parent_task_id``), ``workspace`` and ``handoff`` are passed through
        for the SOR-84/C1 and SOR-83 server seams; pre-seam servers ignore
        them. ``idempotency_key`` maps to the ``Idempotency-Key`` contract —
        a retried key replays the original response instead of
        double-creating.
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
            # SOR-83: {"repo", "base_ref", "base_sha"} — the run starts on
            # this exact checkout; a wrong base_sha fails explicitly.
            body["workspace"] = dict(workspace)
        if handoff:
            # {"artifact_id": ...} or {"head_sha": ...} — cross-agent handoff.
            body["handoff"] = dict(handoff)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._check(self.http.post("/v1/agents", json=body, headers=headers))

    def create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Orchestration alias for ``create_agent``."""
        return self.create_agent(*args, **kwargs)

    def list_agents(self, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._check(self.http.get("/v1/agents", params=clean))

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._check(self.http.get(f"/v1/agents/{agent_id}"))

    def delete_agent(self, agent_id: str) -> dict[str, Any]:
        return self._check(self.http.delete(f"/v1/agents/{agent_id}"))

    def close_agent(self, agent_id: str) -> dict[str, Any]:
        """Close an agent — reclaim the sandbox; history stays read-only."""
        return self.delete_agent(agent_id)

    # --------------------------------------------------------------- runs

    def followup(
        self, agent_id: str, text: str, *, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Queue a follow-up run on an existing agent (``POST .../runs``)."""
        body: dict[str, Any] = {"prompt": {"text": text}}
        if metadata:
            body["metadata"] = dict(metadata)
        return self._check(self.http.post(f"/v1/agents/{agent_id}/runs", json=body))

    def create_run(self, agent_id: str, text: str) -> dict[str, Any]:
        return self.followup(agent_id, text)

    def list_runs(self, agent_id: str) -> list[dict[str, Any]]:
        return self._check(self.http.get(f"/v1/agents/{agent_id}/runs"))["runs"]

    def get_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return self._check(self.http.get(f"/v1/agents/{agent_id}/runs/{run_id}"))

    def cancel_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return self._check(self.http.post(f"/v1/agents/{agent_id}/runs/{run_id}/cancel"))

    def cancel(self, agent_id: str, run_id: str) -> dict[str, Any]:
        """Explicitly cancel a run — the only cancel path; ``watch`` never does."""
        return self.cancel_run(agent_id, run_id)

    def usage(self, agent_id: str) -> dict[str, Any]:
        return self._check(self.http.get(f"/v1/agents/{agent_id}/usage"))

    # --- SOR-83: workspaces, handoffs, artifacts ---------------------------

    def get_workspace(self, agent_id: str) -> dict:
        return self._check(self.http.get(f"/v1/agents/{agent_id}/workspace"))["workspace"]

    def review_workspace(self, agent_id: str, head_sha: str | None = None) -> dict:
        body = {"head_sha": head_sha} if head_sha else {}
        return self._check(self.http.post(f"/v1/agents/{agent_id}/workspace/review", json=body))[
            "workspace"
        ]

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
        return self._check(self.http.post(f"/v1/agents/{agent_id}/handoff", json=body))["workspace"]

    def create_artifact(
        self, agent_id: str, run_id: str | None = None, test_command: str | None = None
    ) -> dict:
        body = {k: v for k, v in {"run_id": run_id, "test_command": test_command}.items() if v}
        return self._check(self.http.post(f"/v1/agents/{agent_id}/artifacts", json=body))[
            "artifact"
        ]

    def list_artifacts(self, agent_id: str | None = None) -> list[dict]:
        params = {"agent_id": agent_id} if agent_id else {}
        return self._check(self.http.get("/v1/artifacts", params=params))["artifacts"]

    def get_artifact(self, artifact_id: str) -> dict:
        return self._check(self.http.get(f"/v1/artifacts/{artifact_id}"))

    def download_artifact(self, artifact_id: str, member: str = "patch.diff") -> bytes:
        resp = self.http.get(f"/v1/artifacts/{artifact_id}/download", params={"member": member})
        if resp.status_code >= 400:
            self._check(resp)
        return resp.content

    def models(self) -> list[dict[str, Any]]:
        return self._check(self.http.get("/v1/models"))["models"]

    def me(self) -> dict[str, Any]:
        return self._check(self.http.get("/v1/me"))

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
        inferred; callers inspect ``run["status"]``.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            run = self.get_run(agent_id, run_id)
            if run.get("status") in RUN_DONE:
                return run
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return run
            time.sleep(min(poll_s, remaining))

    def wait_many(
        self,
        runs: Iterable[Any],
        *,
        timeout_s: float = 300.0,
        poll_s: float = 2.0,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Wait on many runs under one total ``timeout_s`` budget.

        ``runs`` accepts ``(agent_id, run_id)`` pairs or run dicts (anything
        with ``agent_id`` + ``id``). Runs finish in any order; results are
        keyed by ``(agent_id, run_id)`` so an ``ERROR`` or ``CANCELLED``
        outcome is reported independently for that run. On timeout the
        mapping carries each unfinished run's last observed status; a run
        whose GET never succeeded is absent.
        """
        pending = {self._run_key(r) for r in runs}
        deadline = time.monotonic() + timeout_s
        done: dict[tuple[str, str], dict[str, Any]] = {}
        while pending:
            for handle in sorted(pending):
                try:
                    run = self.get_run(*handle)
                except SbxApiError:
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
    ) -> Iterator[SseEvent]:
        """Single SSE pass yielding ``SseEvent`` until the stream ends.

        No reconnect — ``watch`` adds ``Last-Event-ID`` resume and the
        terminal fallback. ``last_event_id`` resumes after that line.
        """
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        with self.http.stream(
            "GET", f"/v1/agents/{agent_id}/runs/{run_id}/stream", headers=headers
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
        timeout = httpx.Timeout(None, read=read_timeout_s)
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
            self._client.http.get("/v1/artifacts", params={"agent_id": agent_id})
        )
        artifacts = list(body.get("artifacts") or [])
        if run_id is not None:
            artifacts = [
                a for a in artifacts if (a.get("producer") or {}).get("run_id") == run_id
            ]
        return artifacts

    def download(
        self,
        agent_id: str,
        artifact_id: str,
        dest: str | Path | None = None,
    ) -> bytes:
        """Download an artifact's bytes, optionally writing them to ``dest``.

        Artifacts persist independently of the sandbox — download does not
        require the producing agent's sandbox to still exist. ``agent_id``
        is accepted for call-site symmetry; the route is artifact-scoped.
        """
        resp = self._client.http.get(f"/v1/artifacts/{artifact_id}/download")
        if resp.status_code >= 400:
            self._client._check(resp)
        data = resp.content
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
