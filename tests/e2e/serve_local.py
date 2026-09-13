#!/usr/bin/env python3
"""Serve real sbx-control + web/ for Playwright (WP2-G / SOR-41).

Starts FastAPI from ``control.app.create_app`` with ``SBX_BACKEND=local`` and
mounts ``web/`` at ``/``. Does not import or connect to Modal.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
SCENARIO_CODEX = Path(__file__).resolve().parent / "scenario_codex.py"

_CLOUD_PREFIXES = ("MODAL_", "OPENAI_", "CODEX_API")
_CLOUD_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "MODAL_TOKEN",
    }
)


def _strip_cloud_env() -> None:
    for key in list(os.environ):
        if key in _CLOUD_KEYS or key.startswith(_CLOUD_PREFIXES):
            os.environ.pop(key, None)


def prepare_env() -> None:
    _strip_cloud_env()
    os.environ["SBX_BACKEND"] = "local"
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    os.environ.setdefault("PYTHONPATH", str(REPO_ROOT))
    os.environ.setdefault("CODEX_BIN", str(SCENARIO_CODEX))
    # Wrapper selects success / resume / hang from the prompt; do not pin a
    # global scenario on the control process.
    os.environ.pop("FAKE_CODEX_SCENARIO", None)


def build_app():
    from control.app import create_app
    from fastapi.staticfiles import StaticFiles

    app = create_app()
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="sbx-control + web/ (local backend)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    prepare_env()
    os.chdir(REPO_ROOT)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    import uvicorn

    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
