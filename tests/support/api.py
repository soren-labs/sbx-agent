"""In-process API harness: real composition root, real PostgreSQL, fake connectors."""

from __future__ import annotations

import json
import secrets
import uuid
from pathlib import Path
from typing import Any

from control.application.execution import ExecutionSettings
from control.composition import Services, UnifiedConfig, build_services, build_worker, create_app
from control.executors.local import LocalExecutor
from control.integrations.connectors.base import Observation
from control.security.vault import Vault
from fastapi.testclient import TestClient

from tests.support.runtime import FAKE_OPENCODE

ZEN_CATALOG = {
    "models": [
        {
            "id": "opencode/big-pickle",
            "provider_model": "big-pickle",
            "free": True,
            "usable_via": "official_opencode_cli",
            "pricing_known": True,
        },
        {
            "id": "opencode/gpt-5.4-mini",
            "provider_model": "gpt-5.4-mini",
            "free": False,
            "usable_via": "official_opencode_cli",
            "pricing_known": True,
        },
    ],
    "preferred_model": "opencode/big-pickle",
    "source": "test",
}


def fake_validators(log: list[str] | None = None) -> dict[str, Any]:
    def zen(material: dict[str, Any], **_: Any) -> Observation:
        (log or []).append("zen")
        if material["api_key"].startswith("invalid"):
            return Observation(
                "invalid", details={"reason": "zen_rejected_key"}, quota_consuming=True
            )
        return Observation(
            "ready", details={"auth": "accepted"}, catalog=ZEN_CATALOG, quota_consuming=True
        )

    def modal(material: dict[str, Any], **_: Any) -> Observation:
        if material["token_id"].startswith("ak-bad"):
            return Observation("invalid", details={"reason": "modal_rejected_token"})
        return Observation("ready", details={"probe": "fake"})

    def github(material: dict[str, Any], **_: Any) -> Observation:
        return Observation("ready", external_identity="octo-test", details={"scopes": ["repo"]})

    def codex(material: dict[str, Any], **_: Any) -> Observation:
        return Observation("ready", details={"probe": "format_only"})

    return {"opencode_zen": zen, "modal": modal, "github": github, "codex": codex}


def make_config(
    db: Any, tmp: Path, *, vault_keys: str | None = None, master: bytes | None = None
) -> UnifiedConfig:
    return UnifiedConfig(
        database_url=db.dsn,
        vault_keys=vault_keys or Vault.generate_spec(),
        runtime_master_key=master or secrets.token_bytes(32),
        data_dir=tmp,
        public_url="http://testserver",
        allowed_origins=("http://testserver",),
        execution=ExecutionSettings(
            poll_busy=0.05, poll_idle=0.1, unreachable_threshold=3, turn_deadline_seconds=60
        ),
    )


class ApiStack:
    def __init__(
        self,
        db: Any,
        tmp: Path,
        *,
        config: UnifiedConfig | None = None,
        executors: dict[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.config = config or make_config(db, tmp)
        executors = (
            executors
            if executors is not None
            else {
                "local": LocalExecutor(tmp / "executor", extra_env={"OPENCODE_BIN": FAKE_OPENCODE})
            }
        )
        self.services: Services = build_services(
            self.config, validators=fake_validators(), executors=executors, db=db
        )
        self.app = create_app(self.services)
        self.worker = build_worker(self.services)

    def client(self) -> TestClient:
        return TestClient(self.app, base_url="http://testserver")

    def drain(self, rounds: int = 200) -> None:
        for _ in range(rounds):
            if not self.worker.run_once():
                return

    def drive(self, until: Any, timeout: float = 30) -> None:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if until():
                return
            if not self.worker.run_once():
                time.sleep(0.03)
        raise AssertionError("condition not reached")

    def shutdown(self) -> None:
        local = self.services.execution.executors.get("local")
        for lease in self.db.read(lambda u: u.find("executor_leases", {})):
            if lease["handle"] and local is not None and lease["backend"] == "local":
                local.terminate(lease["handle"], "cleanup", None)


class User:
    """A signed-in browser-like client (cookie + CSRF) for one product user."""

    def __init__(
        self, stack: ApiStack, email: str | None = None, password: str = "correct horse battery 9"
    ) -> None:
        self.stack = stack
        self.email = email or f"user-{uuid.uuid4().hex[:8]}@example.test"
        self.password = password
        self.http = stack.client()
        assert (
            self.http.post(
                "/api/auth/register", json={"email": self.email, "password": password}
            ).status_code
            == 202
        )
        mail = stack.services.mailer.latest(self.email)
        token = mail["body"].split("token=")[1].strip()
        assert (
            self.http.post("/api/auth/email-verifications", json={"token": token}).status_code
            == 200
        )
        login = self.http.post("/api/auth/login", json={"email": self.email, "password": password})
        assert login.status_code == 200, login.text
        self.csrf = login.json()["csrf_token"]
        self.me = login.json()
        self.workspace_id = self.me["workspaces"][0]["id"]
        self.responses: list[str] = []

    def _headers(self, key: str | None) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf, "Idempotency-Key": key or uuid.uuid4().hex}

    def get(self, path: str, **kw: Any) -> Any:
        r = self.http.get(path, **kw)
        self.responses.append(r.text)
        return r

    def post(self, path: str, body: dict[str, Any] | None = None, *, key: str | None = None) -> Any:
        r = self.http.post(path, json=body or {}, headers=self._headers(key))
        self.responses.append(r.text)
        return r

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        r = self.http.patch(path, json=body, headers=self._headers(None))
        self.responses.append(r.text)
        return r

    def delete(self, path: str) -> Any:
        r = self.http.delete(path, headers=self._headers(None))
        self.responses.append(r.text)
        return r

    def connect(
        self, kind: str, credential: dict[str, Any], label: str | None = None
    ) -> dict[str, Any]:
        r = self.post(
            f"/api/workspaces/{self.workspace_id}/connections",
            {"kind": kind, "label": label or kind, "credential": credential},
        )
        assert r.status_code == 201, r.text
        return r.json()

    def all_text(self) -> str:
        return "\n".join(self.responses) + json.dumps(self.me)
