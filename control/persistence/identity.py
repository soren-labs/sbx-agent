"""Identity: users, password credentials, sessions, API keys, workspaces."""

from __future__ import annotations

from .base import Rows, _now


class UserRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("users", row)

    def get(self, user_id: str) -> dict | None:
        return self.one("SELECT * FROM users WHERE id=%s", (user_id,))

    def get_by_email(self, normalized_email: str) -> dict | None:
        return self.one("SELECT * FROM users WHERE email_normalized=%s", (normalized_email,))

    def update(self, user_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row("users", {"id": user_id}, changes, version_column=None)


class PasswordCredentialRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("password_credentials", row)

    def get_current(self, user_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM password_credentials WHERE user_id=%s ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )


class EmailVerificationRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("email_verifications", row)

    def get_active(self, user_id: str, token_hash: str) -> dict | None:
        return self.one(
            "SELECT * FROM email_verifications WHERE user_id=%s"
            " AND token_hash=%s AND consumed_at IS NULL AND expires_at > now()",
            (user_id, token_hash),
        )

    def consume(self, verification_id: str) -> None:
        self.one(
            "UPDATE email_verifications SET consumed_at=now() WHERE id=%s RETURNING id",
            (verification_id,),
        )


class PasswordResetRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("password_resets", row)

    def get_active(self, user_id: str, token_hash: str) -> dict | None:
        return self.one(
            "SELECT * FROM password_resets WHERE user_id=%s AND token_hash=%s"
            " AND consumed_at IS NULL AND expires_at > now()",
            (user_id, token_hash),
        )

    def consume(self, reset_id: str) -> None:
        self.one(
            "UPDATE password_resets SET consumed_at=now() WHERE id=%s RETURNING id",
            (reset_id,),
        )


class LoginSessionRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("login_sessions", row)

    def get(self, login_session_id: str) -> dict | None:
        return self.one("SELECT * FROM login_sessions WHERE id=%s", (login_session_id,))

    def get_by_token_hash(self, token_hash: str) -> dict | None:
        return self.one(
            "SELECT * FROM login_sessions WHERE token_hash=%s"
            " AND revoked_at IS NULL AND expires_at > now()",
            (token_hash,),
        )

    def revoke(self, login_session_id: str) -> None:
        self.one(
            "UPDATE login_sessions SET revoked_at=now() WHERE id=%s RETURNING id",
            (login_session_id,),
        )

    def revoke_all_for_user(self, user_id: str) -> None:
        self.conn.execute(
            "UPDATE login_sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL",
            (user_id,),
        )


class ApiKeyRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("api_keys", row)

    def get(self, api_key_id: str) -> dict | None:
        return self.one("SELECT * FROM api_keys WHERE id=%s", (api_key_id,))

    def get_by_hash(self, key_hash: str) -> dict | None:
        return self.one(
            "SELECT * FROM api_keys WHERE key_hash=%s AND revoked_at IS NULL",
            (key_hash,),
        )

    def revoke(self, api_key_id: str) -> None:
        self.one(
            "UPDATE api_keys SET revoked_at=now() WHERE id=%s RETURNING id",
            (api_key_id,),
        )


class WorkspaceRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("workspaces", row)

    def get(self, workspace_id: str) -> dict | None:
        return self.one("SELECT * FROM workspaces WHERE id=%s", (workspace_id,))

    def personal_for(self, owner_user_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM workspaces WHERE owner_user_id=%s AND name='personal'",
            (owner_user_id,),
        )

    def memberships_of(self, user_id: str) -> list[dict]:
        return self.all(
            "SELECT w.* FROM workspaces w JOIN workspace_memberships m"
            " ON m.workspace_id = w.id WHERE m.user_id=%s AND m.status='active'",
            (user_id,),
        )

    def member_role(self, workspace_id: str, user_id: str) -> str | None:
        row = self.one(
            "SELECT role FROM workspace_memberships WHERE workspace_id=%s"
            " AND user_id=%s AND status='active'",
            (workspace_id, user_id),
        )
        return row["role"] if row else None

    def add_member(self, row: dict) -> dict:
        return self.insert_row("workspace_memberships", row)
