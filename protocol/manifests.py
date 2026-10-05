from protocol.capabilities import NAMES, Capability, HarnessManifest


def opencode_manifest(cli_version, distribution_digest):
    caps = {name: Capability() for name in NAMES}
    if cli_version != "1.18.29":
        return HarnessManifest(
            "opencode",
            "1",
            cli_version,
            distribution_digest,
            "jsonl",
            "disabled",
            caps,
        )
    for name in ("native_resume", "event_stream", "usage", "model_discovery"):
        caps[name] = Capability("supported", "opencode-v1.18.29-run.ts")
    for name in (
        "steer",
        "interactive_approval",
        "credential_writeback",
        "account_portable_resume",
    ):
        caps[name] = Capability("unsupported", "noninteractive-run-static-key")
    caps["structured_output"] = Capability("unsupported", "platform-validation", "prompt-only")
    caps["native_state_export"] = Capability("supported", "sqlite-after-process-stop")
    caps["interrupt"] = Capability("supported", "supervisor-process-group", "process-stop only")
    return HarnessManifest(
        "opencode",
        "1",
        cli_version,
        distribution_digest,
        "jsonl",
        "experimental",
        caps,
    )


OPENCODE_DIGEST = (
    "sha512-syIDVwlrYTgTOXzZe9SkInJWethbq6l3SNC762UeXyO0a9V0wGfd+"
    "U4yACvppwNBnhIsl0j2QPYYCyLpNaSomg=="
)


def installed_catalog():
    enabled = opencode_manifest("1.18.29", OPENCODE_DIGEST).wire()
    disabled = [
        HarnessManifest(
            provider,
            "1",
            "unverified",
            "unverified",
            transport,
            "disabled",
            {name: Capability() for name in NAMES},
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
