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


def build_app(demo: dict[str, str]):
    from control.app import create_app
    from fastapi.responses import JSONResponse

    # ``create_app`` already mounts ``web/`` at "/" (SOR-211 same-origin).
    app = create_app()
    _verify_seeded_accounts(app)

    # Dev-only fixture info for tests and humans (never mounted in production).
    @app.get("/__dev/info", include_in_schema=False)
    def dev_info() -> JSONResponse:
        return JSONResponse({"demo_workspace": demo, "providers": list(PROVIDERS)})

    # The "/" console mount is a terminal catch-all, so the route appended
    # above would be shadowed — hoist it ahead of the mount.
    routes = app.router.routes
    idx = next(i for i, r in enumerate(routes) if getattr(r, "path", "") == "/__dev/info")
    routes.insert(0, routes.pop(idx))
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
        uvicorn.run(build_app(demo), host=args.host, port=args.port, log_level="warning")
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
