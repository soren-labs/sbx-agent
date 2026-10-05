from datetime import UTC, datetime, timedelta

from control.domain.errors import require
from control.domain.identity import Principal, new_id
from control.security.identity import hashed, password_hash, password_matches, token


class Identity:
    def __init__(self, uow):
        self.uow = uow

    def register(self, email, password, *, verified=False):
        email = email.strip().lower()
        require("@" in email and len(email) <= 254, "credential_invalid")
        encoded = password_hash(password)
        with self.uow.transaction() as repo:
            repo.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (email,))
            require(
                not repo.one("SELECT id FROM users WHERE email=%s", (email,)), "version_conflict"
            )
            uid, wid = new_id("usr"), new_id("wsp")
            repo.execute(
                "INSERT INTO users(id,email,verified_at) VALUES(%s,%s,%s)",
                (uid, email, datetime.now(UTC) if verified else None),
            )
            repo.execute(
                "INSERT INTO password_credentials(user_id,hash) VALUES(%s,%s)", (uid, encoded)
            )
            repo.execute(
                "INSERT INTO workspaces(id,owner_id,name) VALUES(%s,%s,%s)", (wid, uid, "Personal")
            )
            repo.execute(
                "INSERT INTO workspace_memberships(workspace_id,user_id) VALUES(%s,%s)", (wid, uid)
            )
            return {"user_id": uid, "workspace_id": wid, "email_verified": verified}

    def login(self, email, password):
        email = email.strip().lower()
        with self.uow.transaction() as repo:
            repo.execute(
                "INSERT INTO login_limits(email,window_start,attempts) VALUES(%s,now(),1) "
                "ON CONFLICT(email) DO UPDATE SET attempts=CASE WHEN login_limits.window_start < "
                "now()-interval '15 minutes' THEN 1 ELSE login_limits.attempts+1 END,"
                "window_start=CASE WHEN login_limits.window_start<now()-interval '15 minutes' "
                "THEN now() ELSE login_limits.window_start END",
                (email,),
            )
            limited = (
                repo.one("SELECT attempts FROM login_limits WHERE email=%s", (email,))["attempts"]
                > 10
            )
            user = repo.one(
                "SELECT u.*,p.hash FROM users u JOIN password_credentials p ON u.id=p.user_id "
                "WHERE email=%s",
                (email,),
            )
        require(not limited, "rate_limited")
        # Hash a dummy for unknown users too; never return account existence.
        valid = password_matches(password, user["hash"] if user else password_hash("REDACTED"))
        require(user is not None and valid, "credential_invalid")
        require(user["verified_at"] is not None, "email_verification_required")
        value, csrf = token(), token()
        with self.uow.transaction() as repo:
            repo.execute(
                "INSERT INTO login_sessions(id,user_id,token_hash,csrf_hash,auth_epoch,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    new_id("login"),
                    user["id"],
                    hashed(value),
                    hashed(csrf),
                    user["identity_version"],
                    datetime.now(UTC) + timedelta(days=7),
                ),
            )
            repo.execute("DELETE FROM login_limits WHERE email=%s", (email,))
        return value, csrf

    def authenticate(self, cookie=None, api_key=None):
        with self.uow.transaction() as repo:
            if api_key:
                row = repo.one(
                    "SELECT u.* FROM api_keys k JOIN users u ON u.id=k.user_id "
                    "WHERE k.key_hash=%s AND k.revoked_at IS NULL",
                    (hashed(api_key),),
                )
            else:
                row = repo.one(
                    "SELECT u.* FROM login_sessions l JOIN users u ON u.id=l.user_id "
                    "WHERE l.token_hash=%s AND l.revoked_at IS NULL AND l.expires_at>now() "
                    "AND l.auth_epoch=u.identity_version",
                    (hashed(cookie or ""),),
                )
            require(row is not None, "forbidden")
            scopes = repo.all(
                "SELECT workspace_id FROM workspace_memberships WHERE user_id=%s AND s"
                "tatus='active'",
                (row["id"],),
            )
            return Principal(
                row["id"],
                tuple(r["workspace_id"] for r in scopes),
                auth_epoch=row["identity_version"],
            )

    def verify_csrf(self, cookie, csrf):
        with self.uow.transaction() as repo:
            require(
                repo.one(
                    "SELECT id FROM login_sessions WHERE token_hash=%s AND csrf_hash=%s "
                    "AND revoked_at IS NULL AND expires_at>now()",
                    (hashed(cookie or ""), hashed(csrf or "")),
                )
                is not None,
                "forbidden",
            )

    def logout(self, cookie):
        with self.uow.transaction() as repo:
            repo.execute(
                "UPDATE login_sessions SET revoked_at=now() WHERE token_hash=%s",
                (hashed(cookie or ""),),
            )

    def issue_verification(self, user_id):
        # Caller sends the returned private verifier through an injected email transport.
        verifier = token()
        with self.uow.transaction() as repo:
            repo.execute(
                "INSERT INTO email_verifications(id,user_id,token_hash,expires_at) VAL"
                "UES(%s,%s,%s,%s)",
                (
                    new_id("verify"),
                    user_id,
                    hashed(verifier),
                    datetime.now(UTC) + timedelta(hours=1),
                ),
            )
        return verifier

    def verify_email(self, verifier):
        with self.uow.transaction() as repo:
            proof = repo.one(
                "SELECT * FROM email_verifications WHERE token_hash=%s AND used_at IS NULL "
                "AND expires_at>now() FOR UPDATE",
                (hashed(verifier),),
            )
            require(proof is not None, "credential_invalid")
            repo.execute("UPDATE users SET verified_at=now() WHERE id=%s", (proof["user_id"],))
            repo.execute("UPDATE email_verifications SET used_at=now() WHERE id=%s", (proof["id"],))
            return {"verified": True}
