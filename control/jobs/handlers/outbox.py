"""At-least-once invalidation hint; committed events remain the only replay source."""

from control.domain.events import canonical


class OutboxNotify:
    def __init__(self, uow, claims):
        self.uow, self.claims = uow, claims

    def __call__(self, claim):
        with self.uow.transaction() as repo:
            self.claims.assert_current(repo, claim)
            event = repo.one(
                "SELECT session_id,seq FROM session_events WHERE id=%s", (claim.row["event_id"],)
            )
            # PostgreSQL delivers only after this transaction commits. Consumers
            # tolerate duplicates and always replay from their committed cursor.
            repo.one("SELECT pg_notify('sbx_events',%s)", (canonical(event),))
