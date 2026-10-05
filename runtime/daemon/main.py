"""sbx-runtime daemon entrypoint.

Runs inside an executor sandbox (or a local subprocess for the local
backend). Connects outbound to the control-plane ingress with a one-use,
lease-bound enrollment token.

    python -m runtime.daemon.main \
        --lease-id lease_... --lease-generation 1 \
        --connect tcp://127.0.0.1:9000 \
        --worktree /work/worktree --state /state \
        --image-digest sha256:...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .app import DaemonApp
from .auth import enrollment_token_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sbx-runtime")
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--lease-generation", type=int, required=True)
    parser.add_argument("--connect", required=True, help="ingress endpoint tcp://|wss://")
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--image-digest", default=None)
    parser.add_argument("--grant-expires-at", type=float, default=None)
    args = parser.parse_args(argv)

    token = enrollment_token_from_env()
    app = DaemonApp(
        state_dir=args.state,
        worktree_root=args.worktree,
        lease_id=args.lease_id,
        lease_generation=args.lease_generation,
        enrollment_token=token,
        grant_expires_at=args.grant_expires_at,
        endpoint=args.connect,
        image_digest=args.image_digest,
    )
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
