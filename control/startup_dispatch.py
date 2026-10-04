"""Durable hosted startup claims and fencing for a single control-plane worker.

The VPS runs one API worker. A new worker settles an old worker's unbound
accepted intents before serving traffic; owners can retry the same session.
All binding and failure writes compare the durable claim, including workers
that were delayed until after a restart, cancellation, or retry.
"""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from control.api_v1.errors import V1ApiError


def now():
    return datetime.now(UTC).isoformat()


def claim(record):
    for transition in reversed(record.transitions):
        if transition.get("reason") in {"awaiting_dispatch", "retry_dispatch"}:
            return transition.get("detail", {}).get("startup_claim")
    return None


class StartupDispatch:
    def __init__(self, store):
        self.store = store
        self.worker = uuid4().hex

    def prepare(self, record):
        record.transitions[-1]["detail"] = {
            "startup_claim": {"worker": self.worker, "attempt": uuid4().hex}
        }

    def retry(self, expected, record):
        self.prepare(record)
        if not self.store.compare_put(expected, record):
            raise V1ApiError(409, "task_not_retryable", "Startup was already retried or cancelled")

    def reconcile(self):
        settled = 0
        for record in self.store.list():
            if not record.id.startswith("sess_") or record.status != "queued" or record.agent_id:
                continue
            if not any(
                t.get("reason") in {"awaiting_dispatch", "retry_dispatch"}
                for t in record.transitions
            ):
                continue
            previous = claim(record)
            if previous and previous.get("worker") == self.worker:
                continue
            failed = deepcopy(record)
            failed.status = "error"
            failed.updated_at = now()
            failed.transitions.append(
                {
                    "status": "error",
                    "reason": "dispatch_failed",
                    "at": failed.updated_at,
                    "detail": {
                        "code": "internal",
                        "retryable": True,
                        "message": (
                            "Control plane restarted before session startup completed; "
                            "retry this session."
                        ),
                    },
                }
            )
            settled += bool(self.store.compare_put(record, failed))
        return settled

    def guarded(self, record, plane, v1):
        return StartupStore(self.store, record, plane, v1)


class StartupStore:
    def __init__(self, store, record, plane, v1):
        self.store, self.session_id, self.owner = store, record.id, record.owner
        self.expected_claim = claim(record)
        self.plane, self.v1 = plane, v1

    def __getattr__(self, name):
        return getattr(self.store, name)

    def require_current(self):
        record = self.store.get(self.session_id)
        if (
            record is None
            or record.owner != self.owner
            or record.status != "queued"
            or record.agent_id is not None
            or claim(record) != self.expected_claim
        ):
            raise V1ApiError(409, "task_not_retryable", "Startup dispatch was superseded")
        return record

    def put(self, record):
        try:
            current = self.require_current()
            if record.id != self.session_id or record.owner != self.owner:
                raise V1ApiError(409, "task_not_retryable", "Startup identity changed")
            if record.agent_id:
                record.created_at = current.created_at
                record.transitions = [*current.transitions, *record.transitions]
            if not self.store.compare_put(current, record):
                raise V1ApiError(409, "task_not_retryable", "Startup dispatch was superseded")
        except Exception:
            # Only the newly allocated agent from this failed binding is closed.
            # The durable winner (including a newer retry) is never overwritten.
            if record.agent_id and record.owner == self.owner:
                winner = self.store.get(self.session_id)
                agent = self.plane.store.get(record.agent_id)
                if (
                    agent
                    and agent.owner == self.owner
                    and (not winner or winner.agent_id != record.agent_id)
                ):
                    self.plane.close(record.agent_id)
                    self.v1.reconcile_leases(self.plane.store, interval_s=0)
            raise
