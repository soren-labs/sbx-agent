"""Composition root: wires infrastructure into application services, API and workers.

Run: ``python -m control.composition serve`` (API + in-process workers),
``... worker`` or ``... migrate``. Configuration comes from SBX_* variables.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from control.application.changes import Changes
from control.application.connections import Connections
from control.application.delegation import Delegations
from control.application.delivery import Deliveries
from control.application.execution import ExecutionService, ExecutionSettings
from control.application.identity import Identity
from control.application.live import LiveWorkspace
from control.application.project_resolution import ProjectResolver
from control.application.projections import Queries
from control.application.projects import Projects
from control.application.services import Services as ServiceDesires
from control.application.sessions import Sessions
from control.application.tools import ToolGateway
from control.catalog import load_catalog
from control.executors.local import LocalExecutor
from control.executors.modal import ModalExecutor
from control.integrations.connectors.registry import CONNECTORS
from control.integrations.email import FileMailSink, ResendMailer
from control.integrations.git import GitTransport
from control.integrations.github import GitHubHost
from control.jobs.handlers.execution import handlers as execution_handlers
from control.jobs.worker import Worker
from control.persistence.database import Database
from control.runtime_client.client import HttpRuntimeConnector
from control.security.credential_leases import VaultCredentialBroker
from control.security.redaction import install_log_redaction
from control.security.vault import Vault
from control.storage.local import LocalBlobStore

log = logging.getLogger("sbx")


@dataclass
class UnifiedConfig:
    database_url: str
    vault_keys: str
    runtime_master_key: bytes
    data_dir: Path
    public_url: str = "http://localhost:8800"
    allowed_origins: tuple[str, ...] = ()
    cookie_secure: bool = False
    executors: tuple[str, ...] = ("local", "modal")
    local_extra_env: dict[str, str] = field(default_factory=dict)
    resend_api_key: str | None = None
    mail_from: str = "SBX <no-reply@example.invalid>"
    worker_threads: int = 4
    idle_release_seconds: float = 1800.0
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)

    @classmethod
    def from_env(cls) -> UnifiedConfig:
        env = os.environ
        master = env.get("SBX_RUNTIME_MASTER_KEY")
        if not env.get("SBX_DATABASE_URL") or not env.get("SBX_VAULT_KEYS") or not master:
            raise SystemExit(
                "SBX_DATABASE_URL, SBX_VAULT_KEYS and SBX_RUNTIME_MASTER_KEY are required"
            )
        public = env.get("SBX_PUBLIC_URL", "http://localhost:8800")
        origins = tuple(o for o in env.get("SBX_ALLOWED_ORIGINS", public).split(",") if o)
        resend_api_key = env.get("SBX_RESEND_API_KEY")
        mail_from = env.get("SBX_MAIL_FROM")
        if resend_api_key and not mail_from:
            # Resend rejects senders outside a verified domain; fail at startup, not per email.
            raise SystemExit("SBX_MAIL_FROM is required when SBX_RESEND_API_KEY is set")
        return cls(
            database_url=env["SBX_DATABASE_URL"],
            vault_keys=env["SBX_VAULT_KEYS"],
            runtime_master_key=bytes.fromhex(master),
            data_dir=Path(env.get("SBX_DATA_DIR", "./.sbx-data")),
            public_url=public,
            allowed_origins=origins,
            cookie_secure=env.get("SBX_COOKIE_SECURE", "0") == "1",
            executors=tuple(env.get("SBX_EXECUTORS", "local,modal").split(",")),
            resend_api_key=resend_api_key,
            mail_from=mail_from or cls.mail_from,
            worker_threads=int(env.get("SBX_WORKER_THREADS", "4")),
        )


@dataclass
class Services:
    config: UnifiedConfig
    tx: Database
    catalog: Any
    identity: Identity
    projects: Projects
    connections: Connections
    sessions: Sessions
    queries: Queries
    execution: ExecutionService
    broker: VaultCredentialBroker
    mailer: Any
    changes: Changes
    deliveries: Deliveries
    delegations: Delegations
    tools: ToolGateway
    live: LiveWorkspace
    services: ServiceDesires
    handlers: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)


def build_services(
    config: UnifiedConfig,
    *,
    validators: dict[str, Any] | None = None,
    executors: dict[str, Any] | None = None,
    db: Database | None = None,
    git: Any = None,
    host: Any = None,
) -> Services:
    db = db or Database(config.database_url)
    vault = Vault.from_spec(config.vault_keys)
    catalog = load_catalog()
    mailer = (
        ResendMailer(config.resend_api_key, config.mail_from)
        if config.resend_api_key
        else FileMailSink(config.data_dir / "mail")
    )
    broker = VaultCredentialBroker(db, vault, catalog)
    if executors is None:
        executors = {}
        if "local" in config.executors:
            executors["local"] = LocalExecutor(
                config.data_dir / "executor", extra_env=config.local_extra_env
            )
        if "modal" in config.executors:
            executors["modal"] = ModalExecutor()
    connector = HttpRuntimeConnector(config.runtime_master_key)
    blobs = LocalBlobStore(config.data_dir / "blobs")
    execution = ExecutionService(
        db,
        executors=executors,
        connector=connector,
        credentials=broker,
        blobs=blobs,
        catalog=catalog,
        settings=config.execution,
    )
    connections = Connections(db, vault, CONNECTORS, validators=validators)
    sessions = Sessions(db, ProjectResolver(catalog), catalog)
    queries = Queries(db)
    changes = Changes(db, connector, blobs, broker)
    deliveries = Deliveries(
        db, broker, git or GitTransport(config.data_dir / "git"), host or GitHubHost(), blobs
    )
    delegations = Delegations(db, sessions, connector, blobs)
    tools = ToolGateway(
        db, delegations=delegations, queries=queries, changes=changes, deliveries=deliveries
    )
    execution.hooks.turn_terminal += [changes.on_turn_terminal, delegations.on_turn_terminal]
    execution.post_restore_hooks.append(delegations.apply_inputs)
    live = LiveWorkspace(db, connector, broker)
    service_desires = ServiceDesires(db, connector)
    execution.post_restore_hooks.append(service_desires.ensure_on_activation)
    changes.ready_hooks.append(deliveries.on_changeset_ready)
    sessions.close_hooks.append(delegations.on_parent_close)
    connections.dependency_hooks.append(deliveries.dependents)
    services = Services(
        config=config,
        tx=db,
        catalog=catalog,
        identity=Identity(db, mailer, public_url=config.public_url),
        projects=Projects(db),
        connections=connections,
        sessions=sessions,
        queries=queries,
        execution=execution,
        broker=broker,
        mailer=mailer,
        changes=changes,
        deliveries=deliveries,
        delegations=delegations,
        tools=tools,
        live=live,
        services=service_desires,
    )
    services.handlers = {
        **execution_handlers(execution),
        "connection.validate": connections.handle_validate,
        "changeset.capture": changes.handle_capture,
        "changeset.apply": changes.handle_apply,
        "delivery.perform": deliveries.handle_perform,
        "delivery.reconcile": deliveries.handle_reconcile,
        "delivery.merge": deliveries.handle_merge,
        "delegation.publish_result": delegations.handle_publish,
        "service.ensure": service_desires.handle,
        "service.stop": service_desires.handle,
    }
    return services


def create_app(services: Services) -> Any:
    from control.api.router import create_api

    return create_api(services)


def build_worker(services: Services, *, holder: str | None = None) -> Worker:
    return Worker(services.tx, services.handlers, holder=holder)


def start_workers(services: Services, stop: threading.Event) -> list[threading.Thread]:
    from control.jobs.timers import enqueue_idle_releases, enqueue_quarantine_releases

    threads = []
    for i in range(services.config.worker_threads):
        worker = build_worker(services, holder=f"worker-{os.getpid()}-{i}-{secrets.token_hex(3)}")
        t = threading.Thread(
            target=worker.run_forever, args=(stop,), daemon=True, name=f"sbx-worker-{i}"
        )
        t.start()
        threads.append(t)

    def timers() -> None:
        while not stop.wait(60):
            try:
                enqueue_idle_releases(
                    services.tx, idle_seconds=services.config.idle_release_seconds
                )
                enqueue_quarantine_releases(services.tx)
            except Exception:
                log.exception("timer failure")

    threading.Thread(target=timers, daemon=True, name="sbx-timers").start()
    return threads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sbx-control")
    parser.add_argument("command", choices=["serve", "worker", "migrate"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    install_log_redaction()
    config = UnifiedConfig.from_env()
    db = Database(config.database_url)
    db.migrate()
    if args.command == "migrate":
        return 0
    services = build_services(config, db=db)
    stop = threading.Event()
    start_workers(services, stop)
    if args.command == "worker":
        stop.wait()
        return 0
    import uvicorn

    uvicorn.run(
        create_app(services), host=args.host, port=args.port, log_level="warning", access_log=False
    )
    stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
