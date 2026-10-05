import time
from uuid import uuid4

import httpx
from protocol.runtime import OperationFrame, digest

from control.domain.errors import DomainError


class RuntimeClient:
    def __init__(self, url, token, *, transport=None):
        self.http = httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + token},
            transport=transport,
            timeout=30,
        )
        self.hello = self.get("/hello")
        if self.hello["protocol_major"] != 1:
            raise DomainError("runtime_incompatible")

    def get(self, path, **params):
        try:
            response = self.http.get(path, params=params)
        except httpx.TransportError:
            raise DomainError("executor_unavailable") from None
        if response.status_code == 404:
            raise DomainError("not_found")
        if response.status_code >= 400:
            raise DomainError("executor_unavailable")
        return response.json()

    def post(self, path, body):
        try:
            response = self.http.post(path, json=body)
        except httpx.TransportError:
            raise DomainError("executor_unavailable") from None
        if response.status_code >= 400:
            raise DomainError(response.json().get("error", "executor_unavailable"))
        return response.json()

    def submit(self, operation_id, kind, payload, *, expires=3600):
        frame = OperationFrame(
            operation_id=operation_id,
            operation_kind=kind,
            session_id=self.hello["session_id"],
            lease_id=self.hello["lease_id"],
            lease_generation=self.hello["lease_generation"],
            resource_fence=self.hello["lease_generation"],
            grant_id="grant_" + uuid4().hex,
            grant_expires_at=time.time() + expires,
            request_digest=digest(payload),
            payload=payload,
        )
        return self.post("/operations", frame.model_dump())

    def wait(self, operation_id, deadline=600, tick=None):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            row = self.get("/operations/" + operation_id)
            if row["state"] == "terminal":
                return row["result"]
            if tick:
                tick(row)
            time.sleep(0.1)
        raise DomainError("outcome_unknown")
