"""One bounded claim protocol for every handler. No actor memory authority."""

import threading

from control.domain.errors import DomainError
from control.jobs.claims import Claims


class Worker:
    def __init__(self, uow, handlers, holder="worker", *, claims=None):
        self.claims = claims or Claims(uow)
        self.handlers, self.holder = handlers, holder

    def once(self):
        claim = self.claims.take(self.holder)
        if not claim:
            return False
        stopped = threading.Event()

        def heartbeat():
            while not stopped.wait(20):
                try:
                    self.claims.renew(claim)
                except DomainError:
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            handler = self.handlers.get(claim.row["kind"])
            if handler is None:
                raise DomainError("unsupported_capability")
            handler(claim)
            self.claims.finish(claim)
        except DomainError as error:
            if error.code == "version_conflict":
                try:
                    self.claims.finish(claim, "failed", error.code)
                except DomainError:
                    pass
            else:
                retry = (
                    error.code
                    in {
                        "waiting_capacity",
                        "executor_unavailable",
                        "rate_limited",
                        "outcome_unknown",
                        "delivery_unresolved",
                    }
                    and claim.row["attempts"] < claim.row["max_attempts"]
                )
                self.claims.finish(
                    claim,
                    "retry_wait" if retry else "failed",
                    error.code,
                    min(60, 2 ** min(claim.row["attempts"], 6)),
                )
        except Exception:
            # Unknown launch/effect MUST be inspected on reclaim, not replaced.
            try:
                self.claims.finish(claim, "retry_wait", "effect_unresolved", 5)
            except DomainError:
                pass
        finally:
            stopped.set()
        return True
