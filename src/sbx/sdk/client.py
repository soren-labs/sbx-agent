"""Unified high-level client. Server projections, no local agent loop."""

import time
from uuid import uuid4

import httpx


class ApiError(Exception):
    def __init__(self, code, status=None):
        self.code, self.status = code, status
        super().__init__(code)


class Resource:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def get(self, rid):
        return self.client.request("GET", f"/api/{self.name}/{rid}")

    def action(self, rid, action, body=None, key=None):
        return self.client.request("POST", f"/api/{self.name}/{rid}/{action}", body or {}, key=key)


class Client:
    def __init__(self, base_url, *, api_key=None, transport=None, timeout=30):
        self.http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={"Authorization": "Bearer " + api_key} if api_key else {},
        )
        for name in (
            "projects",
            "connections",
            "sessions",
            "messages",
            "turns",
            "changesets",
            "deliveries",
            "delegations",
            "operations",
        ):
            setattr(self, name, Resource(self, name))

    def request(self, method, path, body=None, *, key=None):
        headers = {}
        if method != "GET":
            headers["Idempotency-Key"] = key or uuid4().hex
            if self.http.cookies.get("sbx_csrf"):
                headers["X-CSRF-Token"] = self.http.cookies["sbx_csrf"]
        for attempt in range(2):
            try:
                response = self.http.request(method, path, json=body, headers=headers)
                break
            except httpx.TransportError:
                if attempt:
                    raise ApiError("outcome_unknown") from None
        if response.status_code >= 400:
            raise ApiError(
                response.json().get("error", {}).get("code", "request_failed"), response.status_code
            )
        return response.json()

    def login(self, email, password):
        return self.request("POST", "/api/auth/login", {"email": email, "password": password})

    def create(self, workspace_id, resource, body, *, key=None):
        return self.request("POST", f"/api/workspaces/{workspace_id}/{resource}", body, key=key)

    def send(self, session_id, content, *, key=None):
        return self.sessions.action(session_id, "messages", {"content": content}, key)

    def events(self, session_id, after=0):
        return self.request("GET", f"/api/sessions/{session_id}/events?after={after}")

    def wait_turn(self, turn_id, *, deadline=600):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            turn = self.turns.get(turn_id)
            if turn["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return turn
            time.sleep(0.5)
        raise ApiError("wait_deadline")

    def wait_result(self, delegation_id, *, deadline=600):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            delegation = self.delegations.get(delegation_id)
            if delegation.get("result"):
                return delegation["result"]
            if delegation["state"] in {"failed", "cancelled"}:
                raise ApiError("output_contract_invalid")
            time.sleep(0.5)
        raise ApiError("wait_deadline")

    def execute(self, prompt, *, workspace_id=None, session_id=None, deadline=600, **settings):
        if session_id:
            accepted = self.send(session_id, prompt)
        else:
            accepted = self.create(
                workspace_id, "sessions", {**settings, "message": {"content": prompt}}
            )
        return self.wait_turn(accepted["turn_id"], deadline=deadline)

    def close(self):
        self.http.close()
