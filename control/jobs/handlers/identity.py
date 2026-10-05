"""Durable private verification/reset transport; plaintext never enters Job payloads."""

from control.domain.errors import require


class NoticeHandler:
    def __init__(self, uow, claims, vault, sender=None):
        self.uow, self.claims, self.vault, self.sender = uow, claims, vault, sender

    def __call__(self, claim):
        with self.uow.transaction() as repo:
            notice = repo.one(
                "SELECT * FROM identity_notices WHERE id=%s", (claim.row["effect_id"],)
            )
            self.claims.assert_current(repo, claim)
            if notice["sent_at"]:
                return
            require(
                repo.one(
                    "SELECT expires_at>now() AS valid FROM identity_notices WHERE id=%s",
                    (notice["id"],),
                )["valid"],
                "credential_invalid",
            )
            user = repo.one("SELECT email FROM users WHERE id=%s", (notice["user_id"],))
        require(self.sender is not None, "email_transport_unavailable")
        material = self.vault.decrypt(
            notice["envelope"],
            {"notice": notice["id"], "user": notice["user_id"], "purpose": notice["purpose"]},
        )
        # Sender accepts stable identity so transport retries may dedupe delivery.
        self.sender(user["email"], notice["purpose"], material["verifier"], notice["id"])
        with self.uow.transaction() as repo:
            self.claims.assert_current(repo, claim)
            repo.execute("UPDATE identity_notices SET sent_at=now() WHERE id=%s", (notice["id"],))
