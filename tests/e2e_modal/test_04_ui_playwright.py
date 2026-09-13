"""Playwright against the real control plane via local UI proxy."""

from __future__ import annotations

import os
import subprocess
import time

import httpx

from tests.e2e_modal.helpers import REPO_ROOT, artifacts_dir, close_active_sessions
from tests.e2e_modal.serve_ui import start_background


def test_ui_two_turns_then_readonly(client: httpx.Client, control_url: str) -> None:
    close_active_sessions(client)
    os.environ["SBX_CONTROL_URL"] = control_url
    server, thread, base = start_background()
    env = os.environ.copy()
    env["SBX_UI_ORIGIN"] = base
    env["SBX_CONTROL_URL"] = control_url
    env["SBX_UI_TITLE"] = f"wp2h-ui-{int(time.time())}"
    art = artifacts_dir()
    env["SBX_UI_ARTIFACTS"] = str(art)
    try:
        proc = subprocess.run(
            [
                "npx",
                "--prefix",
                str(REPO_ROOT / "web"),
                "playwright",
                "test",
                "--config",
                str(REPO_ROOT / "tests" / "e2e_modal" / "playwright.config.ts"),
            ],
            cwd=REPO_ROOT / "web",
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        log_path = art / "playwright.log"
        # Do not write env secrets; stdout of playwright should be clean.
        log_path.write_text(
            (proc.stdout or "") + "\n" + (proc.stderr or ""),
            encoding="utf-8",
        )
        assert proc.returncode == 0, f"playwright failed (rc={proc.returncode}); see artifacts"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        close_active_sessions(client)
