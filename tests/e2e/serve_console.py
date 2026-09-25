#!/usr/bin/env python3
"""Serve the web console against a real, cloud-free /v1 control plane.

``control.app.create_app()`` with ``SBX_BACKEND=local``: every provider CLI is
a fake from ``tests/fakes`` (no Modal, no provider credentials), durable
stores live in a throwaway state dir, and ``web/`` is mounted at ``/``.
Used by ``make console-dev`` and the Playwright ``console`` project.

The fake CLI scenario is picked per run from prompt keywords so every UI
state is reachable by typing:

- ``hang``  → never finishes (exercise Cancel)
- ``slow``  → first event, then a few seconds of silence
- ``fail``  → CLI exits non-zero (run ``ERROR``)
- ``auth``  → ``auth_invalid``
- anything else → success
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
FAKES = REPO_ROOT / "tests" / "fakes"

PROVIDERS = ("codex", "devin", "antigravity", "grok", "opencode")
# provider → (binary name on PATH, fake script, scenario env, slow-seconds env)
FAKE_BINS = {
    "codex": ("codex", "fake_codex.py", "FAKE_CODEX_SCENARIO", "FAKE_CODEX_SLOW_SECONDS"),
    "devin": ("devin", "fake_devin.py", "FAKE_DEVIN_SCENARIO", "FAKE_DEVIN_SLOW_SECONDS"),
    "antigravity": ("agy", "fake_agy.py", "FAKE_AGY_SCENARIO", "FAKE_AGY_SLOW_SECONDS"),
    "grok": ("grok", "fake_grok.py", "FAKE_GROK_SCENARIO", "FAKE_GROK_SLOW_SECONDS"),
    "opencode": (
        "opencode",
        "fake_opencode.py",
        "FAKE_OPENCODE_SCENARIO",
        "FAKE_OPENCODE_SLOW_SECONDS",
    ),
}

_CLOUD_PREFIXES = ("MODAL_", "OPENAI_", "CODEX_API", "GH_", "GITHUB_", "SBX_GITHUB_")

WRAPPER = """#!{python}
import os, sys
argv = sys.argv[1:]
text = " ".join(argv).lower()
scenario = "success"
if {resume_check}:
    scenario = "resume"
KEYWORDS = (("hang", "hang"), ("slow", "slow"), ("fail", "nonzero"), ("auth", "auth_invalid"))
for keyword, name in KEYWORDS:
    if keyword in text:
        scenario = name
        break
os.environ[{scenario_env!r}] = scenario
os.environ.setdefault({slow_env!r}, "4")
os.execv({python!r}, [{python!r}, {fake!r}, *argv])
"""


def _strip_cloud_env() -> None:
    for key in list(os.environ):
        if key.startswith(_CLOUD_PREFIXES):
            os.environ.pop(key, None)


def _write_fake_bins(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for provider, (name, script, scenario_env, slow_env) in FAKE_BINS.items():
        # Only codex models resume as a distinct fixture (`codex exec resume`).
        resume_check = '"resume" in argv' if provider == "codex" else "False"
        path = bin_dir / name
        path.write_text(
            WRAPPER.format(
                python=sys.executable,
                fake=str(FAKES / script),
                scenario_env=scenario_env,
                slow_env=slow_env,
                resume_check=resume_check,
            ),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(cwd),
        "GIT_AUTHOR_NAME": "sbx demo",
        "GIT_AUTHOR_EMAIL": "demo@example.invalid",
        "GIT_COMMITTER_NAME": "sbx demo",
        "GIT_COMMITTER_EMAIL": "demo@example.invalid",
    }
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _make_demo_repo(root: Path) -> dict[str, str]:
    """A local repo agents can declare as their workspace (`repo` = path)."""
    work = root / "demo-repo"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q", "-b", "main")
    (work / "README.md").write_text("# demo\n\nA tiny repo for the local console.\n")
    (work / "app.py").write_text('def greet(name):\n    return f"hello {name}"\n')
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "initial commit")
    return {"repo": str(work), "base_ref": "main", "base_sha": _git(work, "rev-parse", "HEAD")}


def prepare_env(state_dir: Path, api_key: str) -> dict[str, str]:
    _strip_cloud_env()
    bin_dir = state_dir / "bin"
    _write_fake_bins(bin_dir)
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ["SBX_BACKEND"] = "local"
    os.environ["SBX_PROVIDERS"] = ",".join(PROVIDERS)
    os.environ["SBX_V1_BOOTSTRAP_KEY"] = api_key
    # Room for one live agent per provider plus a few extras.
    os.environ.setdefault("SBX_MAX_CONCURRENT", "8")
    os.environ["XDG_STATE_HOME"] = str(state_dir / "state")
    os.environ["SBX_ACCOUNT_STORE_DIR"] = str(state_dir / "state" / "accounts")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    os.environ["PYTHONPATH"] = str(REPO_ROOT)
    os.environ["CODEX_BIN"] = str(bin_dir / "codex")
    os.environ["OPENCODE_BIN"] = str(bin_dir / "opencode")
    for _name, _script, scenario_env, _slow in FAKE_BINS.values():
        os.environ.pop(scenario_env, None)
    return _make_demo_repo(state_dir)


def _verify_seeded_accounts(app) -> None:
    """Mark seeded accounts as cloud-verify-passed (SOR-216).

    Bootstrap seeds ``unverified`` accounts under the verified-only
    lifecycle — nothing schedules until a probe proves the credential.
    This fixture's providers are fakes that always authenticate, so every
    seeded account is treated as already verified: ``note_verified`` +
    ``active``, the same state a passed ``/v1/accounts/{id}/verify``
    produces.
    """
    from control.credlifecycle import CredentialLifecycleService

    registry = getattr(app.state, "account_registry", None)
    if registry is None:
        return
    lifecycle = CredentialLifecycleService(registry)
    for account in registry.list():
        if account.status != "unverified":
            continue
        lifecycle.note_verified(account.id, probe="e2e-console")
        registry.mark_status(account.id, "active")


def _fake_rsa_pem() -> str:
    """A throwaway RSA private key for the fake GitHub API — never a real
    credential; generated per serve so nothing secret lands in the repo."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


