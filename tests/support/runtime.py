"""Helpers to run a real sbx-runtime daemon in-process against fake official CLIs."""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from protocol.runtime import encode_grant, request_digest
from runtime.daemon.app import RuntimeDaemon
from runtime.harnesses.codex import CodexHarness
from runtime.harnesses.opencode import OpenCodeHarness

FAKE_OPENCODE = str(Path(__file__).resolve().parents[1] / "fakes" / "official" / "opencode_cli.py")


class DaemonHarness:
    def __init__(
        self,
        base: Path,
        *,
        lease_id: str = "lease_test",
        generation: int = 1,
        max_unacked: int = 20000,
    ) -> None:
        os.environ["OPENCODE_BIN"] = FAKE_OPENCODE
        self.base = base
        self.key = secrets.token_bytes(32)
        self.lease_id = lease_id
        self.generation = generation
        self.max_unacked = max_unacked
        self.start()

    def start(self) -> None:
        self.daemon = RuntimeDaemon(
            state_dir=self.base / "state",
            work_dir=self.base / "work",
            key=self.key,
            lease_id=self.lease_id,
            generation=self.generation,
            image_digest="sha256:test",
            harnesses={
                "opencode": OpenCodeHarness("1.18.34-fake"),
                "codex": CodexHarness("not_installed"),
            },
            max_unacked=self.max_unacked,
        )
        self.server = self.daemon.serve("127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.daemon._stop.set()

    def token(self, scope: str = "manage", generation: int | None = None, ttl: float = 300) -> str:
        claims = {
            "lease_id": self.lease_id,
            "generation": self.generation if generation is None else generation,
            "scope": scope,
            "exp": time.time() + ttl,
        }
        return encode_grant(self.key, claims)

    def post(self, path: str, body: dict[str, Any], *, token: str | None = None) -> httpx.Response:
        return httpx.post(
            self.url + path,
            json=body,
            headers={"Authorization": f"SBX-Grant {token or self.token()}"},
            timeout=60,
        )

    def op(
        self,
        kind: str,
        op_id: str,
        payload: dict[str, Any],
        *,
        secrets_: dict[str, Any] | None = None,
        generation: int | None = None,
        digest: str | None = None,
    ) -> httpx.Response:
        gen = self.generation if generation is None else generation
        frame = {
            "operation_id": op_id,
            "operation_kind": kind,
            "session_id": "sess_test",
            "lease_id": self.lease_id,
            "lease_generation": gen,
            "schema_version": 1,
            "request_digest": digest or request_digest(kind, payload),
            "payload": payload,
            "secrets": secrets_ or {},
        }
        return self.post("/rt/op", frame, token=self.token(generation=gen))

    def wait_op(self, op_id: str, timeout: float = 20) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.post(
                "/rt/query", {"kind": "operation.status", "operation_id": op_id}
            ).json()
            if status["status"] in ("succeeded", "failed", "lost"):
                return status
            time.sleep(0.05)
        raise AssertionError(f"operation {op_id} did not finish")

    def events(self, after: int = 0) -> list[dict[str, Any]]:
        return self.post("/rt/events", {"after": after, "limit": 2000}).json()["items"]
