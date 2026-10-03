"""Fresh OS process for durable hosted acceptance, with explicit mock adapters."""

import base64
import os
import socket
import sys
from pathlib import Path

import uvicorn

# Import the legacy module-level default before constructing this injected
# hosted test application, so SQLite remains a test-only storage seam.
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore
from control.connections import SecretVault

url = os.environ.get("DATABASE_URL")
database = (
    AuthDatabase(database_url=url)
    if url
    else AuthDatabase(path=Path(os.environ["SBX_AUTH_DB_PATH"]))
)
app = create_app(
    auth_store=AuthStore(database),
    hosted=True,
    state_backend="postgres",
    connection_vault=SecretVault(
        base64.urlsafe_b64decode(os.environ["SBX_CONNECTION_ENCRYPTION_KEY"])
    ),
    runner_cmd=[sys.executable, "-m", "runtime.runner"],
)
listener = socket.socket(fileno=int(os.environ["SBX_RESTART_SOCKET_FD"]))
uvicorn.Server(
    uvicorn.Config(app, log_level="critical", access_log=False, timeout_graceful_shutdown=1)
).run(sockets=[listener])
