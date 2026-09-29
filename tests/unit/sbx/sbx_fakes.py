"""Deterministic fakes for the sbx bootstrap tests (SOR-98).

``FakePlane`` implements the ``sbx.plane.Plane`` Protocol in memory —
secrets/dicts/apps/sandboxes with per-step failure injection. ``make_v1``
returns an ``httpx.MockTransport`` that speaks just enough of the frozen
``/v1`` contract (``docs/contracts/api-v1.yaml``) for doctor/smoke/status.
No Modal credentials, no network.
"""

from __future__ import annotations

import hashlib
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
        self.image_specs: dict[str, Any] = {}
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

    def dict_put(self, name: str, key: str, value: object) -> None:
        self._fail("dict_put")
        self.dicts.setdefault(name, {})[key] = value

    def delete_dict(self, name: str) -> bool:
        self._fail("delete_dict")
        return self.dicts.pop(name, None) is not None

    def ensure_image(self, provider: str, name: str | None = None, *, spec: Any = None) -> None:
        self._fail("ensure_image")
        self.image_calls.append(provider)
        self.image_names[provider] = name
        self.image_specs[provider] = spec

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


def provider_row(
    provider: str,
    *,
    runtime_status: str = "ready",
    runtime_enabled: bool = True,
    connection_status: str = "connected",
    accounts_total: int = 1,
    accounts_available: int = 1,
    detail: str = "",
) -> dict[str, Any]:
    """A ``/v1/providers`` catalog row (SOR-212/SOR-221 shape)."""
    return {
        "provider": provider,
        "status": "available",
        "runtime": {
            "enabled": runtime_enabled,
            "image": f"sbx-runtime-{provider}",
            "status": runtime_status,
            "version": "1.0.0" if runtime_status == "ready" else None,
            "detail": detail,
            "updated_at": None,
        },
        "connection": {
            "status": connection_status,
            "accounts_total": accounts_total,
            "accounts_available": accounts_available,
        },
    }


FAKE_GIT_SHA = "deadbeef1234deadbeef1234deadbeef1234dead"
FAKE_CONSOLE_INDEX = (
    "<html><head><title>Session Console</title>"
    f'<meta name="sbx-build-sha" content="{FAKE_GIT_SHA}"></head>'
    '<body><div id="root"></div></body></html>'
)
FAKE_CONSOLE_PRIMARY = b"/* fake console bundle */\n"
FAKE_CONSOLE_MANIFEST: dict[str, Any] = {
    "schema": "sbx-console-build/1",
    "frontend_source": "console/",
    "git_sha": FAKE_GIT_SHA,
    "api_mode": "http",
    "built_at": "2026-09-29T00:00:00Z",
    "primary_asset": {
        "path": "assets/index-deadbeef.js",
        "sha256": hashlib.sha256(FAKE_CONSOLE_PRIMARY).hexdigest(),
        "bytes": len(FAKE_CONSOLE_PRIMARY),
    },
    "files": [
        {
            "path": "index.html",
            "sha256": hashlib.sha256(FAKE_CONSOLE_INDEX.encode()).hexdigest(),
            "bytes": len(FAKE_CONSOLE_INDEX),
        },
        {
            "path": "assets/index-deadbeef.js",
            "sha256": hashlib.sha256(FAKE_CONSOLE_PRIMARY).hexdigest(),
            "bytes": len(FAKE_CONSOLE_PRIMARY),
        },
    ],
}


def make_console_dist(root: Path, *, git_sha: str | None = None) -> Path:
    """Write a minimal console/dist fixture (index + hashed asset + manifest)."""
    manifest = dict(FAKE_CONSOLE_MANIFEST)
    if git_sha:
        manifest["git_sha"] = git_sha
    dist = root / "console-dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(FAKE_CONSOLE_INDEX, encoding="utf-8")
    (dist / "assets" / "index-deadbeef.js").write_bytes(FAKE_CONSOLE_PRIMARY)
    (dist / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return dist


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
    providers: list[dict[str, Any]] | None = None,
    console: bool = True,
    console_manifest: dict[str, Any] | None = None,
    accounts: list[dict[str, Any]] | None = None,
    verify_status: str = "active",
) -> tuple[httpx.MockTransport, dict[str, Any]]:
    """A ``/v1`` transport + observable state (agents, deletions, requests).

    ``token=None`` accepts any well-formed ``Bearer sbx_*`` credential —
    the right default for deploy/upgrade paths that mint their own key.
    Pass an explicit token to test rejection (401) paths.

    ``providers=None`` models a pre-catalog (pre-SOR-221) deployment —
    ``/v1/providers`` and ``/v1/accounts`` 404. ``console=False`` makes
    ``GET /`` 404 as on a pre-SOR-211 deployment.
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
        if path == "/":
            # The same-origin Console shell (SOR-211/SOR-266) —
            # unauthenticated. Serves the React app's ``id="root"`` shell.
            if not console:
                return err(404, "not_found", "no console")
            return httpx.Response(
                200,
                text=FAKE_CONSOLE_INDEX,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if path == "/build-manifest.json":
            if not console:
                return err(404, "not_found", "no console")
            return httpx.Response(
                200,
                json=console_manifest if console_manifest is not None else FAKE_CONSOLE_MANIFEST,
            )
        if path == "/assets/index-deadbeef.js":
            if not console:
                return err(404, "not_found", "no console")
            return httpx.Response(
                200, content=FAKE_CONSOLE_PRIMARY, headers={"content-type": "text/javascript"}
            )
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
        if path == "/v1/console/grant" and request.method == "POST":
            return httpx.Response(
                201,
                json={"grant": "sbxg_test_ticket", "expires_in": 120, "expires_at": 9e12},
            )
        if path == "/v1/providers" and request.method == "GET":
            if providers is None:
                return err(404, "not_found", "no provider catalog")
            return httpx.Response(200, json={"providers": providers})
        if path == "/v1/accounts" and request.method == "GET":
            if accounts is None:
                return err(404, "not_found", "no accounts surface")
            return httpx.Response(200, json={"accounts": accounts})
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
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "accounts"]
            and parts[3] == "verify"
            and request.method == "POST"
        ):
            if accounts is None:
                return err(404, "not_found", "no accounts surface")
            account_id = parts[2]
            for account in accounts:
                if account.get("id") == account_id:
                    return httpx.Response(200, json={**account, "status": verify_status})
            return err(404, "not_found", "account not found")
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
        # SOR-266: deploy builds the console via npm by default; tests pin
        # the prebuilt-dist lane so no test shell-outs to node.
        "SBX_CONSOLE_DIST": str(make_console_dist(tmp_path)),
        # Deterministic deploy SHA matching the fixture manifest/index meta.
        "SBX_GIT_SHA": FAKE_GIT_SHA,
    }
    env.update(extra or {})
    return env


def write_state(tmp_path: Path, payload: dict[str, Any]) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "deploy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
