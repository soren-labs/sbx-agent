"""Explicit operator configuration. No ambient provider credential fallback."""

import os
from pathlib import Path

from control.composition import assemble
from control.domain.identity import Principal
from control.executors.local import LocalExecutor
from control.executors.modal import ModalExecutor
from control.integrations.connectors.github import GitHubConnector
from control.integrations.connectors.modal import ModalConnector
from control.integrations.connectors.opencode_zen import ZenConnector
from control.persistence.database import Database
from control.security.vault import EnvelopeVault
from control.storage.local import LocalObjects


def build(dsn, state_dir, *, allow_local=False):
    state = Path(state_dir)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    # Master is never in PostgreSQL, events, response bodies, or runtime environments.
    key_file = state / "credential-master"
    try:
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as file:
            file.write(os.urandom(32))
            file.flush()
            os.fsync(file.fileno())
    master = key_file.read_bytes()
    db = Database(dsn)
    db.migrate()
    resources = None

    def executor(session, lease, runtime_token):
        if lease["backend"] == "local":
            from control.domain.errors import require

            require(allow_local, "unsupported_capability")
            return LocalExecutor(state / "executors" / session["workspace_id"], runtime_token)
        principal = Principal(session["creator_id"], (session["workspace_id"],))
        if lease.get("_read_only"):
            credential = resources.connections.read_material(principal, lease["connection_id"])
            return ModalExecutor(credential, runtime_token)
        credential, _ = resources.connections.resolve(
            principal,
            lease["connection_id"],
            "teardown" if lease["state"] in {"lost", "quiescing"} else "compute",
            session_id=session["id"],
            lease_id=lease["id"],
            operation_id=lease["allocation_operation_id"],
        )
        return ModalExecutor(credential, runtime_token)

    resources = assemble(
        db,
        EnvelopeVault({"1": master}),
        LocalObjects(state / "objects"),
        master,
        executor,
        {"github": GitHubConnector(), "modal": ModalConnector(), "opencode_zen": ZenConnector()},
    )
    return resources
