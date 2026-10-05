"""Live Worktree files and terminals over the current lease (observations, never wake compute)."""

from __future__ import annotations

from typing import Any

from control.application import access
from control.application.ports import RuntimeConnector, RuntimeRefused, RuntimeUnavailable
from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.domain.ids import new_id


class LiveWorkspace:
    def __init__(self, tx: Any, connector: RuntimeConnector) -> None:
        self.tx = tx
        self.connector = connector

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

    def _call(self, fn: Any) -> Any:
        try:
            return fn()
        except RuntimeUnavailable as exc:
            raise DomainError("executor_unavailable", "runtime unreachable") from exc
        except RuntimeRefused as exc:
            code = (
                exc.code
                if exc.code in ("not_found", "version_conflict", "validation_failed")
                else "executor_unavailable"
            )
            raise DomainError(code, str(exc)) from exc

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
        _, lease = self._lease(principal, session_id)
        return self._call(lambda: self.connector.channel(lease).query("files.read", path=path))

    def write_file(
        self, principal: Principal, session_id: str, body: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        session, lease = self._lease(principal, session_id)
        if not isinstance(body.get("path"), str) or not isinstance(body.get("content"), str):
            raise DomainError("validation_failed", "path and content are required")
        if "expected_digest" not in body:
            raise DomainError(
                "validation_failed",
                "expected_digest (or 'absent') is required to save",
                details={"field": "expected_digest"},
            )
        payload = {
            "path": body["path"],
            "content": body["content"],
            "expected_digest": body["expected_digest"],
        }
        result = self._result(
            self._call(
                lambda: self.connector.channel(lease).op(
                    "files.write", f"{session_id}:write:{idempotency_key}", session_id, payload
                )
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

    def create_terminal(self, principal: Principal, session_id: str) -> dict[str, Any]:
        _, lease = self._lease(principal, session_id)
        return self._result(
            self._call(
                lambda: self.connector.channel(lease).op(
                    "terminal.create", new_id("operation"), session_id, {}
                )
            )
        )

    def terminal_input(
        self, principal: Principal, session_id: str, terminal_id: str, data: str
    ) -> dict[str, Any]:
        _, lease = self._lease(principal, session_id)
        response = self._call(
            lambda: self.connector.channel(lease).op(
                "terminal.input",
                new_id("operation"),
                session_id,
                {"terminal_id": terminal_id, "data": data},
            )
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

    def terminal_output(
        self, principal: Principal, session_id: str, terminal_id: str, after: int
    ) -> dict[str, Any]:
        _, lease = self._lease(principal, session_id)
        return self._call(
            lambda: self.connector.channel(lease).query(
                "terminal.read", terminal_id=terminal_id, after=after
            )
        )
