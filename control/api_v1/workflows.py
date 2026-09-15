"""Workflow query + scoped cleanup service seam (SOR-84 C1 / SOR-91).

Routes stay thin: ``WorkflowService`` composes the durable
``WorkflowStore`` index with the shared ``SessionService`` (plane) and the
run ledger / run-state seam so a caller holding only an api key id +
``workflow_id`` can recover its agents, latest runs and artifact refs —
and close exactly those agents, nobody else's.

Everything here is served from persisted records — the workflow index, the
session store and the durable run ledger. No sandbox I/O (no
``turns/<n>.json`` / ``events.jsonl`` reads) happens on this path, which is
what keeps workflow lookups cheap enough to poll during recovery.

Endpoint wiring (``GET /v1/workflows/{id}``, cleanup route, SDK recovery)
belongs to the SOR-84 integration lane; this module is the seam it plugs
into.
"""

from __future__ import annotations

from typing import Any

from control.config import TERMINAL_STATUSES
from control.run_errors import run_error_for_run
from control.run_store import TERMINAL_RUN_STATUSES, UNKNOWN_RUN_STATUS, default_artifact_refs
from control.service import release_lease
from control.workflow_store import WorkflowStore, WorkflowTaskRecord

_MISSING = "missing"


def _session_dt(rec: Any, field: str) -> str:
    """Session record datetime → ISO string for the ``Run`` contract."""
    value = getattr(rec, field, None) if rec is not None else None
    return value.isoformat() if hasattr(value, "isoformat") else (value or "")


def _metadata_get(metadata: Any, key: str) -> Any:
    if isinstance(metadata, dict):
        return metadata.get(key)
    return getattr(metadata, key, None)


