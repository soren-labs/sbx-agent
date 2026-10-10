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

# Inference is bring-your-own-key and Harness-neutral: one Connection kind carries an
# API key plus one base URL per wire protocol the provider speaks. A Harness declares
# the protocols its official CLI can be pointed at (manifest ``inference_protocols``,
# in preference order); the first one the Connection offers is used.
INFERENCE_KIND = "inference_api"
INFERENCE_PROTOCOLS = ("openai_chat", "openai_responses", "anthropic_messages")
# Retired vendor-specific kinds. Stored Connections and the Sessions pinned to them keep
# working with their original Harness; they cannot be created or newly selected.
LEGACY_INFERENCE_KIND = {"opencode": "opencode_zen", "codex": "codex"}


def select_endpoint(endpoints: object, accepted: object) -> tuple[str, str] | None:
    """First accepted protocol the Connection offers, as ``(protocol, base_url)``."""
    if not isinstance(endpoints, dict):
        return None
    for protocol in accepted if isinstance(accepted, list | tuple) else ():
        base_url = endpoints.get(protocol)
        if isinstance(base_url, str) and base_url:
            return protocol, base_url
    return None


# Official CLI distributions pinned for executor images: provider -> (npm package, version).
# Data shared by the image recipe (control) and the adapters that were verified against it.
HARNESS_CLI_PACKAGES: dict[str, tuple[str, str]] = {
    "opencode": ("opencode-ai", "1.18.35"),
    "codex": ("@openai/codex", "0.162.0"),
    "claude": ("@anthropic-ai/claude-code", "2.1.295"),
    "grok": ("@xai-official/grok", "1.0.50"),
    "commandcode": ("command-code", "1.79.2"),
}

# Custom-API reasoning control verified end to end with the official CLI: Harness ->
# wire protocols on which the single value ``none`` (thinking off) reaches the endpoint.
# A model still needs its Connection's probe to show the control takes effect.
REASONING_OFF = "none"
REASONING_TOGGLE_PROTOCOLS: dict[str, tuple[str, ...]] = {
    "opencode": ("openai_chat", "anthropic_messages"),
}
