"""Unified Python SDK for the single ``/api`` surface (RFC 167 §08).

    from sbx.sdk import UnifiedClient

    client = UnifiedClient(base_url="http://localhost:8000",
                           token="<login token or sbx_k_ key>")
    client.connections.add(workspace_id, "opencode_zen",
                           {"format": "api_key", "payload": {"api_key": ...}})
    sess = client.sessions.create(workspace_id,
                                  harness={"provider_id": "opencode",
                                           "model": "opencode/big-pickle"},
                                  message={"content": {"text": "hi"}})
    out = client.execute("Fix the tests", workspace_id=workspace_id,
                         repository="soren-labs/sbx-e2e-test")

Namespaces: ``projects``, ``connections``, ``sessions``, ``messages``,
``turns``, ``changesets``, ``deliveries``, ``delegations``, ``operations``.
Every mutation sends ``Idempotency-Key``; errors decode the canonical
``error{code,category,message,retryable,...}`` body into
:class:`UnifiedApiError`. Secret inputs are write-only kwargs — the client
never logs or echoes them.
"""

from __future__ import annotations

import itertools
import time
import uuid
from collections.abc import Iterable
from typing import Any

import httpx

from sbx.sdk.errors import SbxApiError, SbxTransportError


class UnifiedApiError(SbxApiError):
    """Canonical unified error body as an exception."""

    @property
    def category(self) -> str | None:
        return (self.details or {}).get("_category")


class UnifiedClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._http = http or httpx.Client(timeout=timeout)
        self._idem = itertools.count()

        self.projects = _Projects(self)
        self.connections = _Connections(self)
        self.sessions = _Sessions(self)
        self.messages = _Messages(self)
        self.turns = _Turns(self)
        self.changesets = _Changesets(self)
        self.deliveries = _Deliveries(self)
        self.delegations = _Delegations(self)
        self.operations = _Operations(self)

    # -- transport ----------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> UnifiedClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self, idem: bool) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self.token:
            h["authorization"] = f"Bearer {self.token}"
        if idem:
            h["idempotency-key"] = f"sdk_{uuid.uuid4().hex[:20]}"
        return h

    def _req(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idem: bool = False,
        params: dict | None = None,
    ) -> Any:
        try:
            resp = self._http.request(
                method,
                self.base_url + path,
                headers=self._headers(idem),
                json=body,
                params={k: v for k, v in (params or {}).items() if v is not None},
            )
        except httpx.TransportError as exc:
            raise SbxTransportError(method, path, check=None, original=exc) from exc
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            err = data.get("error") or {}
            raise UnifiedApiError(
                resp.status_code,
                err.get("code"),
                err.get("message") or f"http {resp.status_code}",
                err.get("retry_after"),
                retryable=err.get("retryable"),
                action=err.get("action"),
                details={**(err.get("details") or {}), "_category": err.get("category")},
            )
        return data

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._req("GET", path, params=params)

    def _post(self, path: str, body: dict | None = None, *, idem: bool = True) -> Any:
        return self._req("POST", path, body=body, idem=idem)

    # -- auth ---------------------------------------------------------------

    def register(self, email: str, password: str, display_name: str | None = None) -> dict:
        return self._post(
            "/api/auth/register",
            {"email": email, "password": password, "display_name": display_name},
            idem=False,
        )

    def login(self, email: str, password: str) -> dict:
        out = self._post(
            "/api/auth/login",
            {"email": email, "password": password},
            idem=False,
        )
        self.token = out["token"]
        return out

    def me(self) -> dict:
        return self._get("/api/me")

    def list_models(self, workspace_id: str) -> dict:
        return self._get("/api/models", {"workspace_id": workspace_id})

    # -- high-level -----------------------------------------------------------

    def execute(
        self,
        prompt: str,
        *,
        workspace_id: str,
        session_id: str | None = None,
        project_version_id: str | None = None,
        repository: str | None = None,
        base_ref: str = "main",
        model: str | None = None,
        timeout_s: float = 600.0,
        poll_s: float = 2.0,
    ) -> dict:
        """Thin one-shot: create (or reuse) a session, send ``prompt``,
        wait for the turn to reach a terminal state, return the evidence
        dict ``{session_id, turn_id, turn_state, delivery?}``."""
        if session_id is None:
            if model is None:
                models = self.list_models(workspace_id)
                default = models.get("default_model") or {}
                items = models.get("items") or []
                model = default.get("model") or (
                    items[0].get("model") if items else "opencode/big-pickle"
                )
            created = self.sessions.create(
                workspace_id,
                harness={"provider_id": "opencode", "model": model},
                project_version_id=project_version_id,
                projectless_spec=(
                    {"repository": repository, "base_ref": base_ref} if repository else None
                ),
            )
            session_id = created["session"]["id"]
        accepted = self.messages.send(session_id, {"text": prompt})
        turn_id = accepted.get("turn_id")
        deadline = time.time() + timeout_s
        state = "queued"
        while time.time() < deadline and turn_id:
            turn = self.turns.get(turn_id)
            state = turn.get("state") or state
            if state in ("succeeded", "failed", "cancelled", "interrupted"):
                break
            time.sleep(poll_s)
        return {
            "session_id": session_id,
            "turn_id": turn_id,
            "turn_state": state,
        }


