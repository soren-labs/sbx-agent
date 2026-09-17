"""Deterministic fakes for the sbx bootstrap tests (SOR-98).

``FakePlane`` implements the ``sbx.plane.Plane`` Protocol in memory —
secrets/dicts/apps/sandboxes with per-step failure injection. ``make_v1``
returns an ``httpx.MockTransport`` that speaks just enough of the frozen
``/v1`` contract (``docs/contracts/api-v1.yaml``) for doctor/smoke/status.
No Modal credentials, no network.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from sbx.config import BootstrapConfig, ResolvedConfig, load, save
from sbx.errors import BootstrapError
from sbx.plane import SandboxInfo


class FakePlane:
    """In-memory ``Plane``. ``fail_on`` step names raise BootstrapError."""

    def __init__(self, workspace: str | None = "ws-test") -> None:
        self.workspace_name = workspace
        self.secrets: dict[str, dict[str, str]] = {}
        self.dicts: dict[str, dict[str, Any]] = {}
        self.apps: dict[str, str] = {}
        self.sandboxes: dict[str, SandboxInfo] = {}
        self.fail_on: set[str] = set()
        self.deploy_calls = 0
        self.secret_create_calls = 0
        self.dict_create_calls = 0
        self.image_calls: list[str] = []
        self.image_names: dict[str, str | None] = {}
        self.deploy_env: dict[str, str] = {}
        self.terminated: list[str] = []
        self.terminate_noop = False

    def _fail(self, step: str) -> None:
        if step in self.fail_on:
            raise BootstrapError(f"injected failure at {step}", code="fake_fail")

    def workspace(self) -> str | None:
        self._fail("workspace")
        return self.workspace_name

    def list_secret_names(self) -> set[str]:
        self._fail("list_secrets")
        return set(self.secrets)

    def ensure_secret(self, name: str, env: dict[str, str]) -> bool:
        self._fail("ensure_secret")
        self.secret_create_calls += 1
        if name in self.secrets:
            return False
        self.secrets[name] = dict(env)
        return True

    def delete_secret(self, name: str) -> bool:
        self._fail("delete_secret")
        return self.secrets.pop(name, None) is not None

    def ensure_dict(self, name: str) -> bool:
        self._fail("ensure_dict")
        self.dict_create_calls += 1
        if name in self.dicts:
            return False
        self.dicts[name] = {}
        return True

    def has_dict(self, name: str) -> bool:
        self._fail("has_dict")
        return name in self.dicts

    def dict_len(self, name: str) -> int:
        self._fail("dict_len")
        if name not in self.dicts:
            raise KeyError(name)
        return len(self.dicts[name])

    def dict_items(self, name: str) -> list[tuple[object, object]]:
        self._fail("dict_items")
        if name not in self.dicts:
            raise KeyError(name)
        return list(self.dicts[name].items())

    def delete_dict(self, name: str) -> bool:
        self._fail("delete_dict")
        return self.dicts.pop(name, None) is not None

    def ensure_image(self, provider: str, name: str | None = None) -> None:
        self._fail("ensure_image")
        self.image_calls.append(provider)
        self.image_names[provider] = name

    def deploy_app(self, app_name: str, *, env: Mapping[str, str] | None = None) -> str:
        self._fail("deploy_app")
        self.deploy_calls += 1
        self.deploy_env = dict(env or {})
        url = f"https://{self.workspace_name}--{app_name}-fastapi-app.modal.run"
        self.apps[app_name] = url
        return url

    def app_url(self, app_name: str) -> str | None:
        return self.apps.get(app_name)

    def stop_app(self, app_name: str) -> bool:
        self._fail("stop_app")
        return self.apps.pop(app_name, None) is not None

    def list_sandboxes(self, app_name: str) -> list[SandboxInfo]:
        self._fail("list_sandboxes")
        return list(self.sandboxes.values())

    def terminate_sandbox(self, sandbox_id: str) -> None:
        self.terminated.append(sandbox_id)
        if not self.terminate_noop:
            self.sandboxes.pop(sandbox_id, None)


def make_v1(
    *,
    token: str | None = None,
    run_statuses: list[str] | None = None,
    run_error: dict[str, Any] | None = None,
    create_error: tuple[int, str, str] | None = None,
    unreachable: bool = False,
    models: list[dict[str, Any]] | None = None,
    agents: list[dict[str, Any]] | None = None,
    agents_page_size: int = 100,
) -> tuple[httpx.MockTransport, dict[str, Any]]:
    """A ``/v1`` transport + observable state (agents, deletions, requests).

    ``token=None`` accepts any well-formed ``Bearer sbx_*`` credential —
    the right default for deploy/upgrade paths that mint their own key.
    Pass an explicit token to test rejection (401) paths.
    """
    statuses = list(run_statuses or ["FINISHED"])
    state: dict[str, Any] = {"agents": {}, "deleted": [], "seq": 0}

    def authed(request: httpx.Request) -> bool:
        header = request.headers.get("Authorization") or ""
        if token is None:
            return header.startswith("Bearer sbx_")
        return header == f"Bearer {token}"

    def err(status: int, code: str, message: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": message}})

    def handler(request: httpx.Request) -> httpx.Response:
        if unreachable:
            raise httpx.ConnectError("no route to host", request=request)
        path = request.url.path
        if path == "/v1/me":
            if not authed(request):
                return err(401, "unauthorized", "missing or invalid bearer token")
            return httpx.Response(
                200, json={"key_id": "key_test", "label": "t", "scopes": ["agents", "admin"]}
            )
        if not authed(request):
            return err(401, "unauthorized", "missing or invalid bearer token")
        if path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": models
                    if models is not None
                    else [{"provider": "codex", "model": "gpt-5.6-luna", "accounts_available": 1}]
                },
            )
        if path == "/v1/agents" and request.method == "GET":
            all_agents = agents if agents is not None else list(state["agents"].values())
            try:
                start = max(0, int(request.url.params.get("cursor") or 0))
            except ValueError:
                start = 0
            page = all_agents[start : start + agents_page_size]
            next_cursor = (
                str(start + agents_page_size)
                if start + agents_page_size < len(all_agents)
                else None
            )
            return httpx.Response(200, json={"agents": page, "next_cursor": next_cursor})
        if path == "/v1/agents" and request.method == "POST":
            if create_error is not None:
                status, code, message = create_error
                return err(status, code, message)
            state["seq"] += 1
            agent_id = f"agt_{state['seq']}"
            run_id = "run-1"
            state["agents"][agent_id] = {"id": agent_id, "status": "running"}
            return httpx.Response(
                201,
                json={
                    "agent": {"id": agent_id, "status": "running"},
                    "run": {"id": run_id, "agent_id": agent_id, "status": "RUNNING"},
                },
            )
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["v1", "agents"] and request.method == "DELETE":
            agent_id = parts[2]
            state["deleted"].append(agent_id)
            state["agents"].pop(agent_id, None)
            return httpx.Response(200, json={"id": agent_id, "status": "closed"})
        if (
            len(parts) == 5
            and parts[:2] == ["v1", "agents"]
            and parts[3] == "runs"
            and request.method == "GET"
        ):
            agent_id, run_id = parts[2], parts[4]
            if agent_id not in state["agents"]:
                return err(404, "not_found", "agent not found")
            status = statuses.pop(0) if statuses else "FINISHED"
            body: dict[str, Any] = {
                "id": run_id,
                "agent_id": agent_id,
                "status": status,
                "error": run_error,
            }
            return httpx.Response(200, json=body)
        return err(404, "not_found", f"no fake route for {request.method} {path}")

    return httpx.MockTransport(handler), state


def make_cfg(
    tmp_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    config: BootstrapConfig | None = None,
    write: bool = True,
) -> ResolvedConfig:
    """ResolvedConfig rooted at ``tmp_path/config.toml`` (+ optional file)."""
    path = tmp_path / "config.toml"
    if write:
        save(config or BootstrapConfig(), path)
    return load(path, env=dict(env or {}))


def make_env(tmp_path: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "SBX_CONFIG": str(tmp_path / "config.toml"),
        "SBX_STATE_DIR": str(tmp_path / "state"),
    }
    env.update(extra or {})
    return env


def write_state(tmp_path: Path, payload: dict[str, Any]) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "deploy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
