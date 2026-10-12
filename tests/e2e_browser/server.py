"""Credential-free control plane for Console browser tests.

Real composition root, API and Job workers on a throwaway PostgreSQL cluster,
with the fake connector validators and the fake official OpenCode CLI used by
the integration tests. Nothing here can reach a cloud account.

Run: ``uv run python -m tests.e2e_browser.server --port 8811 --origin http://127.0.0.1:4173``
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import signal
import tempfile
import threading
from pathlib import Path
from typing import Any

import psycopg
import uvicorn
from control.api.router import create_api
from control.composition import UnifiedConfig, build_services, start_workers
from control.executors.local import LocalExecutor
from control.persistence.database import Database
from control.security.vault import Vault
from fastapi import APIRouter, HTTPException

from tests.conftest import _is_cloud_key
from tests.support.api import fake_validators
from tests.support.postgres import embedded_postgres
from tests.support.runtime import FAKE_OPENCODE


def _mail_router(mailer: Any) -> APIRouter:
    """Test-only inbox: the browser test reads its own verification link."""
    router = APIRouter()

    @router.get("/__e2e/mail")
    def latest(to: str) -> dict[str, Any]:
        mail = mailer.latest(to)
        if mail is None:
            raise HTTPException(status_code=404, detail="no mail")
        return mail

    return router


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sbx-e2e-browser-server")
    parser.add_argument("--port", type=int, default=8811)
    parser.add_argument("--origin", default="http://127.0.0.1:4173")
    args = parser.parse_args(argv)

    # Same guarantee as tests/conftest.py: no ambient credential reaches the stack.
    for key in list(os.environ):
        if _is_cloud_key(key):
            os.environ.pop(key)
    tmp = Path(tempfile.mkdtemp(prefix="sbx-e2e-browser-"))
    os.environ["HOME"] = str(tmp / "home")
    (tmp / "home").mkdir()

    # uvicorn installs SIGINT/SIGTERM handlers itself; SIGTERM from the test
    # runner must still unwind the cluster below.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    stop = threading.Event()
    try:
        with embedded_postgres() as admin:
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute("CREATE DATABASE sbx_e2e")
            dsn = psycopg.conninfo.make_conninfo(admin, dbname="sbx_e2e")
            db = Database(dsn)
            db.migrate()
            config = UnifiedConfig(
                database_url=dsn,
                vault_keys=Vault.generate_spec(),
                runtime_master_key=secrets.token_bytes(32),
                data_dir=tmp / "data",
                public_url=args.origin,
                allowed_origins=(args.origin,),
                executors=("local",),
                worker_threads=2,
            )
            services = build_services(
                config,
                validators=fake_validators(),
                executors={
                    "local": LocalExecutor(
                        tmp / "executor", extra_env={"OPENCODE_BIN": FAKE_OPENCODE}
                    )
                },
                db=db,
            )
            start_workers(services, stop)
            app = create_api(services, extra_routers=[_mail_router(services.mailer)])
            try:
                uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
            except KeyboardInterrupt:
                pass
            finally:
                stop.set()
                local = services.execution.executors["local"]
                for lease in db.read(lambda u: u.find("executor_leases", {})):
                    if lease["handle"]:
                        local.terminate(lease["handle"], "cleanup", None)
                db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
