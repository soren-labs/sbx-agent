"""MVP acceptance harness — boots the full unified product on disposable
Postgres with REAL Modal + official opencode CLI + real GitHub against
soren-labs/sbx-e2e-test (RFC 167 §MVP).

Secret classes used: SBX email+password, OpenCode Zen key, Modal token
pair, GitHub manual token. No Codex/ChatGPT anywhere.

This module contains no pytest marks — ``test_real_mvp.py`` wraps it.
Run directly::

    SBX_MVP=1 python -m tests.mvp.run_acceptance
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_REPO = os.environ.get("SBX_MVP_REPO", "soren-labs/sbx-e2e-test")
E2E_BASE_REF = os.environ.get("SBX_MVP_BASE_REF", "main")


def have_mvp_env() -> tuple[bool, list[str]]:
    missing = []
    if not os.environ.get("SBX_MVP_DATABASE_URL") and not os.environ.get("SBX_TEST_DATABASE_URL"):
        missing.append("database_url")
    if not os.environ.get("OPENCODE_ZEN_API_KEY"):
        missing.append("opencode_zen")
    if not (os.environ.get("SBX_MODAL_TOML") or os.environ.get("SBX_TEST_MODAL_TOKEN_ID")):
        missing.append("modal")
    if shutil.which("gh") is None and not os.environ.get("GH_TOKEN"):
        missing.append("github")
    return (not missing, missing)


def _modal_tokens() -> dict:
    """Parse the injected Modal credential source — never printed."""
    if toml_src := os.environ.get("SBX_MODAL_TOML"):
        data = tomllib.loads(toml_src)
        for profile in data.values():
            if isinstance(profile, dict) and profile.get("token_id"):
                return {
                    "token_id": profile["token_id"],
                    "token_secret": profile.get("token_secret", ""),
                }
    return {
        "token_id": os.environ.get("SBX_TEST_MODAL_TOKEN_ID", ""),
        "token_secret": os.environ.get("SBX_TEST_MODAL_TOKEN_SECRET", ""),
    }


def _github_token() -> str:
    if tok := os.environ.get("GH_TOKEN"):
        return tok
    out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_world(dsn: str, workdir: Path) -> dict:
    """Compose the whole product exactly as a deployment would."""
    from control.api.app import create_app
    from control.application.auth import AuthService
    from control.application.changes import ChangeSetService
    from control.application.connections import ConnectionService
    from control.application.delegation import DelegationService
    from control.application.delivery import DeliveryService
    from control.application.models import ModelDiscoveryService
    from control.application.projects import ProjectService
    from control.application.runtime_stack import (
        RuntimeStack,
        make_compute_resolver,
        make_connection_resolver,
        make_git_resolver,
        make_remote_factory,
    )
    from control.application.sessions import SessionService
    from control.connectors import register_builtin_connectors
    from control.jobs import handlers
    from control.jobs.worker import Worker
    from control.persistence.database import open_database
    from control.security.vault import Vault
    from control.storage.blobs import BlobStore

    db = open_database(dsn)
    vault = Vault.generate()
    registry = register_builtin_connectors()
    connections = ConnectionService(db, vault)
    sessions = SessionService(db)
    blobs = BlobStore(str(workdir / "blobs"))
    changes = ChangeSetService(db, blobs)
    delivery = DeliveryService(
        db,
        remote_factory=make_remote_factory(connections, db),
        connection_service=connections,
        blob_store=blobs,
        scratch_root=workdir / "delivery-scratch",
    )
    delegations = DelegationService(db)

    stack = RuntimeStack(
        db,
        backends={},
        credential_resolver=make_connection_resolver(connections, db),
        git_resolver=make_git_resolver(connections, db),
        compute_resolver=make_compute_resolver(connections, db),
    )
    stack.start()
    from control.executors.local import LocalExecutorBackend
    from control.executors.modal import ModalExecutorBackend

    stack.register_backend(
        "local",
        LocalExecutorBackend(
            run_root=workdir / "exec-local",
            ingress_endpoint=stack.ingress.endpoint,
            repo_root=REPO_ROOT,
        ),
    )
    stack.register_backend("modal", ModalExecutorBackend())

    handlers.set_runtime_stack(stack)
    handlers.set_connection_plane(connections, registry)
    handlers.set_change_plane(changes, delegations)
    handlers.set_delivery_plane(delivery)
    handlers.set_delegation_plane(delegations)

    app = create_app(
        db,
        auth=AuthService(db),
        projects=ProjectService(db),
        connections=connections,
        sessions=sessions,
        changes=changes,
        delivery=delivery,
        delegations=delegations,
        models=ModelDiscoveryService(db, connections, registry),
        runtime_stack=stack,
        execution=stack.service,
    )
    worker = Worker(db=db, holder=f"mvp-{uuid.uuid4().hex[:8]}", handlers=handlers.HANDLERS)
    return {
        "db": db,
        "vault": vault,
        "registry": registry,
        "connections": connections,
        "sessions": sessions,
        "changes": changes,
        "delivery": delivery,
        "delegations": delegations,
        "stack": stack,
        "app": app,
        "worker": worker,
    }


class RunningWorld:
    """HTTP server + worker loop bound to a built world."""

    def __init__(self, world: dict) -> None:
        self.world = world
        self.port = _free_port()
        self._stop = threading.Event()
        self._server = None
        self._threads: list[threading.Thread] = []

    def __enter__(self) -> RunningWorld:
        import uvicorn

        cfg = uvicorn.Config(
            self.world["app"],
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(cfg)
        t = threading.Thread(target=self._server.run, name="mvp-api", daemon=True)
        t.start()
        self._threads.append(t)

        def _pump() -> None:
            while not self._stop.is_set():
                try:
                    n = self.world["worker"].run_until_idle(max_steps=8)
                except Exception:
                    n = 0
                if n == 0:
                    time.sleep(0.25)

        w = threading.Thread(target=_pump, name="mvp-worker", daemon=True)
        w.start()
        self._threads.append(w)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self._server.started:
                break
            time.sleep(0.05)
        return self

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.should_exit = True
        for t in self._threads:
            t.join(timeout=5)
        stack = self.world["stack"]
        try:
            stack.stop()
        except Exception:
            pass
        try:
            self.world["db"].close()
        except Exception:
            pass
        from control.jobs import handlers

        handlers.set_runtime_stack(None)


def wait_for(pred, timeout: float, poll: float = 1.0, desc: str = "") -> object:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(poll)
    raise TimeoutError(f"wait_for timed out ({desc}): last={last!r}")


def scratch_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="sbx-mvp-"))
