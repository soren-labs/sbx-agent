"""Installed Harness registry and manifest data export."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from runtime.harnesses import claude, codex, commandcode, grok, opencode
from runtime.harnesses.claude import ClaudeHarness
from runtime.harnesses.codex import CodexHarness
from runtime.harnesses.commandcode import CommandCodeHarness
from runtime.harnesses.disabled import disabled_manifests
from runtime.harnesses.grok import GrokHarness
from runtime.harnesses.opencode import OpenCodeHarness
from runtime.harnesses.protocol import Harness


def _version(argv: list[str]) -> str:
    try:
        out = subprocess.run(
            [*argv, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/tmp"},
        )
        text = (out.stdout or out.stderr).strip().splitlines()
        return text[-1][:80] if out.returncode == 0 and text else "not_installed"
    except (OSError, subprocess.TimeoutExpired):
        return "not_installed"


def installed(probe: bool = True) -> dict[str, Harness]:
    def version(module: Any) -> str:
        return _version(module._bin()) if probe else "unverified"

    return {
        "opencode": OpenCodeHarness(version(opencode)),
        "codex": CodexHarness(version(codex)),
        "claude": ClaudeHarness(version(claude)),
        "grok": GrokHarness(version(grok)),
        "commandcode": CommandCodeHarness(version(commandcode)),
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