class _Ns:
    def __init__(self, c: UnifiedClient) -> None:
        self.c = c


class _Projects(_Ns):
    def list(self, workspace_id: str) -> dict:
        return self.c._get(f"/api/workspaces/{workspace_id}/projects")

    def create(self, workspace_id: str, slug: str, name: str, metadata: dict | None = None) -> dict:
        return self.c._post(
            f"/api/workspaces/{workspace_id}/projects",
            {"slug": slug, "name": name, "metadata": metadata},
        )

    def publish_version(
        self,
        project_id: str,
        repository: str | None = None,
        base_ref: str = "main",
        environment: dict | None = None,
        services: list | None = None,
        defaults: dict | None = None,
        ship_policy: dict | None = None,
    ) -> dict:
        return self.c._post(
            f"/api/projects/{project_id}/versions",
            {
                "repository": repository,
                "base_ref": base_ref,
                "environment": environment,
                "services": services,
                "defaults": defaults,
                "ship_policy": ship_policy,
            },
        )

    def get(self, project_id: str) -> dict:
        return self.c._get(f"/api/projects/{project_id}")


class _Connections(_Ns):
    """``add`` takes the credential as a dict or via protected input —
    never argv."""

    def list(self, workspace_id: str) -> dict:
        return self.c._get(f"/api/workspaces/{workspace_id}/connections")

    def add(
        self,
        workspace_id: str,
        kind: str,
        credential: dict,
        *,
        label: str | None = None,
        allowed_purposes: list[str] | None = None,
    ) -> dict:
        return self.c._post(
            f"/api/workspaces/{workspace_id}/connections",
            {
                "kind": kind,
                "label": label,
                "credential": credential,
                "allowed_purposes": allowed_purposes,
            },
        )

    def get(self, connection_id: str) -> dict:
        return self.c._get(f"/api/connections/{connection_id}")

    def capabilities(self, connection_id: str) -> dict:
        return self.c._get(f"/api/connections/{connection_id}/capabilities")

    def replace(
        self, connection_id: str, credential: dict, expected_version: int | None = None
    ) -> dict:
        return self.c._post(
            f"/api/connections/{connection_id}/credential-versions",
            {"credential": credential, "expected_version": expected_version},
        )

    def validate(self, connection_id: str) -> dict:
        return self.c._post(f"/api/connections/{connection_id}/validations", {})

    def disconnect(self, connection_id: str) -> dict:
        return self.c._req("DELETE", f"/api/connections/{connection_id}")


class _Sessions(_Ns):
    def list(
        self,
        workspace_id: str,
        lifecycle: str | None = None,
        role: str | None = None,
        parent_session_id: str | None = None,
    ) -> dict:
        return self.c._get(
            f"/api/workspaces/{workspace_id}/sessions",
            {
                "lifecycle": lifecycle,
                "role": role,
                "parent_session_id": parent_session_id,
            },
        )

    def create(
        self,
        workspace_id: str,
        *,
        harness: dict,
        title: str | None = None,
        role: str = "author",
        project_version_id: str | None = None,
        projectless_spec: dict | None = None,
        labels: list[str] | None = None,
        message: dict | None = None,
    ) -> dict:
        return self.c._post(
            f"/api/workspaces/{workspace_id}/sessions",
            {
                "title": title,
                "role": role,
                "labels": labels,
                "project_version_id": project_version_id,
                "projectless_spec": projectless_spec,
                "harness": harness,
                "message": message,
            },
        )

    def get(self, session_id: str) -> dict:
        return self.c._get(f"/api/sessions/{session_id}")

    def events(self, session_id: str, after_seq: int = 0, limit: int = 500) -> dict:
        return self.c._get(
            f"/api/sessions/{session_id}/events",
            {"after_seq": after_seq, "limit": limit},
        )

    def stream(
        self, session_id: str, *, poll_s: float = 2.0, timeout_s: float | None = None
    ) -> Iterable[dict]:
        """Committed-replay tail: yields new events as they land."""
        after = 0
        deadline = time.time() + timeout_s if timeout_s else None
        while True:
            batch = self.events(session_id, after_seq=after)
            for e in batch.get("items", []):
                after = e["seq"]
                yield e
            if deadline and time.time() > deadline:
                return
            time.sleep(poll_s)

    def cancel(self, session_id: str) -> dict:
        """Cancel the active turn (via turn.cancellations)."""
        detail = self.get(session_id)
        turn = (detail or {}).get("active_turn")
        if not turn:
            return {"turn_id": None, "cancelled": False}
        return self.c._post(f"/api/turns/{turn['id']}/cancellations", {})

    def close(self, session_id: str) -> dict:
        return self.c._post(f"/api/sessions/{session_id}/closures", {})

    def continue_(self, session_id: str, **overrides) -> dict:
        return self.c._post(f"/api/sessions/{session_id}/continuations", overrides)

    def export(self, session_id: str) -> dict:
        """Durable export: events + session + messages + turns snapshot."""
        detail = self.get(session_id)
        messages = self.c._get(f"/api/sessions/{session_id}/messages")
        turns = self.c._get(f"/api/sessions/{session_id}/turns")
        events = self.events(session_id, 0, 1000)
        return {
            "session": detail["session"],
            "messages": messages.get("items", []),
            "turns": turns.get("items", []),
            "events": events.get("items", []),
            "event_watermark": events.get("event_watermark"),
        }

    def files(self, session_id: str, path: str = ".") -> dict:
        return self.c._get(f"/api/sessions/{session_id}/files", {"path": path})

    def read_file(self, session_id: str, path: str) -> dict:
        return self.c._get(f"/api/sessions/{session_id}/files/content", {"path": path})


