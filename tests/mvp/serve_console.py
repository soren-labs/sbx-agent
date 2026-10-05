"""Serve the unified API + console locally for manual exploration.

``python -m tests.mvp.serve_console --port 8790`` builds the full unified
composition (same wiring as the MVP harness) against a disposable Postgres
database and serves ``control.api.app`` via uvicorn. No cloud credentials are
required for the server itself — connections are validated on demand through
the product surface.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from tests.mvp.harness import build_world


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--dsn", default=os.environ.get("SBX_TEST_DATABASE_URL"))
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("SBX_TEST_DATABASE_URL (disposable Postgres DSN) required")

    import uvicorn

    world = build_world(args.dsn, Path(tempfile.mkdtemp(prefix="sbx-console-")))
    uvicorn.run(world["app"], host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
