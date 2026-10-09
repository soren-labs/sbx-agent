"""Connection kind -> connector module."""

from __future__ import annotations

from types import ModuleType

from control.integrations.connectors import codex, github, inference_api, modal, opencode_zen

CONNECTORS: dict[str, ModuleType] = {
    "modal": modal,
    "github": github,
    "inference_api": inference_api,
    # Retired kinds: kept so stored Connections still decrypt, validate and disconnect.
    "opencode_zen": opencode_zen,
    "codex": codex,
}
