"""Providers present in the codebase whose install/auth/native-resume gates have not
passed. They are registered honestly as ``disabled`` (RFC 03, 11 release matrix)."""

from __future__ import annotations

from protocol.capabilities import CAPABILITY_NAMES

from runtime.harnesses.protocol import Capability, HarnessManifest

_DISABLED = {
    "claude": ("npm:@anthropic-ai/claude-code", "jsonl", "distribution/auth not verified"),
    "devin": ("static.devin.ai", "acp", "proprietary distribution; ACP resume not verified"),
    "grok": ("host binary", "jsonl", "host-binary distribution not reproducible"),
    "antigravity": ("host binary", "jsonl", "host-binary distribution not reproducible"),
}


def disabled_manifests() -> list[HarnessManifest]:
    out = []
    for provider, (distribution, transport, why) in _DISABLED.items():
        out.append(
            HarnessManifest(
                provider_id=provider,
                adapter_version="none",
                cli_version="unverified",
                distribution=distribution,
                transport=transport,
                support_tier="disabled",
                capabilities={name: Capability("unknown", "", why) for name in CAPABILITY_NAMES},
            )
        )
    return out
