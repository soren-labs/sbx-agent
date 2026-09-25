"""Minimal ``/v1`` client for doctor/smoke (SOR-98).

Thin over ``httpx.Client`` with an injectable transport so tests can run the
full smoke path deterministically. Errors decode the canonical
``{error: {code, message}}`` body into :class:`ApiError`.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """Non-2xx ``/v1`` response with the canonical error code when present."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class V1Client:
    """Bearer-authenticated ``/v1`` caller. Token never leaves this object."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> V1Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _check(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            code = f"http_{resp.status_code}"
            message = resp.text[:200]
            try:
                error = resp.json().get("error")
                if isinstance(error, dict):
                    code = str(error.get("code") or code)
                    message = str(error.get("message") or message)
            except ValueError:
                pass
            raise ApiError(resp.status_code, code, message)
        if not resp.content:
            return {}
        return resp.json()

    def get(self, path: str) -> Any:
        return self._check(self._client.get(path))

    def post(self, path: str, body: Any | None = None) -> Any:
        kwargs = {} if body is None else {"json": body}
        return self._check(self._client.post(path, **kwargs))

    def me(self) -> dict[str, Any]:
        return self.get("/v1/me")

    def models(self) -> dict[str, Any]:
        return self.get("/v1/models")

    def create_agent(
        self, *, prompt: str, provider: str, name: str = "sbx-smoke"
    ) -> dict[str, Any]:
        return self._check(
            self._client.post(
                "/v1/agents",
                json={
                    "prompt": {"text": prompt},
                    "agent": {"provider": provider},
                    "name": name,
                },
            )
        )

    def list_agents(self, *, cursor: str | None = None) -> dict[str, Any]:
        params = {} if cursor is None else {"cursor": cursor}
        return self._check(self._client.get("/v1/agents", params=params))

    def get_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return self.get(f"/v1/agents/{agent_id}/runs/{run_id}")

    def delete_agent(self, agent_id: str) -> Any:
        return self._check(self._client.delete(f"/v1/agents/{agent_id}"))
