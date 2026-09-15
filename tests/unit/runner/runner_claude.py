#!/usr/bin/env python3
"""Test-only ``python -m runtime.runner`` entry with ``claude`` registered.

SOR-97: ``claude`` is not in the frozen provider set, so production
``runner init --provider claude`` is unreachable by contract. This shim
performs exactly the SOR-96-style registration (registry entry + provider
choice) in-process and then delegates to the stock CLI — it is the seam
the frozen ``adapter.py`` diff would make permanent once a real gate
passes. Used only by ``test_claude_turn.py`` subprocesses.
"""

from __future__ import annotations

import importlib

from runtime.runner.adapter import _REGISTRY
from runtime.runner.adapters.claude import ClaudeAdapter

runner_main = importlib.import_module("runtime.runner.main")

_REGISTRY["claude"] = ClaudeAdapter
runner_main.PROVIDERS = (*runner_main.PROVIDERS, "claude")

if __name__ == "__main__":
    runner_main.main()
