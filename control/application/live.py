"""Live Worktree files and terminals over the current lease (observations, never wake compute)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from control.application import access
from control.application.artifact_secrets import runtime_visible
from control.application.ports import RuntimeConnector, RuntimeRefused, RuntimeUnavailable
from control.domain.errors import DomainError
from control.domain.identity import Principal

_PRECONDITION = re.compile(r"^(absent|sha256:[0-9a-f]{64})$")


def _iso_epoch(value: Any) -> str | None:
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value, UTC).isoformat()


class LiveWorkspace:
    def __init__(self, tx: Any, connector: RuntimeConnector, credentials: Any) -> None:
        self.tx = tx
        self.connector = connector
        self.credentials = credentials

    def _lease(
        self, principal: Principal, session_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        def fn(uow: Any) -> tuple[Any, Any]:
            session = access.owned(uow, principal, "sessions", session_id, what="session")
            return session, uow.find_one(
                "executor_leases", {"session_id": session_id, "state": "ready"}
            )

        session, lease = self.tx.read(fn)
        if lease is None:
            raise DomainError(
                "executor_unavailable",
                "no live executor for this Session; activate it explicitly",
                action="activate_executor",
            )
        return session, lease

    @staticmethod
    def _refused(exc: RuntimeRefused) -> DomainError:
        code = (
            exc.code
            if exc.code in ("not_found", "version_conflict", "validation_failed", "forbidden")
            else "executor_unavailable"
        )
        return DomainError(code, str(exc))

    def _call(self, fn: Any) -> Any:
        try:
            return fn()
        except RuntimeUnavailable as exc:
            raise DomainError("executor_unavailable", "runtime unreachable") from exc
        except RuntimeRefused as exc:
            raise self._refused(exc) from exc

    def _keyed_op(
        self, lease: dict[str, Any], kind: str, operation_id: str, session_id: str, payload: Any
    ) -> dict[str, Any]:
        """A user-driven runtime operation whose ID derives from the client's Idempotency-Key.

        The runtime journal answers a retried ID from its record, so a retry after a lost
        response never writes to the file or PTY twice. Transport failures leave the outcome
        unknown (the runtime may have applied it), which only the same key can settle.
        """
        try:
            response = self.connector.channel(lease).op(kind, operation_id, session_id, payload)
        except RuntimeUnavailable as exc:
            raise DomainError(
                "outcome_unknown",
                "the runtime did not confirm this operation; retry with the same"
                " Idempotency-Key to learn its outcome without repeating it",
                retryable=True,
                details={"operation": kind},
            ) from exc
        except RuntimeRefused as exc:
            if exc.code == "operation_conflict":
                raise DomainError(
                    "idempotency_conflict",
                    "Idempotency-Key was reused with a different request body",
                    details={"command": kind},
                ) from exc
            raise self._refused(exc) from exc
        if response.get("replayed") and response.get("status") not in ("succeeded", "failed"):
            raise DomainError(
                "outcome_unknown",
                "the runtime never confirmed this operation and cannot replay its result",
                details={"operation": kind, "status": response.get("status")},
            )
        return response

    @staticmethod
    def _key(idempotency_key: str) -> str:
        if not idempotency_key:
            raise DomainError(
                "validation_failed",
                "Idempotency-Key header is required for mutations",
                details={"header": "Idempotency-Key"},
            )
        return idempotency_key

    @staticmethod
    def _result(response: dict[str, Any]) -> dict[str, Any]:
        if response.get("status") == "failed":
            error = (response.get("result") or {}).get("error") or {}
            code = error.get("code", "executor_unavailable")
            raise DomainError(
                code
                if code in ("version_conflict", "not_found", "validation_failed")
                else "executor_unavailable",
                error.get("message", code),
            )
        return response.get("result") or {}

    def list_files(self, principal: Principal, session_id: str, path: str = "") -> dict[str, Any]:
        _, lease = self._lease(principal, session_id)
        return self._call(lambda: self.connector.channel(lease).query("files.list", path=path))

    def read_file(self, principal: Principal, session_id: str, path: str) -> dict[str, Any]:
        session, lease = self._lease(principal, session_id)
        return self._call(
            lambda: self.connector.channel(lease).query(
                "files.read", path=path, secrets=runtime_visible(self.credentials, session)
            )
        )

    def write_file(
        self, principal: Principal, session_id: str, body: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        session, lease = self._lease(principal, session_id)
        if not isinstance(body.get("path"), str) or not isinstance(body.get("content"), str):
            raise DomainError("validation_failed", "path and content are required")
        expected = body.get("expected_digest")
        # A save is always conditional: a null precondition would turn an echoed partial
        # view (whose read carries ``digest: null``) into a blind overwrite.
        if not isinstance(expected, str) or not _PRECONDITION.match(expected):
            raise DomainError(
                "validation_failed",
                "expected_digest must be 'absent' or the digest of a complete read",
                details={"field": "expected_digest"},
            )
        payload = {"path": body["path"], "content": body["content"], "expected_digest": expected}
        result = self._result(
            self._keyed_op(
                lease,
                "files.write",
                f"{session_id}:write:{self._key(idempotency_key)}",
                session_id,
                payload,
            )
        )

        def commit(uow: Any) -> None:
            row = uow.get("sessions", session_id, lock=True)
            worktree = uow.find_one("worktrees", {"session_id": session_id}, lock=True)
            uow.update(
                "worktrees",
                worktree["id"],
                {
                    "generation": max(worktree["generation"], int(result.get("generation") or 0)),
                    "updated_at": uow.now(),
                },
            )
            uow.append_event(
                row,
                "worktree.changed",
                {
                    "path": body["path"],
                    "generation": result.get("generation"),
                    "digest": result.get("digest"),
                    "source": "user_file_write",
                },
                actor=principal.user_id,
            )

        self.tx.run(commit)
        return result

    def list_terminals(self, principal: Principal, session_id: str) -> dict[str, Any]:
        _, lease = self._lease(principal, session_id)
        try:
            listed = self.connector.channel(lease).query("terminal.list")
        except RuntimeRefused as exc:
            if exc.code != "validation_failed":
                raise self._refused(exc) from exc
            raise DomainError(
                "unsupported_capability",
                "this runtime predates terminal listing (protocol 1.1)",
                details={"capability": "terminal.list"},
            ) from exc
        except RuntimeUnavailable as exc:
            raise DomainError("executor_unavailable", "runtime unreachable") from exc
        items = sorted(listed.get("items") or [], key=lambda t: t.get("created_at") or 0)
        return {
            "items": [
                {
                    "terminal_id": t["terminal_id"],
                    "created_at": _iso_epoch(t.get("created_at")),
                    "closed": bool(t.get("closed")),
                    "offset": int(t.get("offset") or 0),
                }
                for t in items
            ]
        }

    def create_terminal(
        self, principal: Principal, session_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        _, lease = self._lease(principal, session_id)
        op_id = f"{session_id}:terminal.create:{principal.user_id}:{self._key(idempotency_key)}"
        return self._result(self._keyed_op(lease, "terminal.create", op_id, session_id, {}))

    def terminal_input(
        self,
        principal: Principal,
        session_id: str,
        terminal_id: str,
        data: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _, lease = self._lease(principal, session_id)
        # The terminal is part of the request body, so the same key sent to another
        # terminal is a conflict rather than a second command.
        op_id = f"{session_id}:terminal.input:{principal.user_id}:{self._key(idempotency_key)}"
        response = self._keyed_op(
            lease,
            "terminal.input",
            op_id,
            session_id,
            {"terminal_id": terminal_id, "data": data},
        )
        if (
            response.get("status") == "failed"
            and ((response.get("result") or {}).get("error") or {}).get("code") == "busy"
        ):
            raise DomainError(
                "version_conflict",
                "terminal input is read-only while a Turn or barrier is active",
                retryable=True,
            )
        return self._result(response)

    def close_terminal(
        self, principal: Principal, session_id: str, terminal_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        _, lease = self._lease(principal, session_id)
        op_id = f"{session_id}:terminal.close:{principal.user_id}:{self._key(idempotency_key)}"
        response = self._keyed_op(
            lease, "terminal.close", op_id, session_id, {"terminal_id": terminal_id}
        )
        if (
            response.get("status") == "failed"
            and ((response.get("result") or {}).get("error") or {}).get("code") == "busy"
        ):
            raise DomainError(
                "version_conflict", "the terminal process has not stopped yet", retryable=True
            )
        return self._result(response)

    def terminal_output(
        self, principal: Principal, session_id: str, terminal_id: str, after: int
    ) -> dict[str, Any]:
        _, lease = self._lease(principal, session_id)
        return self._call(
            lambda: self.connector.channel(lease).query(
                "terminal.read", terminal_id=terminal_id, after=after
            )
        )
