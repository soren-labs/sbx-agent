"""Harness tool calls enter the same commands with a Session-bound principal.

No provider credentials, delivery authority or generic executor access is exposed.
The transport adapter is separate from these authorization/application semantics.
"""

from control.application.access import owned
from control.domain.errors import require


class SessionTools:
    def __init__(self, resources, principal, session_id):
        self.resources, self.principal, self.session_id = resources, principal, session_id

    def invoke(self, name, arguments, operation_id):
        r, p = self.resources, self.principal
        with r.uow.transaction() as repo:
            owned(repo, "sessions", self.session_id, p)
            if name not in {"spawn", "read", "apply"}:
                delegation = owned(repo, "delegations", arguments["delegation_id"], p)
                require(
                    self.session_id
                    in {delegation["parent_session_id"], delegation["child_session_id"]},
                    "not_found",
                )
            if name in {"cancel", "message", "wait"}:
                require(delegation["parent_session_id"] == self.session_id, "forbidden")
        if name == "read":
            target = arguments.get("session_id", self.session_id)
            with r.uow.transaction() as repo:
                require(
                    target == self.session_id
                    or repo.one(
                        "SELECT id FROM delegations WHERE parent_session_id=%s AND chi"
                        "ld_session_id=%s",
                        (self.session_id, target),
                    ),
                    "forbidden",
                )
            return r.sessions.get(p, target)
        if name == "apply":
            target = arguments["session_id"]
            with r.uow.transaction() as repo:
                require(
                    target == self.session_id
                    or repo.one(
                        "SELECT id FROM delegations WHERE parent_session_id=%s AND chi"
                        "ld_session_id=%s",
                        (self.session_id, target),
                    ),
                    "forbidden",
                )
            return r.io.apply(p, arguments["changeset_id"], arguments, operation_id)
        if name == "spawn":
            return r.delegations.spawn(p, self.session_id, arguments, operation_id)
        if name == "message":
            return r.delegations.message(
                p, arguments["delegation_id"], arguments["content"], operation_id
            )
        if name == "wait":
            return r.delegations.wait(
                p, arguments["delegation_id"], operation_id, arguments.get("seconds", 600)
            )
        if name == "result":
            return r.delegations.get(p, arguments["delegation_id"])
        if name == "cancel":
            return r.delegations.cancel(p, arguments["delegation_id"], operation_id)
        if name == "publish_result":
            require(delegation["child_session_id"] == self.session_id, "forbidden")
            return r.delegations.publish(p, delegation["id"], arguments["turn_id"], operation_id)
        require(False, "unsupported_capability")
