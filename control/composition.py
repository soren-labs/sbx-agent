"""Composition data only; resource commands retain independent application owners."""

from types import SimpleNamespace

from control.application.changes import Changes
from control.application.connections import Connections
from control.application.delegation import Delegations
from control.application.delivery import Deliveries
from control.application.identity import Identity
from control.application.io import SessionIO
from control.application.lifecycle import Lifecycle
from control.application.projects import Projects
from control.application.query import Queries
from control.application.services import Services
from control.application.sessions import Sessions
from control.application.worktrees import Worktrees
from control.domain.errors import require
from control.domain.identity import Principal
from control.integrations.github import GitHubEffects
from control.jobs.claims import Claims
from control.jobs.handlers.changes import CaptureHandler
from control.jobs.handlers.child_inputs import ChildInputs
from control.jobs.handlers.connections import ValidationHandler
from control.jobs.handlers.delegation import DeadlineHandler, ResultHandler
from control.jobs.handlers.delivery import DeliveryHandler
from control.jobs.handlers.environment import EnvironmentResolver
from control.jobs.handlers.execution import ExecutionHandler
from control.jobs.handlers.identity import NoticeHandler
from control.jobs.handlers.io import IOHandler
from control.jobs.handlers.outbox import OutboxNotify
from control.jobs.handlers.snapshots import ReleaseHandler, SnapshotHandler
from control.jobs.worker import Worker


def assemble(uow, vault, objects, master, executor_factory, connectors):
    connections = Connections(uow, vault)
    claims = Claims(uow)

    def credentials(session, execution, lease, purpose):
        cid = session["zen_connection_id"]
        require(cid is not None, "credential_invalid")
        principal = Principal(session["creator_id"], (session["workspace_id"],))
        material, vid = connections.resolve(
            principal,
            cid,
            purpose,
            session_id=session["id"],
            lease_id=lease["id"],
            operation_id=execution["operation_id"],
        )
        return material, vid

    execution = ExecutionHandler(uow, claims, executor_factory, credentials, master)
    snapshots = SnapshotHandler(uow, claims, executor_factory, objects, master)
    execution.snapshot_reader = snapshots.read
    execution.environment_resolver = EnvironmentResolver(uow, connections, connectors["github"])
    sessions = Sessions(uow)
    execution.child_inputs = ChildInputs(uow, objects, claims)
    delegations = Delegations(uow, sessions)
    delivery = DeliveryHandler(
        uow, claims, connections, GitHubEffects(connectors["github"], objects)
    )
    io = SessionIO(uow, claims, executor_factory, master, objects)
    iohandler = IOHandler(uow, claims, io, objects)
    handlers = {
        "delegation.expire": DeadlineHandler(uow, claims, delegations),
        "delegation.expire_wait": DeadlineHandler(uow, claims, delegations),
        "identity.notice": NoticeHandler(uow, claims, vault),
        "worktree.perform": iohandler,
        "worktree.terminal_close": iohandler,
        "delivery.perform": delivery,
        "delivery.reconcile": delivery,
        "delivery.merge": delivery,
        "turn.dispatch": execution,
        "changeset.capture": CaptureHandler(uow, claims, executor_factory, objects, master),
        "delegation.publish_result": ResultHandler(uow, claims, sessions),
        "connection.validate": ValidationHandler(uow, claims, connections, connectors),
        "snapshot.capture": snapshots,
        "executor.release": ReleaseHandler(uow, claims, executor_factory, master),
    }
    return SimpleNamespace(
        uow=uow,
        identity=Identity(uow, vault, master),
        projects=Projects(uow),
        sessions=sessions,
        queries=Queries(uow),
        lifecycle=Lifecycle(uow, sessions),
        io=io,
        services=Services(uow, io),
        changes=Changes(uow, objects),
        deliveries=Deliveries(uow),
        delegations=delegations,
        connections=connections,
        worktrees=Worktrees(uow),
        objects=objects,
        vault=vault,
        claims=claims,
        handlers=handlers,
        worker=Worker(uow, handlers),
        outbox_worker=Worker(
            uow,
            {"outbox.notify": OutboxNotify(uow, Claims(uow, table="outbox_messages"))},
            holder="outbox",
            claims=Claims(uow, table="outbox_messages"),
        ),
        executor_factory=executor_factory,
        master=master,
    )
