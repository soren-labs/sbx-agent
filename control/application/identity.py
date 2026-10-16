"""Identity application: register/verify/login/logout/password/API keys (RFC 06).

Cookie sessions and API keys resolve the same Principal. Stored verifiers are
hashes; plaintext keys/tokens are returned exactly once.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from typing import Any, Protocol

from control.application import access
from control.domain.digests import canonical_json, digest_of
from control.domain.errors import DomainError
from control.domain.identity import API_KEY_SCOPES, Principal, check_password, normalize_email
from control.domain.ids import new_id
from control.security.passwords import (
    ALGORITHM,
    DUMMY_HASH,
    hash_password,
    new_token,
    token_hash,
    verify_password,
)
from control.security.vault import Sealed

MAX_FAILURES = 10
API_KEY_PREFIX = "sbx_key_"
# A retried create (same Idempotency-Key and body) gets the same key's plaintext back only
# within this window, from a vault-sealed copy that is erased afterwards. Later retries get
# `secret_unavailable` with the key's metadata so the client can revoke and reissue.
ISSUANCE_REPLAY_WINDOW = timedelta(minutes=10)


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


class Identity:
    def __init__(
        self,
        tx: Any,
        mailer: Mailer,
        *,
        public_url: str,
        session_ttl: timedelta = timedelta(days=14),
        token_ttl: timedelta = timedelta(hours=24),
        vault: Any = None,
        replay_window: timedelta = ISSUANCE_REPLAY_WINDOW,
    ) -> None:
        self.tx = tx
        self.mailer = mailer
        self.public_url = public_url.rstrip("/")
        self.session_ttl = session_ttl
        self.token_ttl = token_ttl
        # Seals the short-lived issuance replay copy; without one, retries never see plaintext.
        self.vault = vault
        self.replay_window = replay_window

    # -- registration / verification ------------------------------------------------
    def register(self, email: str, password: str) -> dict[str, Any]:
        email = normalize_email(email)
        check_password(password)
        hashed = hash_password(password)
        outgoing: list[tuple[str, str]] = []

        def fn(uow: Any) -> None:
            user = uow.find_one("users", {"email": email}, lock=True)
            if user is not None:
                if user["email_verified_at"] is None:
                    outgoing.append(self._verification(uow, user["id"], "verify_email"))
                return  # identical response; no account enumeration
            user_id, workspace_id = new_id("user"), new_id("workspace")
            uow.insert("users", {"id": user_id, "email": email})
            uow.insert(
                "password_credentials", {"user_id": user_id, "algorithm": ALGORITHM, "hash": hashed}
            )
            uow.insert(
                "workspaces", {"id": workspace_id, "owner_user_id": user_id, "name": "Personal"}
            )
            uow.insert(
                "workspace_memberships",
                {"workspace_id": workspace_id, "user_id": user_id, "role": "owner"},
            )
            uow.audit(
                actor=user_id,
                action="identity.register",
                target_kind="user",
                target_id=user_id,
                result="ok",
                workspace_id=workspace_id,
            )
            outgoing.append(self._verification(uow, user_id, "verify_email"))

        self.tx.run(fn)
        for token, purpose in outgoing:
            self._mail(email, token, purpose)
        return {"status": "verification_sent", "email": email}

    def _verification(self, uow: Any, user_id: str, purpose: str) -> tuple[str, str]:
        token = new_token()
        uow.update_where(
            "email_verifications",
            {"user_id": user_id, "purpose": purpose, "consumed_at": None},
            {"consumed_at": uow.now()},
        )
        uow.insert(
            "email_verifications",
            {
                "id": new_id("verification"),
                "user_id": user_id,
                "purpose": purpose,
                "token_hash": token_hash(token),
                "expires_at": uow.now() + self.token_ttl,
            },
        )
        return token, purpose

    def _mail(self, email: str, token: str, purpose: str) -> None:
        if purpose == "verify_email":
            self.mailer.send(
                email,
                "Verify your SBX email",
                f"Verify your email: {self.public_url}/verify-email?token={token}",
            )
        else:
            self.mailer.send(
                email,
                "Reset your SBX password",
                f"Reset your password: {self.public_url}/reset-password?token={token}",
            )

    def request_verification(self, email: str) -> dict[str, Any]:
        email = normalize_email(email)
        out: list[tuple[str, str]] = []

        def fn(uow: Any) -> None:
            user = uow.find_one("users", {"email": email}, lock=True)
            if user is not None and user["email_verified_at"] is None:
                out.append(self._verification(uow, user["id"], "verify_email"))

        self.tx.run(fn)
        for token, purpose in out:
            self._mail(email, token, purpose)
        return {"status": "verification_sent"}

    def _consume(self, uow: Any, token: str, purpose: str) -> dict[str, Any]:
        row = uow.find_one(
            "email_verifications",
            {"token_hash": token_hash(token or ""), "purpose": purpose},
            lock=True,
        )
        if row is None or row["consumed_at"] is not None or row["expires_at"] <= uow.now():
            raise DomainError(
                "validation_failed", "token is invalid or expired", details={"field": "token"}
            )
        uow.update("email_verifications", row["id"], {"consumed_at": uow.now()})
        return row

    def verify_email(self, token: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            row = self._consume(uow, token, "verify_email")
            user = uow.update(
                "users", row["user_id"], {"email_verified_at": uow.now(), "updated_at": uow.now()}
            )
            uow.audit(
                actor=user["id"],
                action="identity.verify_email",
                target_kind="user",
                target_id=user["id"],
                result="ok",
            )
            return {"status": "verified", "email": user["email"]}

        return self.tx.run(fn)

    # -- login sessions ------------------------------------------------------------------
    def login(self, email: str, password: str) -> dict[str, Any]:
        try:
            email = normalize_email(email)
        except DomainError:
            raise DomainError("unauthenticated", "invalid email or password") from None

        def precheck(uow: Any) -> dict[str, Any] | None:
            failures = uow.query_one("login.failures_recent", email=email)["n"]
            if failures >= MAX_FAILURES:
                raise DomainError(
                    "rate_limited", "too many failed sign-in attempts", retry_after=900
                )
            user = uow.find_one("users", {"email": email})
            cred = uow.find_one("password_credentials", {"user_id": user["id"]}) if user else None
            return {"user": user, "hash": cred["hash"] if cred else None}

        found = self.tx.read(precheck)
        ok = (
            verify_password(found["hash"] or DUMMY_HASH, password or "")
            and found["user"] is not None
        )
        session_token, csrf_token = new_token("sbx_sess_"), new_token()

        def commit(uow: Any) -> dict[str, Any]:
            uow.insert("login_attempts", {"email": email, "succeeded": ok})
            if not ok:
                return {}
            user = found["user"]
            if user["email_verified_at"] is None:
                return {"unverified": True}
            row = uow.insert(
                "login_sessions",
                {
                    "id": new_id("login_session"),
                    "user_id": user["id"],
                    "token_hash": token_hash(session_token),
                    "csrf_hash": token_hash(csrf_token),
                    "auth_epoch": user["auth_epoch"],
                    "expires_at": uow.now() + self.session_ttl,
                },
            )
            uow.audit(
                actor=user["id"],
                action="identity.login",
                target_kind="login_session",
                target_id=row["id"],
                result="ok",
            )
            return {"user_id": user["id"], "expires_at": row["expires_at"]}

        result = self.tx.run(commit)
        if not result:
            raise DomainError("unauthenticated", "invalid email or password")
        if result.get("unverified"):
            raise DomainError(
                "forbidden", "verify your email before signing in", action="verify_email"
            )
        return {
            "session_token": session_token,
            "csrf_token": csrf_token,
            "user_id": result["user_id"],
            "expires_at": result["expires_at"],
        }

    def logout(self, session_token: str) -> None:
        self.tx.run(
            lambda uow: uow.update_where(
                "login_sessions",
                {"token_hash": token_hash(session_token or ""), "revoked_at": None},
                {"revoked_at": uow.now()},
            )
        )

    def _principal(
        self,
        uow: Any,
        user: dict[str, Any],
        *,
        via: str,
        scopes: frozenset[str],
        credential_id: str,
    ) -> Principal:
        workspaces = tuple(w["id"] for w in uow.query("memberships.for_user", user_id=user["id"]))
        return Principal(
            user_id=user["id"],
            workspace_ids=workspaces,
            scopes=scopes,
            auth_epoch=user["auth_epoch"],
            via=via,
            credential_id=credential_id,
        )

    def resolve_session(self, session_token: str | None) -> tuple[Principal, str] | None:
        if not session_token:
            return None

        def fn(uow: Any) -> tuple[Principal, str] | None:
            row = uow.find_one("login_sessions", {"token_hash": token_hash(session_token)})
            if row is None or row["revoked_at"] is not None or row["expires_at"] <= uow.now():
                return None
            user = uow.get("users", row["user_id"])
            if user["auth_epoch"] != row["auth_epoch"]:
                return None
            return self._principal(
                uow, user, via="cookie", scopes=frozenset({"*"}), credential_id=row["id"]
            ), row["csrf_hash"]

        return self.tx.read(fn)

    def resolve_api_key(self, key: str | None) -> Principal | None:
        if not key or not key.startswith(API_KEY_PREFIX):
            return None

        def fn(uow: Any) -> Principal | None:
            row = uow.find_one("api_keys", {"key_hash": token_hash(key)})
            if (
                row is None
                or row["revoked_at"] is not None
                or (row["expires_at"] and row["expires_at"] <= uow.now())
            ):
                return None
            user = uow.get("users", row["user_id"])
            return self._principal(
                uow, user, via="api_key", scopes=frozenset(row["scopes"]), credential_id=row["id"]
            )

        return self.tx.read(fn)

    @staticmethod
    def csrf_ok(csrf_hash: str, presented: str | None) -> bool:
        import hmac

        return bool(presented) and hmac.compare_digest(csrf_hash, token_hash(presented or ""))

    # -- password changes --------------------------------------------------------------
    def change_password(self, principal: Principal, current: str, new: str) -> dict[str, Any]:
        check_password(new)
        stored = self.tx.read(
            lambda uow: uow.find_one("password_credentials", {"user_id": principal.user_id})["hash"]
        )
        if not verify_password(stored, current or ""):
            raise DomainError("unauthenticated", "current password is incorrect")
        hashed = hash_password(new)

        def fn(uow: Any) -> None:
            uow.update_where(
                "password_credentials",
                {"user_id": principal.user_id},
                {"hash": hashed, "updated_at": uow.now()},
            )
            user = uow.get("users", principal.user_id, lock=True)
            uow.update(
                "users", user["id"], {"auth_epoch": user["auth_epoch"] + 1, "updated_at": uow.now()}
            )
            uow.audit(
                actor=user["id"],
                action="identity.change_password",
                target_kind="user",
                target_id=user["id"],
                result="ok",
            )

        self.tx.run(fn)
        return {"status": "changed", "sessions_revoked": True}

    def request_password_reset(self, email: str) -> dict[str, Any]:
        email = normalize_email(email)
        out: list[tuple[str, str]] = []

        def fn(uow: Any) -> None:
            user = uow.find_one("users", {"email": email}, lock=True)
            if user is not None:
                out.append(self._verification(uow, user["id"], "password_reset"))

        self.tx.run(fn)
        for token, purpose in out:
            self._mail(email, token, purpose)
        return {"status": "reset_sent"}

    def reset_password(self, token: str, new: str) -> dict[str, Any]:
        check_password(new)
        hashed = hash_password(new)

        def fn(uow: Any) -> dict[str, Any]:
            row = self._consume(uow, token, "password_reset")
            user = uow.get("users", row["user_id"], lock=True)
            uow.update_where(
                "password_credentials",
                {"user_id": user["id"]},
                {"hash": hashed, "updated_at": uow.now()},
            )
            uow.update(
                "users",
                user["id"],
                {
                    "auth_epoch": user["auth_epoch"] + 1,
                    "email_verified_at": user["email_verified_at"] or uow.now(),
                },
            )
            return {"status": "reset"}

        return self.tx.run(fn)

    # -- API keys -----------------------------------------------------------------------
    def create_api_key(
        self, principal: Principal, body: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Mint one key per (user, Idempotency-Key); a retry returns that key, never a second.

        The response to the first attempt carries the plaintext. A retry with the same key and
        body within the replay window gets the same plaintext again (from a vault-sealed copy);
        after the window, or once the key is revoked, it gets ``secret_unavailable`` with the
        key's metadata. The same Idempotency-Key with another body is ``idempotency_conflict``.
        """
        name = str(body.get("name") or "").strip()[:80]
        if not name:
            raise DomainError("validation_failed", "name is required", details={"field": "name"})
        scopes = ["*"] if body.get("scopes") is None else body["scopes"]
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(not isinstance(s, str) or s not in API_KEY_SCOPES for s in scopes)
        ):
            raise DomainError(
                "validation_failed",
                f"scopes must be a non-empty subset of {sorted(API_KEY_SCOPES)}",
                details={"field": "scopes"},
            )
        if not principal.can("*"):
            raise DomainError("forbidden", "only full-scope principals can mint API keys")
        scopes = sorted(set(scopes))
        request_digest = digest_of({"name": name, "scopes": scopes})
        key = new_token(API_KEY_PREFIX)
        key_id = new_id("api_key")
        sealed = None
        if idempotency_key and self.vault is not None:
            sealed = self._seal_replay(principal.user_id, key_id, idempotency_key, key)

        def fn(uow: Any) -> tuple[dict[str, Any], bool]:
            # Serializes issuance per user, so a concurrent retry sees the committed key.
            uow.get("users", principal.user_id, lock=True)
            if idempotency_key:
                prior = uow.find_one(
                    "api_keys", {"user_id": principal.user_id, "issuance_key": idempotency_key}
                )
                if prior is not None:
                    if prior["issuance_digest"] != request_digest:
                        raise DomainError(
                            "idempotency_conflict",
                            "Idempotency-Key was reused with a different request body",
                            details={"command": "api_keys.create"},
                        )
                    return prior, False
            row = uow.insert(
                "api_keys",
                {
                    "id": key_id,
                    "user_id": principal.user_id,
                    "workspace_id": principal.default_workspace_id,
                    "name": name,
                    "key_hash": token_hash(key),
                    "display_prefix": key[:14],
                    "scopes": scopes,
                    "issuance_key": idempotency_key,
                    "issuance_digest": request_digest if idempotency_key else None,
                    "replay_sealed": sealed,
                    "replay_until": uow.now() + self.replay_window if sealed else None,
                },
            )
            uow.audit(
                actor=principal.user_id,
                action="api_key.create",
                target_kind="api_key",
                target_id=row["id"],
                result="ok",
                workspace_id=row["workspace_id"],
            )
            return row, True

        row, minted = self.tx.run(fn)
        if minted:
            return {**self._key_view(row), "key": key}
        return self._replay(principal, row)

    def _replay_aad(self, user_id: str, key_id: str, idempotency_key: str) -> bytes:
        return canonical_json(
            {
                "purpose": "api_key_issuance_replay",
                "user_id": user_id,
                "api_key_id": key_id,
                "issuance_key": idempotency_key,
            }
        )

    def _seal_replay(
        self, user_id: str, key_id: str, idempotency_key: str, key: str
    ) -> dict[str, Any]:
        s = self.vault.seal({"key": key}, self._replay_aad(user_id, key_id, idempotency_key))
        return {
            "key_id": s.key_id,
            "nonce": base64.b64encode(s.nonce).decode(),
            "ciphertext": base64.b64encode(s.ciphertext).decode(),
        }

    def _replay(self, principal: Principal, row: dict[str, Any]) -> dict[str, Any]:
        """The outcome of an earlier attempt with this Idempotency-Key."""
        view = self._key_view(row)
        live = self.tx.read(
            lambda uow: (
                row["replay_sealed"] is not None
                and row["replay_until"] is not None
                and row["replay_until"] > uow.now()
            )
        )
        if row["revoked_at"] is None and live and self.vault is not None:
            box = row["replay_sealed"]
            key = self.vault.open(
                Sealed(
                    box["key_id"],
                    base64.b64decode(box["nonce"]),
                    base64.b64decode(box["ciphertext"]),
                ),
                self._replay_aad(principal.user_id, row["id"], row["issuance_key"]),
            )["key"]
            self.tx.run(
                lambda uow: uow.audit(
                    actor=principal.user_id,
                    action="api_key.create_replayed",
                    target_kind="api_key",
                    target_id=row["id"],
                    result="ok",
                    workspace_id=row["workspace_id"],
                )
            )
            return {**view, "key": key, "replayed": True}
        raise DomainError(
            "secret_unavailable",
            "this key was already created by an earlier attempt and its secret can no longer "
            "be shown; revoke it and create a new key",
            details={
                "api_key": view,
                "reason": "revoked" if row["revoked_at"] is not None else "replay_expired",
            },
        )

    def purge_issuance_replays(self) -> int:
        """Erase sealed replay copies whose window has passed (run by the timer loop)."""
        return len(self.tx.run(lambda uow: uow.query("api_keys.purge_replays")))

    @staticmethod
    def _key_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "prefix": row["display_prefix"],
            "scopes": list(row["scopes"]),
            "created_at": _iso(row["created_at"]),
            "revoked_at": _iso(row["revoked_at"]),
        }

    def list_api_keys(self, principal: Principal) -> dict[str, Any]:
        rows = self.tx.read(
            lambda uow: uow.find(
                "api_keys", {"user_id": principal.user_id}, order="created_at DESC"
            )
        )
        return {"items": [self._key_view(r) for r in rows]}

    def revoke_api_key(self, principal: Principal, key_id: str) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            row = uow.find_one("api_keys", {"id": key_id, "user_id": principal.user_id}, lock=True)
            if row is None:
                raise DomainError("not_found", "api key not found")
            row = uow.update(
                "api_keys",
                key_id,
                # A revoked key's replay copy is useless and is erased with it.
                {"revoked_at": row["revoked_at"] or uow.now(), "replay_sealed": None},
            )
            uow.audit(
                actor=principal.user_id,
                action="api_key.revoke",
                target_kind="api_key",
                target_id=key_id,
                result="ok",
            )
            return self._key_view(row)

        return self.tx.run(fn)

    def me(self, principal: Principal) -> dict[str, Any]:
        def fn(uow: Any) -> dict[str, Any]:
            user = uow.get("users", principal.user_id)
            workspaces = uow.query("memberships.for_user", user_id=principal.user_id)
            return {
                "user": {
                    "id": user["id"],
                    "email": user["email"],
                    "email_verified": user["email_verified_at"] is not None,
                    "created_at": _iso(user["created_at"]),
                },
                "workspaces": [
                    {"id": w["id"], "name": w["name"], "kind": w["kind"]} for w in workspaces
                ],
                "auth": {"via": principal.via, "scopes": sorted(principal.scopes)},
            }

        return self.tx.read(fn)

    def workspace(self, principal: Principal, workspace_id: str) -> dict[str, Any]:
        access.require_workspace(principal, workspace_id)
        w = self.tx.read(lambda uow: uow.get("workspaces", workspace_id))
        return {
            "id": w["id"],
            "name": w["name"],
            "kind": w["kind"],
            "policy_version": w["policy_version"],
        }
