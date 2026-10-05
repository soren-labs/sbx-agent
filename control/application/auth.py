"""Product auth: email/password users, login sessions, API keys (RFC 167 §06).

MVP flow: signup (verified email, argon2id hash) -> login (rate-limited,
secure cookie token) / scoped API key (plaintext returned once, sha256
stored). Both cookie and key authenticate resolve the same
Principal(user_id, workspace_ids, scopes, auth_epoch). Stored credential
material is hashes only — never decryptable secrets.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

import argon2

from control.domain.errors import DomainError
from control.domain.identity import Principal, normalize_email
from control.domain.ids import new_id
from control.persistence.unit_of_work import SqlUnitOfWork

_hasher = argon2.PasswordHasher(time_cost=2, memory_cost=10240, parallelism=1)

MIN_PASSWORD_LEN = 10
LOGIN_SESSION_TTL_S = 60 * 60 * 24 * 30  # 30 days
API_KEY_PREFIX = "sbx_k_"
EMAIL_TOKEN_TTL_S = 60 * 60 * 24  # 24h
RESET_TOKEN_TTL_S = 60 * 60  # 1h

_email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _hash_token(token: str) -> str:
    """Bearer material is stored as sha256 — plaintext leaves once."""
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def _const_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


class RateLimiter:
    """Bounded fixed-window limiter (login/verification flows)."""

    def __init__(self, limit: int, window_s: float):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            if len(hits) >= self.limit:
                raise DomainError("rate_limited", "too many attempts; retry later")
            hits.append(now)
            self._hits[key] = hits


@dataclass
class SignupResult:
    user_id: str
    workspace_id: str
    email_verification_token: str
    requires_verification: bool = True


@dataclass
class LoginResult:
    login_session_id: str
    token: str  # plaintext cookie token — emitted once
    principal: Principal


@dataclass
class ApiKeyResult:
    api_key_id: str
    plaintext: str  # emitted once at creation
    scopes: list[str] = field(default_factory=list)


class AuthService:
    """All identity writes go through here; cross-owner secrets stay 404."""

    def __init__(self, db, login_limiter: RateLimiter | None = None):
        self.db = db
        self.login_limiter = login_limiter or RateLimiter(limit=8, window_s=300)

    # -- signup / verification -------------------------------------------

    def signup(
        self,
        uow: SqlUnitOfWork,
        *,
        email: str,
        password: str,
        display_name: str | None = None,
    ) -> SignupResult:
        normalized = normalize_email(email)
        if not _email_re.match(normalized):
            raise DomainError("validation_failed", "invalid email address")
        if len(password) < MIN_PASSWORD_LEN:
            raise DomainError(
                "validation_failed",
                f"password must be at least {MIN_PASSWORD_LEN} characters",
            )
        if uow.users.get_by_email(normalized) is not None:
            raise DomainError("idempotency_conflict", "email already registered")

        user_id = new_id("user")
        uow.users.insert(
            {
                "id": user_id,
                "email_normalized": normalized,
                "display_name": display_name,
                "verified_at": None,
            }
        )
        uow.password_credentials.insert(
            {
                "user_id": user_id,
                "algorithm": "argon2id",
                "password_hash": _hasher.hash(password),
            }
        )
        ws_id = new_id("workspace")
        uow.workspaces.insert({"id": ws_id, "owner_user_id": user_id, "name": "personal"})
        uow.workspaces.add_member(
            {
                "workspace_id": ws_id,
                "user_id": user_id,
                "role": "owner",
                "status": "active",
            }
        )
        token = secrets.token_urlsafe(32)
        uow.email_verifications.insert(
            {
                "id": new_id("email_verification"),
                "user_id": user_id,
                "token_hash": _hash_token(token),
                "expires_at": _after(EMAIL_TOKEN_TTL_S),
            }
        )
        uow.commit()
        return SignupResult(
            user_id=user_id,
            workspace_id=ws_id,
            email_verification_token=token,
        )

    def verify_email(self, uow: SqlUnitOfWork, *, token: str) -> dict:
        row = uow.rows.one(
            "SELECT * FROM email_verifications WHERE token_hash=%s"
            " AND consumed_at IS NULL AND expires_at > now()",
            (_hash_token(token),),
        )
        if row is None:
            raise DomainError("validation_failed", "invalid or expired token")
        uow.email_verifications.consume(row["id"])
        uow.conn.execute("UPDATE users SET verified_at=now() WHERE id=%s", (row["user_id"],))
        uow.commit()
        return {"user_id": row["user_id"]}

    def request_password_reset(self, uow: SqlUnitOfWork, *, email: str) -> str | None:
        """Returns a one-time token for local delivery (dev: returned; prod:
        mailed). Always succeeds silently for unknown emails."""
        user = uow.users.get_by_email(normalize_email(email))
        if user is None:
            return None
        token = secrets.token_urlsafe(32)
        uow.password_resets.insert(
            {
                "id": new_id("password_reset"),
                "user_id": user["id"],
                "token_hash": _hash_token(token),
                "expires_at": _after(RESET_TOKEN_TTL_S),
            }
        )
        uow.commit()
        return token

    def reset_password(self, uow: SqlUnitOfWork, *, token: str, new_password: str) -> None:
        row = uow.rows.one(
            "SELECT * FROM password_resets WHERE token_hash=%s"
            " AND consumed_at IS NULL AND expires_at > now()",
            (_hash_token(token),),
        )
        if row is None:
            raise DomainError("validation_failed", "invalid or expired reset token")
        if len(new_password) < MIN_PASSWORD_LEN:
            raise DomainError(
                "validation_failed",
                f"password must be at least {MIN_PASSWORD_LEN} characters",
            )
        uow.password_resets.consume(row["id"])
        uow.conn.execute(
            "UPDATE password_credentials SET password_hash=%s, version=version+1 WHERE user_id=%s",
            (_hasher.hash(new_password), row["user_id"]),
        )
        # Bump auth_epoch: every prior login session / API key dies with the
        # credential that minted it.
        user = uow.users.get(row["user_id"])
        uow.conn.execute(
            "UPDATE users SET auth_epoch=%s WHERE id=%s",
            (user["auth_epoch"] + 1, row["user_id"]),
        )
        uow.login_sessions.revoke_all_for_user(row["user_id"])
        uow.commit()

    # -- login sessions ---------------------------------------------------

    def login(
        self,
        uow: SqlUnitOfWork,
        *,
        email: str,
        password: str,
        client_key: str | None = None,
    ) -> LoginResult:
        normalized = normalize_email(email)
        self.login_limiter.check(f"{normalized}|{client_key or '-'}")
        user = uow.users.get_by_email(normalized)
        cred = uow.password_credentials.get_current(user["id"]) if user else None
        if user is None or cred is None:
            raise DomainError("unauthenticated", "invalid email or password")
        try:
            _hasher.verify(cred["password_hash"], password)
        except argon2.exceptions.VerifyMismatchError as exc:
            raise DomainError("unauthenticated", "invalid email or password") from exc

        token = secrets.token_urlsafe(32)
        session_id = new_id("login_session")
        uow.login_sessions.insert(
            {
                "id": session_id,
                "user_id": user["id"],
                "token_hash": _hash_token(token),
                "auth_epoch": user["auth_epoch"],
                "expires_at": _after(LOGIN_SESSION_TTL_S),
            }
        )
        uow.commit()
        return LoginResult(
            login_session_id=session_id,
            token=token,
            principal=self._principal_for(uow, user),
        )

    def authenticate_cookie(self, uow: SqlUnitOfWork, *, token: str) -> Principal:
        row = uow.rows.one(
            "SELECT * FROM login_sessions WHERE token_hash=%s"
            " AND revoked_at IS NULL AND expires_at > now()",
            (_hash_token(token),),
        )
        if row is None:
            raise DomainError("unauthenticated", "session expired or revoked")
        user = uow.users.get(row["user_id"])
        if user is None or user["auth_epoch"] != row["auth_epoch"]:
            raise DomainError("unauthenticated", "session revoked (stale auth epoch)")
        return self._principal_for(uow, user)

    def logout(self, uow: SqlUnitOfWork, *, login_session_id: str) -> None:
        uow.login_sessions.revoke(login_session_id)
        uow.commit()

    # -- API keys ---------------------------------------------------------

    def create_api_key(
        self,
        uow: SqlUnitOfWork,
        *,
        user_id: str,
        label: str | None,
        scopes: list[str],
    ) -> ApiKeyResult:
        user = uow.users.get(user_id)
        if user is None:
            raise DomainError("not_found", "user not found")
        plaintext = API_KEY_PREFIX + secrets.token_urlsafe(24)
        key_id = new_id("api_key")
        uow.api_keys.insert(
            {
                "id": key_id,
                "user_id": user_id,
                "label": label,
                "key_hash": _hash_token(plaintext),
                "scopes": list(scopes),
                "auth_epoch": user["auth_epoch"],
            }
        )
        uow.commit()
        return ApiKeyResult(api_key_id=key_id, plaintext=plaintext, scopes=list(scopes))

    def authenticate_api_key(self, uow: SqlUnitOfWork, *, key: str) -> Principal:
        row = uow.api_keys.get_by_hash(_hash_token(key))
        if row is None or row["revoked_at"] is not None:
            raise DomainError("unauthenticated", "invalid api key")
        if row["expires_at"] is not None and row["expires_at"] <= _now():
            raise DomainError("unauthenticated", "api key expired")
        user = uow.users.get(row["user_id"])
        if user is None or user["auth_epoch"] != row["auth_epoch"]:
            raise DomainError("unauthenticated", "api key revoked (stale auth epoch)")
        principal = self._principal_for(uow, user)
        return Principal(
            user_id=principal.user_id,
            workspace_ids=principal.workspace_ids,
            scopes=frozenset(row["scopes"]),
            auth_epoch=user["auth_epoch"],
            label="api_key",
        )

    def revoke_api_key(self, uow: SqlUnitOfWork, *, user_id: str, api_key_id: str) -> None:
        row = uow.api_keys.get(api_key_id)
        if row is None or row["user_id"] != user_id:
            raise DomainError("not_found", "api key not found")
        uow.api_keys.revoke(api_key_id)
        uow.commit()

    def _principal_for(self, uow: SqlUnitOfWork, user: dict) -> Principal:
        ws_ids = frozenset(m["id"] for m in uow.workspaces.memberships_of(user["id"]))
        return Principal(
            user_id=user["id"],
            workspace_ids=ws_ids,
            scopes=frozenset({"*"}),
            auth_epoch=user["auth_epoch"],
        )


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _after(seconds: float):
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(seconds=seconds)
