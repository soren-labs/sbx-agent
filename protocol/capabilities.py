"""Harness capability vocabulary (RFC 03 HarnessManifest)."""

from __future__ import annotations

CAPABILITY_NAMES = (
    "native_resume",
    "native_state_export",
    "account_portable_resume",
    "event_stream",
    "interrupt",
    "steer",
    "interactive_approval",
    "mcp",
    "skills",
    "attachments",
    "structured_output",
    "model_discovery",
    "effort_settings",
    "credential_writeback",
    "usage",
)
STATUSES = ("supported", "unsupported", "unknown")
SUPPORT_TIERS = ("supported", "experimental", "disabled")
TRANSPORTS = ("jsonl", "acp", "official_server", "text")

# Harness provider -> Connection kind supplying its inference credential.
INFERENCE_CONNECTION_KIND = {"opencode": "opencode_zen", "codex": "codex"}
