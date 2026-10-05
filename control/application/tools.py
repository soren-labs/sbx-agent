"""Narrow SBX tool gateway over Session-scoped, attenuated, revocable grants (RFC 07).

The gateway calls the same application commands; it never touches the DB directly
for business state. Each mutation is deduped by the tool-call operation ID.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from control.domain.errors import DomainError
from control.domain.identity import Principal
from control.domain.ids import new_id
from control.security.passwords import new_token, token_hash

DEFAULT_ACTIONS = (
    "sbx.sessions.spawn",
    "sbx.sessions.send",
    "sbx.sessions.wait",
    "sbx.sessions.read",
    "sbx.delegations.result",
    "sbx.sessions.cancel",
    "sbx.changesets.apply",
)
ALL_ACTIONS = (*DEFAULT_ACTIONS, "sbx.deliveries.request")


class ToolGateway:
    def __init__(
        self, tx: Any, *, delegations: Any, queries: Any, changes: Any, deliveries: Any
    ) -> None:
        self.tx = tx
        self.delegations = delegations
        self.queries = queries
        self.changes = changes
        self.deliveries = deliveries

    def mint(
        self,
        uow: Any,
        session: dict[str, Any],
        *,
        execution_id: str | None = None,
        actions: tuple[str, ...] = DEFAULT_ACTIONS,
        ttl: timedelta = timedelta(hours=2),
        max_depth: int = 2,
        max_children: int = 5,
    ) -> str:
        unknown = set(actions) - set(ALL_ACTIONS)
        if unknown:
            raise DomainError("validation_failed", f"unknown tool actions {sorted(unknown)}")
        token = new_token("sbx_tool_")
        user = uow.get("users", session["created_by"])
        uow.insert(
            "tool_grants",
            {
                "id": new_id("grant"),
                "workspace_id": session["workspace_id"],
                "session_id": session["id"],
                "execution_id": execution_id,
                "principal_id": session["created_by"],
                "token_hash": token_hash(token),
                "actions": list(actions),
                "max_depth": max_depth,
                "max_children": max_children,
                "auth_epoch": user["auth_epoch"],
                "expires_at": uow.now() + ttl,
            },
        )
        return token

    def revoke_for_session(self, uow: Any, session_id: str) -> None:
        uow.update_where(
            "tool_grants", {"session_id": session_id, "revoked_at": None}, {"revoked_at": uow.now()}
        )

    def _grant(self, token: str) -> tuple[dict[str, Any], Principal]:
        def fn(uow: Any) -> tuple[dict[str, Any], Principal]:
            grant = uow.find_one("tool_grants", {"token_hash": token_hash(token or "")})
            if grant is None or grant["revoked_at"] is not None or grant["expires_at"] <= uow.now():
                raise DomainError("unauthenticated", "tool grant invalid, expired or revoked")
            user = uow.get("users", grant["principal_id"])
            session = uow.get("sessions", grant["session_id"])
            if user["auth_epoch"] != grant["auth_epoch"] or session["lifecycle"] == "closed":
                raise DomainError(
                    "unauthenticated", "tool grant revoked by auth epoch or Session closure"
                )
            principal = Principal(
                user_id=grant["principal_id"],
                workspace_ids=(grant["workspace_id"],),
                scopes=frozenset({"tools"}),
                via="tool_grant",
                credential_id=grant["id"],
            )
            return grant, principal

        return self.tx.read(fn)

    def _own_child(
        self, grant: dict[str, Any], principal: Principal, delegation_id: str
    ) -> dict[str, Any]:
        view = self.delegations.get(principal, delegation_id)
        if view["parent_session_id"] != grant["session_id"]:
            raise DomainError("not_found", "delegation not found")
        return view

    def call(
        self, token: str, tool: str, operation_id: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        grant, principal = self._grant(token)
        if tool not in grant["actions"]:
            raise DomainError("forbidden", f"grant does not allow {tool}")
        if not operation_id:
            raise DomainError("validation_failed", "operation_id is required for tool calls")
        key = f"tool:{grant['id']}:{operation_id}"
        sid = grant["session_id"]
        if tool == "sbx.sessions.spawn":
            return self.delegations.spawn(
                principal,
                sid,
                args,
                idempotency_key=key,
                limits={"max_depth": grant["max_depth"], "max_children": grant["max_children"]},
            )
        if tool == "sbx.sessions.send":
            self._own_child(grant, principal, args.get("delegation_id", ""))
            return self.delegations.send(
                principal,
                args["delegation_id"],
                {"content": args.get("content"), "routing": args.get("routing", "queue")},
                idempotency_key=key,
            )
        if tool == "sbx.sessions.wait":
            self._own_child(grant, principal, args.get("delegation_id", ""))
            return self.delegations.wait(
                principal,
                args["delegation_id"],
                {"wake": "message", "deadline_seconds": args.get("deadline_seconds")},
                idempotency_key=key,
            )
        if tool == "sbx.sessions.read":
            target = args.get("session_id") or sid
            if target != sid:
                children = {
                    d["child_session_id"] for d in self.delegations.list(principal, sid)["items"]
                }
                if target not in children:
                    raise DomainError("not_found", "session not found")
            return {
                "session": self.queries.session(principal, target)["session"],
                "messages": self.queries.messages(principal, target)["items"][-20:],
            }
        if tool == "sbx.delegations.result":
            self._own_child(grant, principal, args.get("delegation_id", ""))
            return self.delegations.result(principal, args["delegation_id"])
        if tool == "sbx.sessions.cancel":
            self._own_child(grant, principal, args.get("delegation_id", ""))
            return self.delegations.cancel(principal, args["delegation_id"])
        if tool == "sbx.changesets.apply":
            return self.changes.request_apply(
                principal,
                args["changeset_id"],
                {
                    "destination_session_id": sid,
                    "expected_generation": args.get("expected_generation"),
                },
                idempotency_key=key,
            )
        if tool == "sbx.deliveries.request":
            return self.deliveries.request(
                principal,
                args["changeset_id"],
                {"transport": args.get("transport")},
                idempotency_key=key,
            )
        raise DomainError("unsupported_capability", f"tool {tool} is not available")
