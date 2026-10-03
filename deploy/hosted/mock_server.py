#!/usr/bin/env python3
"""Loopback mock deployment; private state and a credential-free environment."""

import argparse
import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path


def build_app(state: Path, *, database_url: str | None = None, origins: str = ""):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    home = state / "home"
    home.mkdir(exist_ok=True, mode=0o700)
    key_file = state / "connection-vault.key"
    if not key_file.exists():
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
    repository = Path(__file__).resolve().parents[2]
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(state),
        "SBX_HOSTED": "1",
        "SBX_STATE_BACKEND": "postgres",
        "SBX_BACKEND": "local",
        "SBX_CONNECTIONS_MODE": "mock",
        "SBX_AUTH_EMAIL_MODE": "mock",
        "SBX_CONNECTION_ENCRYPTION_KEY": key_file.read_text().strip(),
        "SBX_BROWSER_ORIGINS": origins,
        "CODEX_BIN": str(repository / "tests/fakes/hosted_codex.py"),
        "PYTHONPATH": str(repository),
    }
    if database_url:
        environment["DATABASE_URL"] = database_url
    os.environ.clear()
    os.environ.update(environment)
    if not database_url:
        # The legacy module-level ASGI default is imported before this mock's
        # explicit SQLite test/dev store is injected. Keep that lazy default
        # off the hosted PostgreSQL path during import only.
        os.environ["SBX_HOSTED"] = "0"
        os.environ["SBX_STATE_BACKEND"] = "legacy"
    from control.app import create_app
    from control.auth_email import MockEmailSender
    from control.auth_store import AuthDatabase, AuthStore

    os.environ["SBX_HOSTED"] = "1"
    os.environ["SBX_STATE_BACKEND"] = "postgres"

    sender = MockEmailSender()
    auth = AuthStore(
        AuthDatabase(database_url=database_url)
        if database_url
        else AuthDatabase(path=state / "mock.sqlite3")
    )
    app = create_app(
        auth_store=auth,
        email_sender=sender,
        hosted=True,
        state_backend="postgres",
        runner_cmd=[sys.executable, "-m", "runtime.runner"],
    )
    from fastapi import Request
    from fastapi.responses import JSONResponse

    # A dev-only mail inbox, installed only by this launcher. It requires the
    # unguessable challenge ID; production factory never mounts this route.
    @app.get("/dev/email-inbox/{challenge_id}")
    def inbox(challenge_id: str, request: Request):
        if (
            request.client.host not in {"127.0.0.1", "::1", "testclient"}
            and request.url.hostname != "localhost"
        ):
            return JSONResponse({"error": "not_found"}, status_code=404)
        with auth.database.transaction() as conn:
            row = auth.database.execute(
                conn,
                "SELECT email FROM email_verification_challenges "
                "WHERE id = ? AND consumed_at IS NULL AND expires_at > ?",
                (challenge_id, auth.clock()),
            ).fetchone()
        if row is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            return JSONResponse(
                {"code": sender.latest_code(row["email"])},
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        except StopIteration:
            return JSONResponse({"error": "not_found"}, status_code=404)

    # Insert before the optional same-origin SPA catch-all.
    inbox_route = app.router.routes.pop()
    first_mount = next(
        (
            i
            for i, route in enumerate(app.router.routes)
            if getattr(route, "path", None) in {"", "/"}
        ),
        len(app.router.routes),
    )
    app.router.routes.insert(first_mount, inbox_route)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state", type=Path, default=Path(tempfile.gettempdir()) / f"sbx-hosted-mock-{os.getuid()}"
    )
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    args = parser.parse_args()
    app = build_app(
        args.state.resolve(),
        database_url=os.environ.get("DATABASE_URL"),
        origins=os.environ.get("SBX_BROWSER_ORIGINS", ""),
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
