from protocol.capabilities import NAMES, Capability, HarnessManifest
from protocol.runtime import ProtocolError

from runtime.harnesses.opencode import OpenCodeHarness


def catalog():
    enabled = OpenCodeHarness().describe().wire()
    disabled = [
        HarnessManifest(
            provider,
            "1",
            "unverified",
            "unverified",
            transport,
            "disabled",
            {n: Capability() for n in NAMES},
        ).wire()
        for provider, transport in [
            ("codex", "jsonl"),
            ("claude", "jsonl"),
            ("devin", "acp"),
            ("grok", "jsonl"),
            ("antigravity", "jsonl"),
        ]
    ]
    return [enabled, *disabled]


def get_harness(provider, **kwargs):
    if provider == "opencode":
        return OpenCodeHarness(**kwargs)
    raise ProtocolError("unsupported_capability")
