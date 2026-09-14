"""Worker /v1 passthrough behavior, executed via node (deploy/sbx-edge/worker.js)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WORKER = ROOT / "deploy" / "sbx-edge" / "worker.js"
SCRIPT = Path(__file__).with_name("worker_test.mjs")

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@node
def test_worker_v1_passthrough() -> None:
    proc = subprocess.run(
        ["node", str(SCRIPT), str(WORKER)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "passed" in proc.stdout
