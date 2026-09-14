#!/usr/bin/env python3
"""Minimal sbx-browser public API client (Cursor Cloud Agents shape).

Usage:
    SBX_API_KEY=sbx_... SBX_BASE_URL=https://sbx.sorenforge.com \
        python examples/sbx_client.py "Write hello.txt containing hi"

The demo flow: create an agent (first run starts immediately), stream its
events over SSE, send a follow-up run, cancel it, then delete the agent.
Only dependency: httpx.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator

import httpx

DEFAULT_BASE_URL = "https://sbx.sorenforge.com"


class SbxClient:
    """Thin wrapper over ``/v1/*``. Pass ``client=`` to inject a transport."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns = client is None
        self.http = client or httpx.Client(
            base_url=(base_url or os.environ.get("SBX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            headers={"Authorization": f"Bearer {api_key or os.environ['SBX_API_KEY']}"},
            timeout=None,
        )

    def close(self) -> None:
        if self._owns:
            self.http.close()

    def _check(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            error = resp.json().get("error", {})
            raise RuntimeError(f"{resp.status_code} {error.get('code')}: {error.get('message')}")
        return resp.json() if resp.content else {}

    def create_agent(
        self,
        text: str,
        provider: str = "codex",
        account_id: str = "auto",
        model: str | None = None,
        name: str | None = None,
    ) -> dict:
        body = {"prompt": {"text": text}, "agent": {"provider": provider, "account_id": account_id}}
        if model:
            body["agent"]["model"] = model
        if name:
            body["name"] = name
        return self._check(self.http.post("/v1/agents", json=body))

    def list_agents(self, **params) -> dict:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._check(self.http.get("/v1/agents", params=clean))

    def get_agent(self, agent_id: str) -> dict:
        return self._check(self.http.get(f"/v1/agents/{agent_id}"))

    def delete_agent(self, agent_id: str) -> dict:
        return self._check(self.http.delete(f"/v1/agents/{agent_id}"))

    def create_run(self, agent_id: str, text: str) -> dict:
        body = {"prompt": {"text": text}}
        return self._check(self.http.post(f"/v1/agents/{agent_id}/runs", json=body))

    def list_runs(self, agent_id: str) -> list[dict]:
        return self._check(self.http.get(f"/v1/agents/{agent_id}/runs"))["runs"]

    def get_run(self, agent_id: str, run_id: str) -> dict:
        return self._check(self.http.get(f"/v1/agents/{agent_id}/runs/{run_id}"))

    def cancel_run(self, agent_id: str, run_id: str) -> dict:
        return self._check(self.http.post(f"/v1/agents/{agent_id}/runs/{run_id}/cancel"))

    def usage(self, agent_id: str) -> dict:
        return self._check(self.http.get(f"/v1/agents/{agent_id}/usage"))

    def models(self) -> list[dict]:
        return self._check(self.http.get("/v1/models"))["models"]

    def me(self) -> dict:
        return self._check(self.http.get("/v1/me"))

    def stream_run(
        self, agent_id: str, run_id: str, last_event_id: int | None = None
    ) -> Iterator[dict]:
        """Yield canonical events for one run (SSE). ``Last-Event-ID`` resumes."""
        headers = {"Last-Event-ID": str(last_event_id)} if last_event_id else {}
        with self.http.stream(
            "GET", f"/v1/agents/{agent_id}/runs/{run_id}/stream", headers=headers
        ) as resp:
            resp.raise_for_status()
            data_lines: list[str] = []
            for line in resp.iter_lines():
                if not line:
                    if data_lines:
                        yield json.loads("\n".join(data_lines))
                        data_lines = []
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())


def _wait_finished(client: SbxClient, agent_id: str, run_id: str, timeout_s: float = 120) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = client.get_run(agent_id, run_id)
        if run["status"] in ("FINISHED", "ERROR", "CANCELLED", "EXPIRED"):
            return run
        time.sleep(1)
    return client.get_run(agent_id, run_id)


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Create hello.txt containing 'hi'."
    client = SbxClient()
    try:
        created = client.create_agent(prompt, provider="codex")
        agent, run = created["agent"], created["run"]
        print(f"agent {agent['id']} ({agent['status']}); first run {run['id']} ({run['status']})")
        for event in client.stream_run(agent["id"], run["id"]):
            if event.get("type") == "sbx.turn_finished":
                break
        run = _wait_finished(client, agent["id"], run["id"])
        result_text = (run.get("result") or {}).get("text", "")[:80]
        print(f"run {run['id']} -> {run['status']}: {result_text}")
        follow = client.create_run(agent["id"], "Now append a second line.")
        print(f"follow-up {follow['id']} ({follow['status']}); cancelling")
        cancelled = client.cancel_run(agent["id"], follow["id"])
        print(f"cancelled -> {cancelled['status']}")
        print("usage:", json.dumps(client.usage(agent["id"])))
        closed = client.delete_agent(agent["id"])
        print(f"agent {closed['id']} -> {closed['status']}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
