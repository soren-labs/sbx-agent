"""Entrypoint: ``python -m runtime.daemon.main --state-dir ... --work-dir ... --port ...``.

A backend may use this bounded boot command solely to start the daemon.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from runtime.daemon.app import RuntimeDaemon
from runtime.harnesses.registry import installed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sbx-runtime")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file")
    args = parser.parse_args(argv)
    key_hex = os.environ.pop("SBX_RUNTIME_KEY", "")
    if not key_hex:
        print("SBX_RUNTIME_KEY is required", file=sys.stderr)
        return 2
    daemon = RuntimeDaemon(
        state_dir=Path(args.state_dir),
        work_dir=Path(args.work_dir),
        key=bytes.fromhex(key_hex),
        lease_id=os.environ["SBX_LEASE_ID"],
        generation=int(os.environ.get("SBX_LEASE_GENERATION", "1")),
        image_digest=os.environ.get("SBX_IMAGE_DIGEST", "unknown"),
        harnesses=installed(),
        lease_ttl=float(os.environ.get("SBX_LEASE_TTL", "1800")),
        max_unacked=int(os.environ.get("SBX_SPOOL_MAX_UNACKED", "20000")),
    )
    server = daemon.serve(args.host, args.port)
    if args.port_file:
        tmp = Path(args.port_file + ".tmp")
        tmp.write_text(str(server.server_address[1]))
        tmp.replace(args.port_file)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
