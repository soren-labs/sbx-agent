import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from control.domain.errors import require
from control.domain.events import canonical
from control.domain.identity import Principal, new_id
from control.security.identity import hashed, password_hash, password_matches, token


class Identity:
    def __init__(self, uow, vault=None, receipt_key=None):
        self.uow, self.vault, self.receipt_key = uow, vault, receipt_key
        self.email_enabled = False

    def fingerprint(self, body):
        require(self.receipt_key is not None, "unsupported_capability")
        return hmac.new(self.receipt_key, canonical(body).encode(), hashlib.sha256).hexdigest()

    def receipt(self, repo, kind, key, fingerprint):
        repo.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (kind + ":" + key,))
        row = repo.one(
            "SELECT * FROM auth_command_receipts WHERE command_kind=%s AND key=%s", (kind, key)
        )
        require(not row or row["fingerprint"] == fingerprint, "idempotency_conflict")
        return row["response"] if row else None

    def save_receipt(self, repo, kind, key, fingerprint, response):
        repo.execute(
            "INSERT INTO auth_command_receipts(command_kind,key,fingerprint,response) "
            "VALUES(%s,%s,%s,%s)",
            (kind, key, fingerprint, response),
        )

    def notice_in(self, repo, uid, wid, purpose):
        require(self.vault is not None and self.email_enabled, "email_transport_unavailable")
        proof, nid = token(), new_id("notice")
        epoch = repo.one("SELECT identity_version FROM users WHERE id=%s", (uid,))[
            "identity_version"
        ]
        repo.execute(
            "INSERT INTO email_verifications(id,user_id,token_hash,purpose,expires_at,"
            "auth_epoch) VALUES(%s,%s,%s,%s,now()+interval '1 hour',%s)",
            (new_id("verify"), uid, hashed(proof), purpose, epoch),
        )
        context = {"notice": nid, "user": uid, "purpose": purpose}
        repo.execute(
            "INSERT INTO identity_notices(id,workspace_id,user_id,purpose,envelope,exp"
            "ires_at) VALUES(%s,%s,%s,%s,%s,now()+interval '1 hour')",
            (nid, wid, uid, purpose, self.vault.encrypt({"verifier": proof}, context)),
        )
        repo.enqueue(wid, "identity.notice", uid, nid)

    def register(self, email, password, *, verified=False, key=None):
        email = email.strip().lower()
        require(
            "@" in email
            and len(email) <= 254
            and not any(c.isspace() or ord(c) < 32 for c in email),
            "credential_invalid",
        )
        key = hashed(email) + ":" + key if key else None
        encoded = password_hash(password)
        with self.uow.transaction() as repo:
            repo.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (email,))
            fingerprint = self.fingerprint({"email": email, "password": password}) if key else None
            if key:
                previous = self.receipt(repo, "register", key, fingerprint)
                if previous:
                    return previous
                require(self.email_enabled, "email_transport_unavailable")
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
            result = {"user_id": uid, "workspace_id": wid, "email_verified": verified}
            if key:
                self.notice_in(repo, uid, wid, "verify")
                self.save_receipt(repo, "register", key, fingerprint, result)
            return result

    def login(self, email, password, key=None):
        key = hashed(email.strip().lower()) + ":" + key if key else None
        fingerprint = (
            self.fingerprint({"email": email.strip().lower(), "password": password})
            if key
            else None
        )
        if key:
            with self.uow.transaction() as repo:
                previous = self.receipt(repo, "login", key, fingerprint)
            if previous:
                material = self.vault.decrypt(
                    previous["envelope"], {"auth": "login", "key": key, "fingerprint": fingerprint}
                )
                self.authenticate(material["cookie"])
                return material["cookie"], material["csrf"]
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
            if key:
                previous = self.receipt(repo, "login", key, fingerprint)
                if previous:
                    material = self.vault.decrypt(
                        previous["envelope"],
                        {"auth": "login", "key": key, "fingerprint": fingerprint},
                    )
                    return material["cookie"], material["csrf"]
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
            if key:
                envelope = self.vault.encrypt(
                    {"cookie": value, "csrf": csrf},
                    {"auth": "login", "key": key, "fingerprint": fingerprint},
                )
                self.save_receipt(repo, "login", key, fingerprint, {"envelope": envelope})
        return value, csrf

    def authenticate(self, cookie=None, api_key=None):
        with self.uow.transaction() as repo:
            if api_key:
                row = repo.one(
                    "SELECT u.*,k.scopes AS api_scopes,k.workspace_id AS key_workspace"
                    " FROM api_keys k JOIN users u ON u.id=k.user_id "
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
                tuple(
                    r["workspace_id"]
                    for r in scopes
                    if not api_key or r["workspace_id"] == row["key_workspace"]
                ),
                scopes=tuple(row["api_scopes"]) if api_key else ("owner",),
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
                "AND expires_at>now() AND purpose='verify' FOR UPDATE",
                (hashed(verifier),),
            )
            require(proof is not None, "credential_invalid")
            repo.execute("UPDATE users SET verified_at=now() WHERE id=%s", (proof["user_id"],))
            repo.execute("UPDATE email_verifications SET used_at=now() WHERE id=%s", (proof["id"],))
            return {"verified": True}

    def password_change(self, principal, current_password, password):
        encoded = password_hash(password)
        with self.uow.transaction() as repo:
            row = repo.one(
                "SELECT * FROM password_credentials WHERE user_id=%s FOR UPDATE",
                (principal.user_id,),
            )
            require(password_matches(current_password, row["hash"]), "credential_invalid")
            repo.execute(
                "UPDATE password_credentials SET hash=%s,version=version+1 WHERE user_id=%s",
                (encoded, principal.user_id),
            )
            repo.execute(
                "UPDATE users SET identity_version=identity_version+1 WHERE id=%s",
                (principal.user_id,),
            )
            repo.execute(
                "UPDATE login_sessions SET revoked_at=now() WHERE user_id=%s", (principal.user_id,)
            )
            repo.execute(
                "UPDATE api_keys SET revoked_at=now() WHERE user_id=%s", (principal.user_id,)
            )
        return {"changed": True, "sign_in_required": True}

    def api_keys(self, principal):
        with self.uow.transaction() as repo:
            return {
                "items": repo.all(
                    "SELECT id,workspace_id,scopes,revoked_at,created_at FROM api_keys"
                    " WHERE user_id=%s ORDER BY created_at,id",
                    (principal.user_id,),
                )
            }

    def issue_api_key(self, principal, wid, scopes, key):
        from control.application.deduplication import command

        require(
            wid in principal.workspace_ids and set(scopes) <= {"owner", "read"} and bool(scopes),
            "forbidden",
        )
        value = "sbx_" + token()
        issued = False
        with self.uow.transaction() as repo:

            def perform():
                nonlocal issued
                kid = new_id("key")
                repo.execute(
                    "INSERT INTO api_keys(id,user_id,workspace_id,key_hash,scopes) VAL"
                    "UES(%s,%s,%s,%s,%s)",
                    (kid, principal.user_id, wid, hashed(value), scopes),
                )
                issued = True
                return {"id": kid, "workspace_id": wid, "scopes": scopes}

            response = command(
                repo,
                principal,
                wid,
                "identity.key",
                key,
                {"workspace_id": wid, "scopes": scopes},
                perform,
            )
        return {**response, "key": value} if issued else response

    def revoke_api_key(self, principal, kid):
        with self.uow.transaction() as repo:
            row = repo.one(
                "SELECT id FROM api_keys WHERE id=%s AND user_id=%s", (kid, principal.user_id)
            )
            require(row is not None, "not_found")
            repo.execute("UPDATE api_keys SET revoked_at=now() WHERE id=%s", (kid,))
        return {"revoked": True}

    def request_reset(self, email, key):
        email = email.strip().lower()
        key = hashed(email) + ":" + key
        with self.uow.transaction() as repo:
            fingerprint = self.fingerprint({"email": email})
            previous = self.receipt(repo, "reset", key, fingerprint)
            if previous:
                return previous
            user = repo.one(
                "SELECT u.id,w.id AS workspace FROM users u JOIN workspaces w ON w.own"
                "er_id=u.id WHERE email=%s AND verified_at IS NOT NULL",
                (email,),
            )
            require(self.email_enabled, "email_transport_unavailable")
            if user:
                recent = repo.one(
                    "SELECT count(*) AS n FROM identity_notices WHERE user_id=%s AND p"
                    "urpose='reset' AND created_at>now()-interval '1 hour'",
                    (user["id"],),
                )["n"]
                if recent < 3:
                    self.notice_in(repo, user["id"], user["workspace"], "reset")
            result = {"accepted": True}
            self.save_receipt(repo, "reset", key, fingerprint, result)
            return result

    def reset_password(self, verifier, password):
        encoded = password_hash(password)
        with self.uow.transaction() as repo:
            proof = repo.one(
                "SELECT v.* FROM email_verifications v JOIN users u ON u.id=v.user_id "
                "WHERE token_hash=%s AND purpose='reset' AND used_at IS NULL AND expir"
                "es_at>now() AND v.auth_epoch=u.identity_version FOR UPDATE OF v,u",
                (hashed(verifier),),
            )
            require(proof is not None, "credential_invalid")
            repo.execute(
                "UPDATE password_credentials SET hash=%s,version=version+1 WHERE user_id=%s",
                (encoded, proof["user_id"]),
            )
            repo.execute("UPDATE email_verifications SET used_at=now() WHERE id=%s", (proof["id"],))
            repo.execute(
                "UPDATE users SET identity_version=identity_version+1 WHERE id=%s",
                (proof["user_id"],),
            )
            repo.execute(
                "UPDATE login_sessions SET revoked_at=now() WHERE user_id=%s", (proof["user_id"],)
            )
            repo.execute(
                "UPDATE api_keys SET revoked_at=now() WHERE user_id=%s", (proof["user_id"],)
            )
        return {"changed": True, "sign_in_required": True}
