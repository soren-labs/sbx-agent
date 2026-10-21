"""Client-side diagnosis of one SBX control plane: ``SBXClient.diagnose()`` / ``sbx status``.

Each finding has one category so callers can tell failures apart:

* ``reachability``: no HTTP answer, not an SBX control plane, or the control plane
  cannot read its database (``/readyz`` 503);
* ``authentication``: no API key, or the server rejected it;
* ``executor``: the deployment has no executor backend this workspace can use.

Only read endpoints are called; nothing is created or started.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from sbx.sdk.errors import SBXError

if TYPE_CHECKING:
    from sbx.sdk.client import SBXClient

CATEGORIES = ("config", "reachability", "authentication", "executor")
# Exit status of `sbx status` (and `sbx server check|start|status`) per first failing category.
EXIT_CODES = {"config": 3, "reachability": 4, "authentication": 5, "executor": 6}


def _check(name: str, category: str, status: str, message: str, action: str | None = None):
    found = {"name": name, "category": category, "status": status, "message": message}
    if action:
        found["action"] = action
    return found


def _report(base_url: str, checks: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    failing = [c for c in checks if c["status"] == "fail"]
    problem = min(failing, key=lambda c: CATEGORIES.index(c["category"])) if failing else None
    return {
        "ok": problem is None,
        "base_url": base_url,
        **extra,
        "problem": problem,
        "checks": checks,
    }


def exit_code(report: dict[str, Any]) -> int:
    problem = report.get("problem")
    return 0 if not problem else EXIT_CODES[problem["category"]]


def diagnose(client: SBXClient) -> dict[str, Any]:
    base = client.base_url
    checks: list[dict[str, Any]] = []
    start = "start it with `sbx server start`, or check --base-url / SBX_BASE_URL"
    try:
        health = client.http.get("/healthz", timeout=10)
    except httpx.HTTPError as exc:
        checks.append(
            _check(
                "server",
                "reachability",
                "fail",
                f"cannot reach SBX at {base} ({type(exc).__name__})",
                start,
            )
        )
        return _report(base, checks)
    body: dict[str, Any] = {}
    try:
        body = health.json()
    except ValueError:
        pass
    if health.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
        checks.append(
            _check(
                "server",
                "reachability",
                "fail",
                f"{base} answered HTTP {health.status_code} but is not an SBX control plane",
                "check --base-url / SBX_BASE_URL",
            )
        )
        return _report(base, checks)
    git_sha = body.get("git_sha")
    checks.append(_check("server", "reachability", "ok", f"SBX control plane at {base}"))
    try:
        ready = client.http.get("/readyz", timeout=10).status_code == 200
    except httpx.HTTPError:
        ready = False
    if not ready:
        checks.append(
            _check(
                "database",
                "reachability",
                "fail",
                "the control plane answers but cannot read its database (/readyz)",
                "on the server host, `sbx server check` shows the database problem",
            )
        )
        return _report(base, checks, git_sha=git_sha)
    checks.append(_check("database", "reachability", "ok", "ready"))

    if not client.api_key and not client.csrf:
        checks.append(
            _check(
                "api_key",
                "authentication",
                "fail",
                "no API key is configured",
                "sign in with `sbx auth login --email EMAIL` (or set SBX_API_KEY)",
            )
        )
        return _report(base, checks, git_sha=git_sha)
    try:
        me = client.me()
    except SBXError as exc:
        if exc.code == "transport_error":
            checks.append(_check("api_key", "reachability", "fail", exc.message, exc.action))
        elif exc.status in (401, 403):
            checks.append(
                _check(
                    "api_key",
                    "authentication",
                    "fail",
                    "the server rejected the API key (revoked, expired, or issued by another "
                    "server)",
                    "sign in again with `sbx auth login --email EMAIL`",
                )
            )
        else:
            checks.append(
                _check("api_key", "reachability", "fail", f"{exc.code}: {exc.message}", exc.action)
            )
        return _report(base, checks, git_sha=git_sha)
    who = (me.get("user") or {}).get("email") if isinstance(me, dict) else None
    checks.append(_check("api_key", "authentication", "ok", f"signed in as {who or 'API key'}"))

    try:
        backends = [b["kind"] for b in client.get("/api/executor-backends")["items"]]
        connections = client.connections.list() if "modal" in backends else []
    except SBXError as exc:
        category = "authentication" if exc.status in (401, 403) else "reachability"
        checks.append(_check("executors", category, "fail", f"{exc.code}: {exc.message}"))
        return _report(base, checks, git_sha=git_sha)
    usable: list[str] = []
    if "local" in backends:
        usable.append("local")
    if "modal" in backends:
        ready_modal = [
            c for c in connections if c.get("kind") == "modal" and c.get("health") == "ready"
        ]
        if ready_modal:
            usable.append("modal")
        else:
            checks.append(
                _check(
                    "executor_modal",
                    "executor",
                    "warn",
                    "Modal is enabled but this workspace has no ready Modal Connection",
                    "add one with `sbx connections add modal` (credential JSON on stdin)",
                )
            )
    if usable:
        checks.append(_check("executors", "executor", "ok", "usable: " + ", ".join(usable)))
    else:
        checks.append(
            _check(
                "executors",
                "executor",
                "fail",
                "no executor backend can run Turns for this workspace"
                + ("" if backends else " (the deployment enables none)"),
                "add a Modal Connection, or ask the operator to enable one in SBX_EXECUTORS",
            )
        )
    return _report(base, checks, git_sha=git_sha, executor_backends=backends)
