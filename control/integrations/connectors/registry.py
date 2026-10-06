"""Connection kind -> connector module."""

from __future__ import annotations

from types import ModuleType

from control.integrations.connectors import codex, github, modal, opencode_zen

CONNECTORS: dict[str, ModuleType] = {
    "modal": modal,
    "github": github,
    "opencode_zen": opencode_zen,
    "codex": codex,
}
