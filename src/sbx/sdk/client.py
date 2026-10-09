"""SBXClient: projects, connections, sessions, messages, turns, changesets, deliveries,
delegations and operations namespaces plus ``execute``.

``execute`` only creates/continues ordinary resources and follows committed
events until the accepted Turn is terminal; there is no local agent loop, and
process exit is never treated as terminal authority.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from sbx.sdk.errors import DeadlineExceeded, OutcomeUnknown, SBXError
from sbx.sdk.events import parse_sse

TERMINAL_TURN = ("succeeded", "failed", "cancelled", "interrupted")


class _Namespace:
    def __init__(self, client: SBXClient) -> None:
        self.c = client


class SBXClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8800",
        api_key: str | None = None,
        *,
        http: httpx.Client | None = None,
        timeout: float = 60.0,
        workspace_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(base_url=self.base_url, timeout=timeout)
        self.api_key = api_key
        self.csrf: str | None = None
        self._workspace_id = workspace_id
        self.projects = Projects(self)
        self.connections = Connections(self)
        self.sessions = Sessions(self)
        self.messages = Messages(self)
        self.turns = Turns(self)
        self.changesets = ChangeSets(self)
        self.deliveries = Deliveries(self)
        self.delegations = Delegations(self)
        self.operations = Operations(self)

    # ------------------------------------------------------------------ transport
    def _headers(self, mutation: bool, key: str | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif mutation and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        if mutation:
            headers["Idempotency-Key"] = key or uuid.uuid4().hex
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        retries: int = 3,
    ) -> Any:
        mutation = method.upper() not in ("GET", "HEAD")
        key = idempotency_key or (uuid.uuid4().hex if mutation else None)
        for attempt in range(retries + 1):
            try:
                response = self.http.request(
                    method, path, json=json, params=params, headers=self._headers(mutation, key)
                )
            except httpx.TransportError as exc:
                # Network uncertainty: retry with the SAME Idempotency-Key.
                if attempt == retries:
                    raise SBXError("transport_error", type(exc).__name__, retryable=True) from None
                time.sleep(0.2 * (2**attempt))
                continue
            if response.status_code == 204:
                return None
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code >= 400:
                err = body.get("error") if isinstance(body, dict) else None
                err = err or {"code": "http_error", "message": response.text[:200]}
                if response.status_code >= 500 and attempt < retries and mutation:
                    time.sleep(0.2 * (2**attempt))
                    continue
                raise SBXError(
                    err.get("code", "http_error"),
                    err.get("message", ""),
                    status=response.status_code,
                    details=err.get("details"),
                    retryable=bool(err.get("retryable")),
                    request_id=err.get("request_id"),
                    action=err.get("action"),
                )
            return body
        raise AssertionError("unreachable")

    def get(self, path: str, **params: Any) -> Any:
        return self.request(
            "GET", path, params={k: v for k, v in params.items() if v is not None} or None
        )

    def post(
        self, path: str, body: dict[str, Any] | None = None, *, idempotency_key: str | None = None
    ) -> Any:
        return self.request("POST", path, json=body or {}, idempotency_key=idempotency_key)

    # ------------------------------------------------------------------- identity
    def login(self, email: str, password: str) -> dict[str, Any]:
        body = self.request("POST", "/api/auth/login", json={"email": email, "password": password})
        self.csrf = body.get("csrf_token")
        return body

    def register(self, email: str, password: str) -> dict[str, Any]:
        return self.request(
            "POST", "/api/auth/register", json={"email": email, "password": password}
        )

    def verify_email(self, token: str) -> dict[str, Any]:
        return self.request("POST", "/api/auth/email-verifications", json={"token": token})

    def me(self) -> dict[str, Any]:
        return self.get("/api/me")

    def create_api_key(self, name: str) -> dict[str, Any]:
        return self.post("/api/api-keys", {"name": name})

    @property
    def workspace_id(self) -> str:
        if self._workspace_id is None:
            self._workspace_id = self.me()["workspaces"][0]["id"]
        return self._workspace_id

    def models(self, provider_id: str = "opencode") -> dict[str, Any]:
        """Models offered by the workspace's inference Connections, and whether each
        Connection speaks a protocol the given Harness accepts."""
        return self.get("/api/models", workspace_id=self.workspace_id, provider_id=provider_id)

    def harnesses(self) -> list[dict[str, Any]]:
        """Harness manifests: support tier, capabilities and accepted inference protocols."""
        return self.get("/api/harnesses")["items"]

    # -------------------------------------------------------------------- execute
    def execute(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        project_id: str | None = None,
        project_version_id: str | None = None,
        harness: dict[str, Any] | None = None,
        executor: dict[str, Any] | None = None,
        repository: dict[str, Any] | None = None,
        deadline: float = 1800.0,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if session_id:
            accepted = self.sessions.send(session_id, prompt)
            watermark = accepted.get("event_watermark", 0)
        else:
            body: dict[str, Any] = {"message": {"content": prompt}}
            for key, value in (
                ("project_id", project_id),
                ("project_version_id", project_version_id),
                ("harness", harness),
                ("executor", executor),
                ("repository", repository),
            ):
                if value is not None:
                    body[key] = value
            accepted = self.sessions.create(**body)
            session_id = accepted["session_id"]
            watermark = 0
        turn_id = accepted["turn_id"]
        for event in self.sessions.events(
            session_id, after=max(0, int(watermark) - 50), follow=True, deadline=deadline
        ):
            if on_event:
                on_event(event)
            if (
                event.get("turn_id") == turn_id
                and event["type"].removeprefix("turn.") in TERMINAL_TURN
            ):
                break
        turn = self.turns.get(turn_id)
        if turn["reason"] == "outcome_unknown":
            raise OutcomeUnknown(
                "outcome_unknown",
                "the Turn outcome could not be proven",
                details={"turn_id": turn_id},
            )
        return {"session_id": session_id, "turn": turn}


class Projects(_Namespace):
    def list(self) -> list[dict[str, Any]]:
        return self.c.get(f"/api/workspaces/{self.c.workspace_id}/projects")["items"]

    def create(self, slug: str, spec: dict[str, Any], name: str | None = None) -> dict[str, Any]:
        return self.c.post(
            f"/api/workspaces/{self.c.workspace_id}/projects",
            {"slug": slug, "name": name or slug, "spec": spec},
        )

    def get(self, project_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/projects/{project_id}")

    def publish(
        self, project_id: str, spec: dict[str, Any], expected_version: int
    ) -> dict[str, Any]:
        return self.c.post(
            f"/api/projects/{project_id}/versions",
            {"spec": spec, "expected_version": expected_version},
        )


class Connections(_Namespace):
    def list(self) -> list[dict[str, Any]]:
        return self.c.get(f"/api/workspaces/{self.c.workspace_id}/connections")["items"]

    def add(
        self, kind: str, credential: dict[str, Any], label: str | None = None
    ) -> dict[str, Any]:
        return self.c.post(
            f"/api/workspaces/{self.c.workspace_id}/connections",
            {"kind": kind, "label": label or kind, "credential": credential},
        )

    def add_inference(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str | None = None,
        protocol: str = "openai_chat",
        endpoints: dict[str, str] | None = None,
        models: list[str] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Add a bring-your-own-key inference Connection.

        Give one ``base_url`` + ``protocol``, or ``endpoints`` mapping each protocol the
        provider speaks (``openai_chat``, ``openai_responses``, ``anthropic_messages``)
        to its base URL so every Harness can use the same key.
        """
        credential: dict[str, Any] = {"api_key": api_key, "model": model}
        if endpoints:
            credential["endpoints"] = endpoints
        else:
            credential.update({"base_url": base_url, "protocol": protocol})
        if models:
            credential["models"] = models
        return self.add("inference_api", credential, label or "inference")

    def get(self, connection_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/connections/{connection_id}")

    def replace(
        self, connection_id: str, credential: dict[str, Any], expected_version: int
    ) -> dict[str, Any]:
        return self.c.post(
            f"/api/connections/{connection_id}/credential-versions",
            {"credential": credential, "expected_version": expected_version},
        )

    def validate(self, connection_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/connections/{connection_id}/validations")

    def disconnect(self, connection_id: str) -> dict[str, Any]:
        return self.c.request("DELETE", f"/api/connections/{connection_id}")

    def wait_health(self, connection_id: str, *, deadline: float = 120.0) -> dict[str, Any]:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            view = self.get(connection_id)
            if view["health"] not in ("unverified", "verifying"):
                return view
            time.sleep(0.5)
        raise DeadlineExceeded("deadline_exceeded", "connection validation did not settle")


class Sessions(_Namespace):
    def create(self, **body: Any) -> dict[str, Any]:
        return self.c.post(f"/api/workspaces/{self.c.workspace_id}/sessions", body)

    def list(self, **filters: Any) -> dict[str, Any]:
        return self.c.get(f"/api/workspaces/{self.c.workspace_id}/sessions", **filters)

    def get(self, session_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/sessions/{session_id}")

    def send(
        self,
        session_id: str,
        content: str,
        *,
        routing: str = "queue",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.c.post(
            f"/api/sessions/{session_id}/messages",
            {"content": content, "routing": routing},
            idempotency_key=idempotency_key,
        )

    def close(self, session_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/sessions/{session_id}/closures")

    def archive(self, session_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/sessions/{session_id}/archives")

    def executor(self, session_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/sessions/{session_id}/executor")

    def release(self, session_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/sessions/{session_id}/executor/releases")

    def activate(self, session_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/sessions/{session_id}/executor/activations")

    def export(self, session_id: str) -> dict[str, Any]:
        return {
            "session": self.get(session_id)["session"],
            "messages": self.c.messages.list(session_id),
            "events": list(self.events(session_id, follow=False)),
        }

    def events(
        self,
        session_id: str,
        *,
        after: int = 0,
        follow: bool = False,
        deadline: float = 600.0,
        poll: float = 0.5,
    ) -> Iterator[dict[str, Any]]:
        """Committed journal replay by sequence; follow polls until the deadline."""
        end = time.monotonic() + deadline
        position = after
        while True:
            page = self.c.get(f"/api/sessions/{session_id}/events", after=position, limit=500)
            yield from page["items"]
            advanced = page["next_after"] > position
            position = page["next_after"]
            if not follow and not advanced:
                return
            if not advanced:
                if time.monotonic() > end:
                    raise DeadlineExceeded(
                        "deadline_exceeded",
                        "event stream deadline reached",
                        details={"after": position},
                    )
                time.sleep(poll)

    def stream(
        self, session_id: str, *, after: int = 0, max_seconds: float = 60
    ) -> Iterator[dict[str, Any]]:
        with self.c.http.stream(
            "GET",
            f"/api/sessions/{session_id}/events",
            params={"after": after, "max_seconds": max_seconds},
            headers={"Accept": "text/event-stream", **self.c._headers(False, None)},
        ) as response:
            yield from parse_sse(response.iter_lines())


class Messages(_Namespace):
    def list(self, session_id: str) -> list[dict[str, Any]]:
        return self.c.get(f"/api/sessions/{session_id}/messages")["items"]


class Turns(_Namespace):
    def list(self, session_id: str) -> list[dict[str, Any]]:
        return self.c.get(f"/api/sessions/{session_id}/turns")["items"]

    def get(self, turn_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/turns/{turn_id}")["turn"]

    def cancel(self, turn_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/turns/{turn_id}/cancellations")

    def retry(self, turn_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/turns/{turn_id}/retries")

    def acknowledge(self, turn_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/turns/{turn_id}/acknowledgements")

    def wait(self, turn_id: str, *, deadline: float = 1800.0, poll: float = 1.0) -> dict[str, Any]:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            turn = self.get(turn_id)
            if turn["state"] in TERMINAL_TURN:
                if turn["reason"] == "outcome_unknown":
                    raise OutcomeUnknown(
                        "outcome_unknown",
                        "outcome could not be proven",
                        details={"turn_id": turn_id},
                    )
                return turn
            time.sleep(poll)
        raise DeadlineExceeded(
            "deadline_exceeded", "Turn did not reach a terminal state", details={"turn_id": turn_id}
        )


class ChangeSets(_Namespace):
    def list(self, session_id: str) -> list[dict[str, Any]]:
        return self.c.get(f"/api/sessions/{session_id}/changesets")["items"]

    def capture(
        self, session_id: str, *, origin: str = "explicit", source_turn_id: str | None = None
    ) -> dict[str, Any]:
        return self.c.post(
            f"/api/sessions/{session_id}/changesets",
            {"origin": origin, "source_turn_id": source_turn_id},
        )["changeset"]

    def get(self, changeset_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/changesets/{changeset_id}")

    def diff(self, changeset_id: str) -> str:
        return self.c.get(f"/api/changesets/{changeset_id}/diff")["diff"]

    def apply(self, changeset_id: str, destination_session_id: str) -> dict[str, Any]:
        return self.c.post(
            f"/api/changesets/{changeset_id}/applications",
            {"destination_session_id": destination_session_id},
        )

    def wait_ready(
        self, session_id: str, *, source_turn_id: str | None = None, deadline: float = 300.0
    ) -> dict[str, Any]:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            for cs in self.list(session_id):
                if source_turn_id in (None, cs["source_turn_id"]) and cs["state"] in (
                    "ready",
                    "failed",
                ):
                    return cs
            time.sleep(1.0)
        raise DeadlineExceeded("deadline_exceeded", "ChangeSet capture did not settle")


class Deliveries(_Namespace):
    def request(self, changeset_id: str, **body: Any) -> dict[str, Any]:
        return self.c.post(f"/api/changesets/{changeset_id}/deliveries", body)["delivery"]

    def get(self, delivery_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/deliveries/{delivery_id}")

    def retry(self, delivery_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/deliveries/{delivery_id}/retries")

    def refresh(self, delivery_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/deliveries/{delivery_id}/refreshes")

    def merge(
        self, delivery_id: str, *, method: str = "squash", mark_ready: bool = False
    ) -> dict[str, Any]:
        """Send exactly the server's subject/head/version pins; the server revalidates."""
        view = self.get(delivery_id)
        pins = {
            "expected_head_sha": view["commit_sha"],
            "subject_digest": view["subject_digest"],
            "expected_version": view["version"],
            "method": method,
            "mark_ready": mark_ready,
        }
        return self.c.post(f"/api/deliveries/{delivery_id}/merge-requests", pins)

    def wait(self, delivery_id: str, *, deadline: float = 600.0) -> dict[str, Any]:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            view = self.get(delivery_id)
            if view["state"] in ("succeeded", "failed", "blocked", "cancelled"):
                return view
            time.sleep(1.0)
        raise DeadlineExceeded("deadline_exceeded", "Delivery did not settle")


class Delegations(_Namespace):
    def spawn(self, session_id: str, role: str, **body: Any) -> dict[str, Any]:
        return self.c.post(f"/api/sessions/{session_id}/delegations", {"role": role, **body})

    def get(self, delegation_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/delegations/{delegation_id}")

    def result(self, delegation_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/delegations/{delegation_id}/result")

    def cancel(self, delegation_id: str) -> dict[str, Any]:
        return self.c.post(f"/api/delegations/{delegation_id}/cancellations")

    def wait_result(self, delegation_id: str, *, deadline: float = 1800.0) -> dict[str, Any]:
        """Waiting for a validated DelegationResult is distinct from waiting for a Turn."""
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            view = self.get(delegation_id)
            if view["state"] in ("succeeded", "failed", "cancelled"):
                return view
            time.sleep(1.0)
        raise DeadlineExceeded("deadline_exceeded", "Delegation result not published in time")


class Operations(_Namespace):
    def get(self, operation_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/operations/{operation_id}")
