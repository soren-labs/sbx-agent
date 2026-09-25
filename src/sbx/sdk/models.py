"""Typed models for the sbx-browser public ``/v1`` API (SOR-226).

Every model is a thin typed view over the wire payload: canonical fields are
exposed as typed attributes, and the full payload stays available as ``raw``
(plus ``Mapping``-style ``model["key"]`` / ``model.get`` access) so nothing
the server returns is ever lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ``RunStatus`` from docs/contracts/api-v1.yaml: FINISHED/ERROR/CANCELLED/
# EXPIRED are persisted terminal states that never change once written.
RUN_TERMINAL = frozenset({"FINISHED", "ERROR", "CANCELLED", "EXPIRED"})
# UNKNOWN = outcome unavailable (no persisted terminal state, no readable
# evidence): a final answer waiting cannot improve, never inferred success.
RUN_DONE = RUN_TERMINAL | {"UNKNOWN"}
# Canonical runner events that close a run's SSE slice.
RUN_END_EVENTS = frozenset({"sbx.turn_finished", "sbx.error"})

# Task statuses are lowercase (control/tasks.py); these are the ones that
# never move again — ``delivering``/``delivery_failed`` are delivery
# policy states and also terminal for ``tasks.wait``.
TASK_TERMINAL = frozenset({"finished", "error", "cancelled", "expired", "delivery_failed"})


@dataclass(frozen=True)
class SseEvent:
    """One SSE frame from ``GET .../runs/{runId}/stream``.

    ``id`` is the verbatim ``id:`` field, ``type`` the ``event:`` field and
    ``data`` the decoded ``data:`` JSON payload.
    """

    id: str | None
    type: str
    data: Any


class _Raw:
    """Payload-backed model base: ``.raw`` plus dict-style read access."""

    raw: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


@dataclass(frozen=True)
class RunError(_Raw):
    """Structured terminal error on a run (``error`` object)."""

    code: str
    source: str
    message: str
    retryable: bool
    retry_after: float | None
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RunError | None:
        if not data:
            return None
        return cls(
            code=str(data.get("code") or "unknown"),
            source=str(data.get("source") or "runtime"),
            message=str(data.get("message") or ""),
            retryable=bool(data.get("retryable")),
            retry_after=data.get("retry_after"),
            raw=dict(data),
        )


@dataclass(frozen=True)
class Run(_Raw):
    """A run (turn) payload — ``GET /v1/agents/{id}/runs/{runId}``."""

    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def agent_id(self) -> str:
        return str(self.raw["agent_id"])

    @property
    def status(self) -> str:
        return str(self.raw["status"])

    @property
    def terminal(self) -> bool:
        return self.status in RUN_DONE

    @property
    def error(self) -> RunError | None:
        return RunError.from_dict(self.raw.get("error"))

    @property
    def result(self) -> dict[str, Any] | None:
        return self.raw.get("result")

    @property
    def structured_output(self) -> Any:
        return self.raw.get("structured_output")

    @property
    def artifact_refs(self) -> list[str]:
        return list(self.raw.get("artifact_refs") or [])

    @property
    def queue_position(self) -> int | None:
        return self.raw.get("queue_position")

    @property
    def created_at(self) -> str:
        return str(self.raw.get("created_at") or "")

    @property
    def updated_at(self) -> str:
        return str(self.raw.get("updated_at") or "")


@dataclass(frozen=True)
class Agent(_Raw):
    """An agent (session) payload — ``GET /v1/agents/{id}``."""

    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def name(self) -> str | None:
        return self.raw.get("name")

    @property
    def provider(self) -> str | None:
        return self.raw.get("provider")

    @property
    def account_id(self) -> str | None:
        return self.raw.get("account_id")

    @property
    def model(self) -> str | None:
        return self.raw.get("model")

    @property
    def status(self) -> str:
        return str(self.raw["status"])

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self.raw.get("metadata") or {})

    @property
    def created_at(self) -> str:
        return str(self.raw.get("created_at") or "")

    @property
    def updated_at(self) -> str:
        return str(self.raw.get("updated_at") or "")


@dataclass(frozen=True)
class Delivery(_Raw):
    """A task's delivery projection (``task.delivery``)."""

    raw: dict[str, Any]

    @property
    def status(self) -> str | None:
        return self.raw.get("status")

    @property
    def required(self) -> bool:
        return bool(self.raw.get("required"))

    @property
    def branch(self) -> str | None:
        return self.raw.get("branch")

    @property
    def pushed_head_sha(self) -> str | None:
        return self.raw.get("pushed_head_sha")

    @property
    def pull_request(self) -> dict[str, Any] | None:
        return self.raw.get("pull_request")

    @property
    def merge(self) -> dict[str, Any] | None:
        return self.raw.get("merge")

    @property
    def error(self) -> dict[str, Any] | None:
        return self.raw.get("error")


