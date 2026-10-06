"""HTTP transport for runtime frames. Maps transport failures to RuntimeUnavailable."""

from __future__ import annotations

from typing import Any

import httpx
from protocol.runtime import PAYLOAD_SCHEMA_VERSION, PROTOCOL_MAJOR, request_digest

from control.application.ports import RuntimeRefused, RuntimeUnavailable
from control.runtime_client.grants import lease_key, mint


class RuntimeClient:
    def __init__(
        self, endpoint: str, key: bytes, lease_id: str, generation: int, *, timeout: float = 120.0
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.key = key
        self.lease_id = lease_id
        self.generation = generation
        self.timeout = timeout

    def _post(self, path: str, body: dict[str, Any], scope: str = "manage") -> dict[str, Any]:
        token = mint(self.key, self.lease_id, self.generation, scope=scope)
        try:
            response = httpx.post(
                self.endpoint + path,
                json=body,
                headers={"Authorization": f"SBX-Grant {token}"},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise RuntimeUnavailable(type(exc).__name__) from None
        if response.status_code >= 500 or response.status_code in (502, 503, 504):
            raise RuntimeUnavailable(f"runtime http {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise RuntimeUnavailable("non-json runtime response") from None
        if response.status_code >= 400:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            raise RuntimeRefused(error.get("code", "runtime_error"), error.get("message", ""))
        return data

    def hello(self) -> dict[str, Any]:
        return self._post("/rt/hello", {"protocol_majors": [PROTOCOL_MAJOR]}, scope="read")

    def op(
        self,
        kind: str,
        operation_id: str,
        session_id: str,
        payload: dict[str, Any],
        *,
        secrets: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        frame = {
            "operation_id": operation_id,
            "operation_kind": kind,
            "session_id": session_id,
            "lease_id": self.lease_id,
            "lease_generation": self.generation,
            "schema_version": PAYLOAD_SCHEMA_VERSION,
            "request_digest": request_digest(kind, payload),
            "payload": payload,
            "secrets": secrets or {},
        }
        return self._post("/rt/op", frame)

    def query(self, kind: str, **body: Any) -> dict[str, Any]:
        return self._post("/rt/query", {"kind": kind, **body}, scope="read")

    def events(self, after: int, limit: int = 500) -> dict[str, Any]:
        return self._post("/rt/events", {"after": after, "limit": limit}, scope="read")

    def ack(self, runtime_epoch: str, through: int) -> dict[str, Any]:
        return self._post(
            "/rt/ack", {"runtime_epoch": runtime_epoch, "through": through}, scope="read"
        )


class HttpRuntimeConnector:
    def __init__(self, master_key: bytes, *, timeout: float = 120.0) -> None:
        if len(master_key) < 32:
            raise ValueError("runtime master key must be at least 32 bytes")
        self.master_key = master_key
        self.timeout = timeout

    def enrollment_key(self, lease: dict[str, Any]) -> str:
        return lease_key(self.master_key, lease["id"], lease["generation"]).hex()

    def channel(self, lease: dict[str, Any]) -> RuntimeClient:
        endpoint = (lease.get("handle") or {}).get("endpoint")
        if not endpoint:
            raise RuntimeUnavailable("lease has no runtime endpoint")
        return RuntimeClient(
            endpoint,
            lease_key(self.master_key, lease["id"], lease["generation"]),
            lease["id"],
            lease["generation"],
            timeout=self.timeout,
        )