class WorkflowService:
    """Index-backed workflow lookup + scoped, idempotent cleanup.

    ``v1`` (the per-app ``V1State``) and ``run_states`` are optional: the
    durable run ledger on the plane is preferred when present; the
    cancelled-run overlay and scheduler-lease release degrade to no-ops
    when the v1 state seam is absent.
    """

    def __init__(
        self,
        store: WorkflowStore,
        plane: Any,
        *,
        v1: Any = None,
        run_states: Any = None,
    ) -> None:
        self._store = store
        self._plane = plane
        self._v1 = v1
        self._run_states = run_states

    # ------------------------------------------------------------ write

    def attach(self, *, owner: str, agent_id: str, metadata: Any) -> WorkflowTaskRecord:
        """Bind ``agent_id`` to a caller workflow task (idempotent upsert).

        ``metadata`` is the request's ``metadata`` object (or an equivalent
        mapping); the schema layer already enforces non-empty ids, the
        ``ValueError`` here is defense in depth for direct service users.
        """
        record = WorkflowTaskRecord(
            owner=owner,
            workflow_id=str(_metadata_get(metadata, "workflow_id") or ""),
            task_id=str(_metadata_get(metadata, "task_id") or ""),
            role=str(_metadata_get(metadata, "role") or ""),
            agent_id=agent_id,
            parent_task_id=_metadata_get(metadata, "parent_task_id") or None,
        )
        missing = [key for key in ("workflow_id", "task_id", "role") if not getattr(record, key)]
        if missing:
            raise ValueError(f"workflow metadata missing: {', '.join(missing)}")
        return self._store.attach(record)

    def for_agent(self, agent_id: str) -> WorkflowTaskRecord | None:
        """The workflow binding for one agent, or None when untagged."""
        return self._store.for_agent(agent_id)

    def agent_ids(self, owner: str, workflow_id: str) -> set[str]:
        """Ids bound to ``(owner, workflow_id)`` — the ``GET /v1/agents``
        ``workflow_id`` filter."""
        return {task.agent_id for task in self._store.list_workflow(owner, workflow_id)}

    # ------------------------------------------------------------ query

    def lookup(self, owner: str, workflow_id: str) -> dict[str, Any] | None:
        """Agents + latest runs + progress for ``(owner, workflow_id)``.

        Returns None when the workflow is unknown so the route layer can
        answer 404. Doubles as the recover-workflow read: agent ids, task
        bindings, latest run ids/statuses and artifact refs are everything
        a fresh client process needs to resume polling/streaming.
        """
        tasks = self._store.list_workflow(owner, workflow_id)
        if not tasks:
            return None
        agents = [self._agent_view(task) for task in tasks]
        latest_statuses = [a["latest_run"]["status"] for a in agents if a["latest_run"] is not None]
        by_status: dict[str, int] = {}
        for status in latest_statuses:
            by_status[status] = by_status.get(status, 0) + 1
        progress = {
            "tasks": len({t.task_id for t in tasks}),
            "agents": len(agents),
            "open_agents": sum(
                1 for a in agents if a["status"] not in TERMINAL_STATUSES | {_MISSING}
            ),
            "runs": sum(a["runs"] for a in agents),
            "latest_runs_by_status": by_status,
            "all_terminal": len(agents) > 0
            and len(latest_statuses) == len(agents)
            and all(s in TERMINAL_RUN_STATUSES for s in latest_statuses),
        }
        return {"workflow_id": workflow_id, "agents": agents, "progress": progress}

    def _agent_view(self, task: WorkflowTaskRecord) -> dict[str, Any]:
        try:
            rec = self._plane.get(task.agent_id)
        except Exception:
            rec = None
        records = self._run_records(task.agent_id)
        view: dict[str, Any] = {
            "agent_id": task.agent_id,
            "task_id": task.task_id,
            "role": task.role,
            "parent_task_id": task.parent_task_id,
            "attached_at": task.created_at,
            "status": rec.status if rec is not None else _MISSING,
            "provider": (rec.sandbox_tags or {}).get("provider") if rec else None,
            "account_id": (rec.sandbox_tags or {}).get("account_id") if rec else None,
            "model": rec.model if rec is not None else None,
            "agent_created_at": rec.created_at.isoformat() if rec is not None else None,
            "runs": len(records),
            "latest_run": None,
        }
        if records:
            view["latest_run"] = self._run_view(task.agent_id, records[-1], rec)
        elif rec is not None:
            view["latest_run"] = self._derived_latest_run(task.agent_id, rec)
            if view["latest_run"] is not None:
                view["runs"] = max(int(rec.turns), int(rec.current_turn_n or 0))
        return view

    def _run_records(self, agent_id: str) -> list[Any]:
        """Persisted run records, highest ``n`` last; cheapest source wins."""
        ledger = getattr(self._plane, "run_ledger", None)
        if ledger is not None:
            try:
                return sorted(ledger.list(agent_id), key=lambda r: r.n)
            except Exception:
                pass
        if self._run_states is not None:
            try:
                return sorted(self._run_states.list(agent_id), key=lambda r: r.n)
            except Exception:
                pass
        return []

    def _run_view(self, agent_id: str, record: Any, rec: Any = None) -> dict[str, Any]:
        """Persisted run record → light run summary (no sandbox I/O).

        Keeps the ``Run`` contract shape — ``agent_id`` plus non-null
        ``created_at``/``updated_at`` are required there, so missing record
        timestamps fall back to the session record. ``usage`` is omitted
        while unmeasured (never fabricated zeros).
        """
        n = int(getattr(record, "n", 0) or 0)
        status = str(getattr(record, "status", "UNKNOWN"))
        error = getattr(record, "error", None)
        if status not in TERMINAL_RUN_STATUSES and n in self._cancelled(agent_id):
            status = "CANCELLED"
        if (
            status not in TERMINAL_RUN_STATUSES
            and status != UNKNOWN_RUN_STATUS
            and rec is not None
            and rec.status in TERMINAL_STATUSES
        ):
            # The session died (reap/lose/timeout) with this run still open
            # in the ledger — derive the same honest terminal status the run
            # route reports instead of claiming RUNNING forever.
            if rec.status == "closed":
                status = "CANCELLED"
            elif rec.status == "timed_out":
                status = "EXPIRED"
            else:
                status = UNKNOWN_RUN_STATUS
            if error is None:
                derived = run_error_for_run(status, agent_status=str(rec.status))
                error = derived.public() if derived is not None else None
        refs = list(getattr(record, "artifact_refs", None) or default_artifact_refs(n))
        result_text = getattr(record, "result_text", None)
        view: dict[str, Any] = {
            "id": f"run-{n}",
            "agent_id": agent_id,
            "n": n,
            "status": status,
            "created_at": getattr(record, "created_at", None) or _session_dt(rec, "created_at"),
            "updated_at": getattr(record, "updated_at", None) or _session_dt(rec, "updated_at"),
            "started_at": getattr(record, "started_at", None),
            "finished_at": getattr(record, "finished_at", None),
            "result": {"text": result_text} if result_text else None,
            "error": error,
            "provider": getattr(record, "provider", None),
            "account_id": getattr(record, "account_id", None),
            "model": getattr(record, "model", None),
            "artifact_refs": refs,
        }
        usage = getattr(record, "usage", None)
        if usage is not None:
            view["usage"] = dict(usage)
        return view

    def _derived_latest_run(self, agent_id: str, rec: Any) -> dict[str, Any] | None:
        """Ledger-less fallback: latest run derived from the session record.

        Status is never inferred as success — a finished turn with no
        persisted record is explicit ``UNKNOWN`` (same rule as the routes).
        Timestamps come from the session record so the ``Run`` contract's
        required fields stay populated.
        """
        n = max(int(rec.turns), int(rec.current_turn_n or 0))
        if n < 1:
            return None
        if rec.current_turn_n == n and rec.status == "running":
            status = "RUNNING"
        elif rec.status == "closed" or n in self._cancelled(agent_id):
            status = "CANCELLED"
        elif rec.status == "timed_out":
            status = "EXPIRED"
        else:
            status = "UNKNOWN"
        tags = rec.sandbox_tags or {}
        return {
            "id": f"run-{n}",
            "agent_id": agent_id,
            "n": n,
            "status": status,
            "created_at": _session_dt(rec, "created_at"),
            "updated_at": _session_dt(rec, "updated_at"),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "provider": tags.get("provider"),
            "account_id": tags.get("account_id"),
            "model": rec.model,
            "artifact_refs": default_artifact_refs(n),
        }

    def _cancelled(self, agent_id: str) -> set[int]:
        cancelled = getattr(self._v1, "cancelled", None)
        if not callable(cancelled):
            return set()
        try:
            return cancelled(agent_id)
        except Exception:
            return set()

    # ----------------------------------------------------------- cleanup

    def cleanup(self, owner: str, workflow_id: str) -> dict[str, Any] | None:
        """Close every agent of ``(owner, workflow_id)`` — nothing else.

        Idempotent: a second call finds the same index entry, sees the
        agents already terminal, and closes nothing. Scope is enforced
        twice — the index lookup is keyed by ``owner``, and the session
        record's ``owner`` is re-checked before close so even a corrupt
        index entry can never make cleanup touch another principal's
        session.
        """
        tasks = self._store.list_workflow(owner, workflow_id)
        if not tasks:
            return None
        result: dict[str, Any] = {
            "workflow_id": workflow_id,
            "matched": len(tasks),
            "closed": [],
            "already_terminal": [],
            "missing": [],
            "skipped": [],
            "errors": {},
        }
        for task in tasks:
            agent_id = task.agent_id
            try:
                rec = self._plane.get(agent_id)
            except Exception:
                rec = None
            if rec is None:
                result["missing"].append(agent_id)
            elif rec.owner != owner:
                result["skipped"].append(agent_id)
                continue
            elif rec.status in TERMINAL_STATUSES:
                result["already_terminal"].append(agent_id)
            else:
                try:
                    self._plane.close(agent_id)
                except KeyError:
                    result["missing"].append(agent_id)
                except Exception as exc:
                    result["errors"][agent_id] = str(exc)[:200] or exc.__class__.__name__
                    continue
                else:
                    result["closed"].append(agent_id)
            release_lease(self._v1, agent_id)
        return result


__all__ = ["WorkflowService"]