@dataclass(frozen=True)
class Task(_Raw):
    """A task payload — ``GET /v1/tasks/{taskId}``'s ``task`` object."""

    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def status(self) -> str:
        return str(self.raw["status"])

    @property
    def terminal(self) -> bool:
        return self.status in TASK_TERMINAL

    @property
    def prompt(self) -> str | None:
        return self.raw.get("prompt")

    @property
    def agent_id(self) -> str | None:
        return self.raw.get("agent_id")

    @property
    def run_id(self) -> str | None:
        return self.raw.get("run_id")

    @property
    def request(self) -> dict[str, Any]:
        return dict(self.raw.get("request") or {})

    @property
    def resolved(self) -> dict[str, Any] | None:
        return self.raw.get("resolved")

    @property
    def delivery(self) -> Delivery | None:
        data = self.raw.get("delivery")
        return Delivery(raw=dict(data)) if data else None

    @property
    def revision(self) -> dict[str, Any] | None:
        return self.raw.get("revision")

    @property
    def transitions(self) -> list[dict[str, Any]]:
        return list(self.raw.get("transitions") or [])

    @property
    def created_at(self) -> str:
        return str(self.raw.get("created_at") or "")

    @property
    def updated_at(self) -> str:
        return str(self.raw.get("updated_at") or "")


@dataclass(frozen=True)
class TaskDetail(_Raw):
    """``GET /v1/tasks/{taskId}`` — the task plus live agent/run views."""

    task: Task
    agent: Agent | None
    run: Run | None
    runs: list[Run]
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskDetail:
        return cls(
            task=Task(raw=dict(data.get("task") or {})),
            agent=Agent(raw=dict(data["agent"])) if data.get("agent") else None,
            run=Run(raw=dict(data["run"])) if data.get("run") else None,
            runs=[Run(raw=dict(r)) for r in data.get("runs") or []],
            raw=dict(data),
        )


@dataclass(frozen=True)
class TaskCreated(_Raw):
    """``POST /v1/tasks`` 201 — task plus its queued agent and first run."""

    task: Task
    agent: Agent | None
    run: Run | None
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskCreated:
        return cls(
            task=Task(raw=dict(data.get("task") or {})),
            agent=Agent(raw=dict(data["agent"])) if data.get("agent") else None,
            run=Run(raw=dict(data["run"])) if data.get("run") else None,
            raw=dict(data),
        )


@dataclass(frozen=True)
class Revision(_Raw):
    """A revision payload — ``GET /v1/tasks/{id}/revisions[/{ref}]``."""

    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def n(self) -> int | None:
        return self.raw.get("n")

    @property
    def agent_id(self) -> str | None:
        return self.raw.get("agent_id")

    @property
    def task_id(self) -> str | None:
        return self.raw.get("task_id")

    @property
    def run_id(self) -> str | None:
        return self.raw.get("run_id")

    @property
    def artifact_id(self) -> str | None:
        return self.raw.get("artifact_id")

    @property
    def repo(self) -> str | None:
        return self.raw.get("repo")

    @property
    def base_sha(self) -> str | None:
        return self.raw.get("base_sha")

    @property
    def head_sha(self) -> str | None:
        return self.raw.get("head_sha")

    @property
    def status(self) -> str:
        return str(self.raw["status"])

    @property
    def delivery(self) -> dict[str, Any] | None:
        return self.raw.get("delivery")

    @property
    def error(self) -> dict[str, Any] | None:
        return self.raw.get("error")


@dataclass(frozen=True)
class Review(_Raw):
    """A review payload — ``GET/POST /v1/tasks/{id}/reviews``."""

    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def revision_id(self) -> str | None:
        return self.raw.get("revision_id")

    @property
    def agent_id(self) -> str | None:
        return self.raw.get("agent_id")

    @property
    def verdict(self) -> str:
        return str(self.raw["verdict"])

    @property
    def findings(self) -> list[dict[str, Any]]:
        return list(self.raw.get("findings") or [])

    @property
    def reviewer(self) -> dict[str, Any]:
        return dict(self.raw.get("reviewer") or {})

    @property
    def reviewed_head_sha(self) -> str | None:
        return self.raw.get("reviewed_head_sha")

    @property
    def independent(self) -> bool:
        return bool(self.raw.get("independent"))

    @property
    def stale(self) -> bool:
        return bool(self.raw.get("stale"))

    @property
    def comment_url(self) -> str | None:
        return self.raw.get("comment_url")


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
