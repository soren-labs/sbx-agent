"""Owned connection views for the existing account scheduler; credentials stay brokered."""

from __future__ import annotations

from typing import Any

from control.codex_broker import CodexBroker
from control.ports import Account
from control.postgres_state import DatabaseRecords
from control.scheduler import AccountScheduler, session_running_source


class HostedAccounts:
    hosted = True

    def __init__(self, broker: CodexBroker, owner: str | None = None):
        self.broker, self.owner = broker, owner
        self.records = DatabaseRecords(broker.store.auth.database)
        self._running = lambda _: 0

    def scoped(self, owner):
        view = HostedAccounts(self.broker, owner)
        view._running = self._running
        return view

    def _record(self, account_id):
        auth = self.broker.store.auth
        with auth.database.transaction() as conn:
            sql = (
                "SELECT user_id, provider FROM hosted_connections "
                "WHERE provider IN ('codex', 'opencode') AND id = ?"
            )
            params = (account_id,)
            if self.owner:
                sql += " AND user_id = ?"
                params += (self.owner,)
            row = auth.database.execute(conn, sql, params).fetchone()
        return self.broker.store.get(row["user_id"], row["provider"]) if row else None

    def _account(self, record):
        health = self.records.get("connection_health", record.id, owner=record.user_id) or {}
        status = health.get("status", "active")
        if record.state in {"reauth_required", "invalid", "disabled"}:
            status = "disabled" if record.state == "disabled" else "invalid"
        return Account(
            record.id,
            record.provider,
            "OpenCode Zen" if record.provider == "opencode" else "My Codex connection",
            status=status,
            max_concurrent=3,
            models=tuple(
                record.metadata.get(
                    "models", [] if record.provider == "opencode" else ["gpt-5.6-luna"]
                )
            ),
            created_at=record.updated_at,
            last_used_at=health.get("last_used_at"),
            cooldown_until=health.get("cooldown_until"),
            last_error=health.get("last_error"),
        )

    def list(self, provider=None):
        if provider not in (None, "codex", "opencode"):
            return []
        auth = self.broker.store.auth
        with auth.database.transaction() as conn:
            sql = "SELECT id FROM hosted_connections WHERE provider IN ('codex', 'opencode')"
            params = ()
            if self.owner:
                sql += " AND user_id = ?"
                params = (self.owner,)
            if provider:
                sql += " AND provider = ?"
                params += (provider,)
            sql += " ORDER BY CASE provider WHEN 'opencode' THEN 0 ELSE 1 END"
            rows = auth.database.execute(conn, sql, params).fetchall()
        return [account for row in rows if (account := self.get(row["id"])) is not None]

    def get(self, account_id):
        record = self._record(account_id)
        return self._account(record) if record else None

    def get_credential_blob(self, account_id):
        record = self._record(account_id)
        if record is None or record.state in {"disabled", "invalid", "reauth_required"}:
            return None
        if record.provider == "opencode":
            from control.manual_connections import opencode_blob

            return opencode_blob(self.broker.store.credentials(record)["secret"])
        return self.broker.lease(record.user_id).blob()

    def put_credential_blob(self, account_id, blob):
        raise PermissionError("hosted refresh state is owned by the VPS credential broker")

    def put(self, account):
        raise PermissionError("connect through the hosted provider authorization")

    def mark_status(self, account_id, status, *, cooldown_until=None, last_error=None):
        record = self._record(account_id)
        if record is None:
            raise KeyError(account_id)
        health = self.records.get("connection_health", account_id, owner=record.user_id) or {}
        # Only catalogued scheduler codes belong in durable public health metadata.
        from control.scheduler import failure_status

        safe_error = last_error if last_error and failure_status(last_error) else None
        health.update(
            {"status": status, "cooldown_until": cooldown_until, "last_error": safe_error}
        )
        self.records.put_owned("connection_health", account_id, record.user_id, health)
        if record.provider == "opencode" and status == "invalid" and record.state == "connected":
            record.state = "invalid"
            record.metadata = {"error": "opencode_key_invalid_or_expired_replace_key"}
            self.broker.store.save(record)
        return self._account(record)

    def touch(self, account_id, used_at):
        record = self._record(account_id)
        if record is None:
            raise KeyError(account_id)
        health = self.records.get("connection_health", account_id, owner=record.user_id) or {}
        health["last_used_at"] = used_at
        self.records.put_owned("connection_health", account_id, record.user_id, health)

    def remove(self, account_id):
        record = self._record(account_id)
        if record is None:
            raise KeyError(account_id)
        if record.provider == "opencode":
            from control.manual_connections import ManualConnections

            ManualConnections(self.broker.store).disable(record.user_id, "opencode")
        else:
            self.broker.disable(record.user_id)

    def running_count(self, account_id):
        return self._running(account_id) if self.get(account_id) else 0

    def bind_running(self, running):
        self._running = running


class HostedScheduling:
    """Reuse atomic scheduler leases/cooldowns, with an independent five-slot user cap."""

    def __init__(self, accounts: HostedAccounts, sessions):
        import threading

        self.accounts, self.sessions = accounts, sessions
        self._items: dict[str, Any] = {}
        self._lock = threading.Lock()
        accounts.bind_running(self.running_count)

    def running_count(self, account_id):
        record = self.accounts._record(account_id)
        if record is None:
            return 0
        return self.for_user(record.user_id).running_count(account_id)

    def for_user(self, owner):
        with self._lock:
            if owner not in self._items:
                self._items[owner] = AccountScheduler(
                    self.accounts.scoped(owner),
                    max_global=5,
                    external_running=session_running_source(self.sessions),
                )
            return self._items[owner]


class HostedRuntimeEvidence:
    def __init__(self, connections, owner):
        self.connections, self.owner = connections, owner

    def get(self, provider):
        from control.runtime_state import ProviderRuntimeRecord

        record = self.connections.get(self.owner, "modal") if self.owner else None
        if provider not in {"codex", "opencode"} or record is None:
            return None
        return ProviderRuntimeRecord(
            provider,
            "ready" if record.state == "ready" else "degraded",
            record.metadata.get("image", ""),
            record.metadata.get("runtime_version"),
            "Your Modal runtime",
            record.updated_at,
        )


class HostedCapabilities:
    """Serve the proved Zen catalog directly, with no speculative CLI catalog."""

    def __init__(self, registry):
        self.registry = registry

    def get(self, account, *, ensure=True):
        from dataclasses import replace

        from control.capabilities import declared_snapshot

        snapshot = declared_snapshot(account)
        if account.provider == "opencode":
            return replace(
                snapshot,
                source="discovered",
                stale=False,
                models=tuple(m for m in snapshot.models if m.model in account.models),
                default_model=account.models[0] if account.models else None,
            )
        return snapshot

    def refresh(self, account, blob=None):
        return self.get(account)