_FAKE_GH_INSTALLATION = {
    "id": 88,
    "account": {"login": "e2e-org", "type": "Organization"},
    "repository_selection": "selected",
}
_FAKE_GH_REPOS = {
    "repository_selection": "selected",
    "total_count": 1,
    "repositories": [{"full_name": "e2e-org/hello"}],
}


def build_app(demo: dict[str, str], base: str):
    from control.app import create_app

    # SOR-220 e2e seam: a local fake of the GitHub API endpoints the App
    # flow exercises — manifest conversion, installations, installation
    # tokens. ``SBX_GITHUB_APP_API_URL`` points the control plane's client
    # at it; nothing here validates JWTs (the service signs a real one
    # from the registered PEM).
    os.environ["SBX_GITHUB_APP_API_URL"] = f"{base}/__fake_gh"
    pem = _fake_rsa_pem()

    # ``create_app`` already mounts ``web/`` at "/" (SOR-211 same-origin).
    app = create_app()
    _verify_seeded_accounts(app)

    # Dev-only fixture info for tests and humans (never mounted in production).
    @app.get("/__dev/info", include_in_schema=False)
    def dev_info() -> JSONResponse:
        return JSONResponse({"demo_workspace": demo, "providers": list(PROVIDERS)})

    @app.post("/__fake_gh/settings/apps/new", include_in_schema=False)
    async def fake_apps_new(request: Request, state: str = "") -> Response:
        """Simulate GitHub's App-manifest landing page: the console POSTs
        the manifest form here; GitHub would create the App then redirect
        the browser to ``redirect_url`` with ``?code=&state=``."""
        redirect = ""
        try:
            manifest = json.loads(
                parse_qs((await request.body()).decode()).get("manifest", ["{}"])[0]
            )
            redirect = str(manifest.get("redirect_url") or "")
        except (ValueError, TypeError, IndexError):
            pass
        if not redirect:
            return JSONResponse({"error": "bad manifest"}, status_code=400)
        sep = "&" if "?" in redirect else "?"
        return RedirectResponse(f"{redirect}{sep}code=e2e-conv-code&state={state}", status_code=303)

    @app.post("/__fake_gh/app-manifests/{code}/conversions", include_in_schema=False)
    def fake_manifest_conversion(code: str) -> JSONResponse:
        return JSONResponse(
            {
                "id": 7770001,
                "slug": "sbx-e2e-app",
                "client_id": "Iv1.e2efake",
                "client_secret": "e2e-fake-client-secret",
                "pem": pem,
                "webhook_secret": "e2e-fake-webhook-secret",
                "name": "sbx-e2e-app",
                "html_url": f"{base}/apps/sbx-e2e-app",
            },
            status_code=201,
        )

    @app.get("/__fake_gh/app/installations", include_in_schema=False)
    def fake_installations() -> JSONResponse:
        return JSONResponse([dict(_FAKE_GH_INSTALLATION)])

    @app.get("/__fake_gh/installation/repositories", include_in_schema=False)
    def fake_installation_repos() -> JSONResponse:
        return JSONResponse(dict(_FAKE_GH_REPOS))

    @app.post(
        "/__fake_gh/app/installations/{installation_id}/access_tokens", include_in_schema=False
    )
    def fake_access_token(installation_id: int) -> JSONResponse:
        from datetime import UTC, datetime, timedelta

        return JSONResponse(
            {
                "token": "ghs_e2e_fake_token",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
            status_code=201,
        )

    @app.delete("/__fake_gh/app/installations/{installation_id}", include_in_schema=False)
    def fake_delete_installation(installation_id: int) -> Response:
        return Response(status_code=204)

    # The "/" console mount is a terminal catch-all, so dev/fake routes
    # appended above would be shadowed — hoist them ahead of the mount.
    routes = app.router.routes
    ours = [r for r in routes if getattr(r, "path", "").startswith(("/__dev/", "/__fake_gh/"))]
    for route in ours:
        routes.remove(route)
    for route in reversed(ours):
        routes.insert(0, route)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="web console + local /v1 control plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--state-dir", help="keep durable state here (default: temp dir)")
    args = parser.parse_args()

    temp = None
    if args.state_dir:
        state_dir = Path(args.state_dir).resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp = tempfile.mkdtemp(prefix="sbx-console-")
        state_dir = Path(temp)
    # A fresh random key per run unless the caller pins one (Playwright does).
    api_key = os.environ.get("SBX_CONSOLE_DEV_KEY") or f"sbx_dev_{secrets.token_hex(12)}"
    demo = prepare_env(state_dir, api_key)
    os.chdir(REPO_ROOT)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    print(
        json.dumps(
            {
                "console": f"http://{args.host}:{args.port}/",
                "api_key": api_key,
                "state_dir": str(state_dir),
                "demo_workspace": demo,
            },
            indent=2,
        ),
        flush=True,
    )

    import uvicorn

    try:
        uvicorn.run(
            build_app(demo, f"http://{args.host}:{args.port}"),
            host=args.host,
            port=args.port,
            log_level="warning",
        )
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
