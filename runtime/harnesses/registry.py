"""Installed Harness registry and manifest data export."""

from __future__ import annotations

import subprocess
from typing import Any

from runtime.harnesses.codex import CodexHarness
from runtime.harnesses.codex import _bin as codex_bin
from runtime.harnesses.disabled import disabled_manifests
from runtime.harnesses.opencode import OpenCodeHarness
from runtime.harnesses.opencode import _bin as opencode_bin
from runtime.harnesses.protocol import Harness


def _version(argv: list[str]) -> str:
    try:
        out = subprocess.run(
            [*argv, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
        )
        text = (out.stdout or out.stderr).strip().splitlines()
        return text[-1][:80] if out.returncode == 0 and text else "not_installed"
    except (OSError, subprocess.TimeoutExpired):
        return "not_installed"


def installed(probe: bool = True) -> dict[str, Harness]:
    return {
        "opencode": OpenCodeHarness(_version(opencode_bin()) if probe else "unverified"),
        "codex": CodexHarness(_version(codex_bin()) if probe else "unverified"),
    }


def manifest_data(harnesses: dict[str, Harness] | None = None) -> list[dict[str, Any]]:
    """Manifests as data. Control consumes this, never adapter execution code."""
    harnesses = harnesses if harnesses is not None else installed(probe=False)
    manifests = [h.describe().to_dict() for h in harnesses.values()]
    return manifests + [m.to_dict() for m in disabled_manifests()]


if __name__ == "__main__":  # pragma: no cover - regenerates the catalog data file
    import json
    import sys

    json.dump(manifest_data(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
