"""Cloud-free execution stack: PostgreSQL + Local executor + real sbx-runtime daemon +
fake official OpenCode CLI, driven by the real Job worker."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from control.application.execution import ExecutionService, ExecutionSettings
from control.application.projections import Queries
from control.application.resolution import ExplicitResolver
from control.application.sessions import Sessions
from control.catalog import load_catalog
from control.domain.errors import DomainError
from control.executors.local import LocalExecutor
from control.jobs.handlers.execution import handlers as execution_handlers
from control.jobs.worker import Worker
from control.runtime_client.client import HttpRuntimeConnector
from control.storage.local import LocalBlobStore

from tests.support.runtime import FAKE_OPENCODE


class StaticBroker:
    """Phase-2 stand-in for the vault broker: fixed inference bundle, no ambient env."""

    def __init__(self, api_key: str = "zen-static-test-key-123456") -> None:
        self.api_key = api_key
        self.health: list[str] = []

    def check_inference(self, uow: Any, session: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise DomainError("connection_required", "no inference connection")
        return {"credential_version_id": None}

    def inference(self, session: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return {"opencode_zen": {"api_key": self.api_key}}, {}

    def compute(self, session: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def source(self, session: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def report_health(
        self, uow: Any, connection_id: Any, credential_version_id: Any, health: str
    ) -> None:
        self.health.append(health)


class Stack:
    def __init__(self, db: Any, tmp: Path, broker: Any = None, resolver: Any = None) -> None:
        self.db = db
        self.catalog = load_catalog()
        self.broker = broker or StaticBroker()
        self.executor = LocalExecutor(tmp / "executor", extra_env={"OPENCODE_BIN": FAKE_OPENCODE})
        self.connector = HttpRuntimeConnector(secrets.token_bytes(32), timeout=30)
        self.blobs = LocalBlobStore(tmp / "blobs")
        self.sessions = Sessions(db, resolver or ExplicitResolver(self.catalog), self.catalog)
        self.queries = Queries(db)
        self.execution = ExecutionService(
            db,
            executors={"local": self.executor},
            connector=self.connector,
            credentials=self.broker,
            blobs=self.blobs,
            catalog=self.catalog,
            settings=ExecutionSettings(
                poll_busy=0.05, poll_idle=0.1, unreachable_threshold=3, turn_deadline_seconds=60
            ),
        )
        self.handlers: dict[str, Any] = dict(execution_handlers(self.execution))
        self.worker = Worker(db, self.handlers, lease_seconds=60)

    def rebuild_worker(self) -> None:
        self.worker = Worker(self.db, self.handlers, lease_seconds=60)

    def drive(self, until: Callable[[], bool], timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if until():
                return
            if not self.worker.run_once():
                time.sleep(0.03)
        raise AssertionError(
            "condition not reached; jobs="
            + str(
                self.db.read(
                    lambda u: [
                        (j["kind"], j["state"], j["last_error"])
                        for j in u.find("jobs", {}, order="created_at")
                    ]
                )
            )
        )

    def turn(self, turn_id: str) -> dict[str, Any]:
        return self.db.read(lambda u: u.get("turns", turn_id))

    def turn_done(self, turn_id: str) -> Callable[[], bool]:
        return lambda: (
            self.turn(turn_id)["state"] in ("succeeded", "failed", "cancelled", "interrupted")
        )

    def events(self, session_id: str) -> list[dict[str, Any]]:
        return self.db.read(
            lambda u: u.find("session_events", {"session_id": session_id}, order="seq")
        )

    def output_text(self, turn_id: str) -> str:
        def fn(u: Any) -> str:
            turn = u.get("turns", turn_id)
            parts = u.find(
                "message_parts",
                {"message_id": turn["output_message_id"], "kind": "text"},
                order="ordinal",
            )
            return "\n".join(p["content"] for p in parts)

        return self.db.read(fn)

    def shutdown(self) -> None:
        for lease in self.db.read(lambda u: u.find("executor_leases", {})):
            if lease["handle"]:
                self.executor.terminate(lease["handle"], "cleanup", None)
