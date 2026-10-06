"""Job worker: claims due Jobs and runs registered handlers.

The worker owns no domain state; every decision is a DB-authoritative command
committed under a current claim (RFC 04, U06).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from control.domain.errors import DomainError
from control.jobs import claims
from control.jobs.model import Claim, Failed, Outcome, Retry
from control.security.redaction import safe_traceback

log = logging.getLogger("sbx.jobs")


class JobContext:
    def __init__(self, db: Any, claim: Claim, worker: Worker) -> None:
        self.db = db
        self.claim = claim
        self.worker = worker
        self.finished = False

    @property
    def input(self) -> dict[str, Any]:
        return self.claim.input

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Domain transaction that commits only while this claim is current."""
        with self.db.transaction() as uow:
            yield uow
            claims.assert_current(uow, self.claim)

    def commit(self, fn: Callable[[Any], Any], outcome: Outcome | None = None) -> Any:
        """Atomically apply ``fn`` and (optionally) finish the Job under the claim."""

        def run(uow: Any) -> Any:
            result = fn(uow)
            claims.assert_current(uow, self.claim)
            if outcome is not None:
                claims.finish(uow, self.claim, outcome)
            return result

        result = self.db.run(run)
        if outcome is not None:
            self.finished = True
        return result

    def renew(self) -> None:
        self.claim = claims.renew(self.db, self.claim, lease_seconds=self.worker.lease_seconds)


Handler = Callable[[JobContext], Outcome]


class Worker:
    def __init__(
        self,
        db: Any,
        handlers: dict[str, Handler],
        *,
        holder: str | None = None,
        lease_seconds: float = 60,
        kinds: list[str] | None = None,
    ) -> None:
        self.db = db
        self.handlers = handlers
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
        self.lease_seconds = lease_seconds
        self.kinds = kinds or sorted(handlers)

    def run_once(self) -> bool:
        claim = claims.claim_next(
            self.db, self.holder, kinds=self.kinds, lease_seconds=self.lease_seconds
        )
        if claim is None:
            return False
        ctx = JobContext(self.db, claim, self)
        handler = self.handlers[claim.kind]
        try:
            outcome = handler(ctx)
        except claims.StaleClaim:
            log.warning("stale claim for %s; successor owns the effect", claim.job_id)
            return True
        except DomainError as exc:
            outcome = (
                Retry(exc.code, exc.message, retry_after=exc.retry_after)
                if exc.retryable
                else Failed(exc.code, exc.message)
            )
        except Exception as exc:  # categorized, retried with backoff
            # Exception text can embed selected credentials: log only a redacted render.
            log.error("job %s (%s) failed\n%s", claim.job_id, claim.kind, safe_traceback(exc))
            outcome = Retry("internal_error", type(exc).__name__)
        if not ctx.finished and outcome is not None:
            try:
                self.db.run(lambda uow: claims.finish(uow, ctx.claim, outcome))
            except claims.StaleClaim:
                log.warning("stale completion rejected for %s", claim.job_id)
        return True

    def run_until_idle(self, *, max_jobs: int = 1000, settle: float = 0.0) -> int:
        """Deterministic drain for tests/local mode; ``settle`` waits for due continuations."""
        ran = 0
        deadline = time.monotonic() + settle
        while ran < max_jobs:
            if self.run_once():
                ran += 1
                continue
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        return ran

    def run_forever(self, stop: threading.Event, *, idle_sleep: float = 0.25) -> None:
        while not stop.is_set():
            try:
                if not self.run_once():
                    stop.wait(idle_sleep)
            except Exception as exc:
                log.error("worker loop error\n%s", safe_traceback(exc))
                stop.wait(1.0)
