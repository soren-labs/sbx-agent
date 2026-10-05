from dataclasses import asdict, dataclass

NAMES = (
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


@dataclass(frozen=True)
class Capability:
    status: str = "unknown"
    evidence: str = "not_verified"
    limitation: str | None = None


@dataclass(frozen=True)
class HarnessManifest:
    provider_id: str
    adapter_version: str
    cli_version: str
    distribution_digest: str
    transport: str
    support_tier: str
    capabilities: dict[str, Capability]

    def wire(self):
        return asdict(self)