class _Messages(_Ns):
    def list(self, session_id: str, limit: int = 100) -> dict:
        return self.c._get(f"/api/sessions/{session_id}/messages", {"limit": limit})

    def send(self, session_id: str, content: dict, *, routing: str = "queue") -> dict:
        return self.c._post(
            f"/api/sessions/{session_id}/messages",
            {"routing": routing, "content": content},
        )


class _Turns(_Ns):
    def list(self, session_id: str) -> dict:
        return self.c._get(f"/api/sessions/{session_id}/turns")

    def get(self, turn_id: str) -> dict:
        return self.c._get(f"/api/turns/{turn_id}")

    def cancel(self, turn_id: str) -> dict:
        return self.c._post(f"/api/turns/{turn_id}/cancellations", {})


class _Changesets(_Ns):
    def list(self, session_id: str) -> dict:
        return self.c._get(f"/api/sessions/{session_id}/changesets")

    def get(self, changeset_id: str) -> dict:
        return self.c._get(f"/api/changesets/{changeset_id}")

    def files(self, changeset_id: str) -> dict:
        return self.c._get(f"/api/changesets/{changeset_id}/files")

    def capture(
        self, session_id: str, source_turn_id: str | None = None, origin: str = "explicit"
    ) -> dict:
        return self.c._post(
            f"/api/sessions/{session_id}/changes",
            {"source_turn_id": source_turn_id, "origin": origin},
        )

    def apply(self, changeset_id: str, session_id: str) -> dict:
        return self.c._post(
            f"/api/changesets/{changeset_id}/applications",
            {"session_id": session_id},
        )


class _Deliveries(_Ns):
    def get(self, delivery_id: str) -> dict:
        return self.c._get(f"/api/deliveries/{delivery_id}")

    def request(
        self,
        changeset_id: str,
        target: dict,
        *,
        transport: str = "pull_request",
        ship_policy: dict | None = None,
        connection_id: str | None = None,
    ) -> dict:
        return self.c._post(
            f"/api/changesets/{changeset_id}/deliveries",
            {
                "target": target,
                "transport": transport,
                "ship_policy": ship_policy,
                "connection_id": connection_id,
            },
        )

    def retry(self, delivery_id: str) -> dict:
        return self.c._post(f"/api/deliveries/{delivery_id}/retries", {})

    def merge(self, delivery_id: str, expected_head_sha: str, merge_method: str = "squash") -> dict:
        return self.c._post(
            f"/api/deliveries/{delivery_id}/merge-requests",
            {
                "expected_head_sha": expected_head_sha,
                "merge_method": merge_method,
            },
        )


class _Delegations(_Ns):
    def list(self, session_id: str) -> dict:
        return self.c._get(f"/api/sessions/{session_id}/delegations")

    def spawn(
        self,
        session_id: str,
        role: str,
        prompt: str,
        *,
        result_contract: dict | None = None,
        inputs: list | None = None,
        harness: dict | None = None,
        budget: dict | None = None,
    ) -> dict:
        return self.c._post(
            f"/api/sessions/{session_id}/delegations",
            {
                "role": role,
                "prompt": prompt,
                "result_contract": result_contract,
                "inputs": inputs,
                "harness": harness,
                "budget": budget,
            },
        )

    def get(self, delegation_id: str) -> dict:
        return self.c._get(f"/api/delegations/{delegation_id}")

    def result(self, delegation_id: str) -> dict:
        return self.c._get(f"/api/delegations/{delegation_id}/result")

    def wait(
        self,
        delegation_id: str,
        *,
        subscriber_session_id: str | None = None,
        predicate: dict | None = None,
        deadline_seconds: int = 1800,
    ) -> dict:
        return self.c._post(
            f"/api/delegations/{delegation_id}/waits",
            {
                "subscriber_session_id": subscriber_session_id,
                "predicate": predicate,
                "deadline_seconds": deadline_seconds,
            },
        )

    def cancel(self, delegation_id: str) -> dict:
        return self.c._post(f"/api/delegations/{delegation_id}/cancellations", {})


class _Operations(_Ns):
    def get(self, operation_id: str) -> dict:
        return self.c._get(f"/api/operations/{operation_id}")

    def job(self, job_id: str) -> dict:
        return self.c._get(f"/api/jobs/{job_id}")
