"""VPS lifecycle reconciliation: one bounded worker, durable sweep diagnostics."""

from __future__ import annotations

import logging
import threading
import uuid

from control.reaper import _bounded_call, sweep_plane

log = logging.getLogger(__name__)


class SweepPlane:
    """Keep a sweep's detached records from racing live dispatch.

    Only short durable reads/writes hold the plane lock. Remote capture
    runs with a durable creating claim, so request paths can still accept
    queued intent, but cannot execute on a filesystem being snapshotted.
    """

    def __init__(self, plane):
        self.source = plane
        self.store = SweepStore(plane)
        self.checkpoints = SweepCheckpoints(plane)

    def __getattr__(self, name):
        return getattr(self.source, name)


class SweepStore:
    def __init__(self, plane):
        self.plane = plane
        self.baselines = {}

    def __getattr__(self, name):
        return getattr(self.plane.store, name)

    @staticmethod
    def identity(rec):
        return rec.sandbox_id, rec.status, rec.current_turn_id, rec.updated_at

    def list_all(self):
        with self.plane._lock:
            records = self.plane.store.list_all()
            self.baselines = {rec.id: self.identity(rec) for rec in records}
            return records

    def put(self, rec):
        with self.plane._lock:
            current = self.plane.store.get(rec.id)
            if current is None or current.sandbox_id != rec.sandbox_id:
                raise RuntimeError("lifecycle record changed")
            if current.sandbox_tags.get("lifecycle_claim"):
                if current.status != "creating":
                    raise RuntimeError("lifecycle claim superseded")
                # New accepted prompts are durable outside the snapshot. They
                # will be written into restored compute when dispatched.
                rec.messages = current.messages
                rec.sandbox_tags.pop("lifecycle_claim", None)
            elif self.identity(current) != self.baselines.get(rec.id):
                raise RuntimeError("lifecycle record changed")
            self.plane.store.put(rec)
            self.baselines[rec.id] = self.identity(rec)


class SweepCheckpoints:
    def __init__(self, plane):
        self.plane = plane

    def __getattr__(self, name):
        return getattr(self.plane.checkpoints, name)

    def suspend(self, rec, handle):
        with self.plane._lock:
            current = self.plane.store.get(rec.id)
            if (
                current is None
                or current.status != "idle"
                or current.current_turn_id
                or current.sandbox_id != handle.id
                or current.updated_at != rec.updated_at
            ):
                raise RuntimeError("lifecycle record changed")
            current.status = "creating"
            current.sandbox_tags["lifecycle_claim"] = uuid.uuid4().hex
            current.updated_at = self.plane.clock()
            self.plane.store.put(current)
        return self.plane.checkpoints.suspend(rec, handle)


class HostedLifecycle:
    def __init__(self, app, *, interval_s=30):
        self.app, self.interval_s = app, interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sbx-hosted-lifecycle", daemon=True)

    def sweep(self):
        state = self.app.state
        # Upgrade the image for new creates while existing handles keep their
        # immutable runtime_image. Provisioning is idempotent per build.
        with state.auth_store.database.transaction() as conn:
            owners = state.auth_store.database.execute(
                conn,
                "SELECT user_id FROM hosted_connections "
                "WHERE provider = 'modal' AND state = 'ready'",
            ).fetchall()
        for row in owners:
            try:
                _bounded_call(
                    lambda owner=row["user_id"]: state.modal_connections.provision(owner), 5
                )
            except Exception:
                log.warning("hosted runtime reconciliation failed")
        summary = sweep_plane(
            SweepPlane(state.plane),
            v1_state=state.v1_state,
            account_registry=state.hosted_accounts,
            log=log.info,
        )
        state.database_records.put(
            "hosted_lifecycle",
            "latest",
            {k: v for k, v in summary.items() if k not in {"actions", "now"}}
            | {"at": summary["now"].isoformat()},
        )
        return summary

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:
                log.warning("hosted lifecycle sweep failed")
            self._stop.wait(self.interval_s)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
