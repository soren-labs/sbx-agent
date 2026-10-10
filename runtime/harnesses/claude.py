"""Claude Code official CLI Harness (``claude -p --output-format stream-json``).

``--include-partial-messages`` relays the provider's own stream events, so reply text,
visible reasoning and tool calls are observed while they are generated (verified in
print mode with 2.1.295; the CLI requires ``stream-json`` and ``--verbose`` for it).

Inference is bring-your-own-key over the Anthropic Messages protocol only:
``ANTHROPIC_BASE_URL`` and ``ANTHROPIC_API_KEY`` point the CLI at the Turn's endpoint
(verified against @anthropic-ai/claude-code 2.1.295 with DeepSeek's Anthropic-compatible
API). Claude subscription login is never used: ``CLAUDE_CONFIG_DIR`` is an isolated
per-Session directory and no credential file is written.

Transcripts live in ``CLAUDE_CONFIG_DIR/projects/<cwd slug>/<session>.jsonl``; resume
(``--resume <id>``) therefore needs the same worktree path, which the runtime keeps
stable for a Session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.harnesses import messages_stream
from runtime.harnesses.protocol import (
    Capability,
    HarnessError,
    HarnessManifest,
    HarnessOutcome,
    NativeInvocation,
    PreparedHarness,
    TurnContext,
    argv_text,
    base_env,
    cli_bin,
    resolve_inference,
)
from runtime.security.credentials import private_dir, scrub

ADAPTER_VERSION = "claude-harness/2"
PROTOCOLS = ("anthropic_messages",)
CONFIG_REL = Path(".claude")


def _bin() -> list[str]:
    return cli_bin("CLAUDE_BIN", "claude")


class ClaudeHarness:
    provider_id = "claude"

    def __init__(self, cli_version: str = "unverified") -> None:
        self.cli_version = cli_version

    def describe(self) -> HarnessManifest:
        return HarnessManifest(
            provider_id="claude",
            adapter_version=ADAPTER_VERSION,
            cli_version=self.cli_version,
            distribution="npm:@anthropic-ai/claude-code",
            transport="jsonl",
            support_tier="supported",
            credential_methods=["inference_api key via ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY"],
            inference_protocols=list(PROTOCOLS),
            native_state_versions=["claude-projects-jsonl-1"],
            capabilities={
                "native_resume": Capability("supported", "claude -p --resume <session_id>"),
                "native_state_export": Capability("supported", "CLAUDE_CONFIG_DIR/projects"),
                "account_portable_resume": Capability(
                    "unknown", "", "not verified across inference connections"
                ),
                "event_stream": Capability(
                    "supported",
                    "--output-format stream-json --verbose --include-partial-messages",
                ),
                "interrupt": Capability("unsupported", "", "supervisor process-group stop only"),
                "steer": Capability("unsupported", "", "print mode has no injection channel"),
                "interactive_approval": Capability(
                    "unsupported", "", "--dangerously-skip-permissions approves"
                ),
                "mcp": Capability("unknown", "", "--mcp-config not wired"),
                "skills": Capability("unknown"),
                "attachments": Capability("unknown", "", "not wired"),
                "structured_output": Capability("unsupported", "", "prompt-only", "prompt_only"),
                "model_discovery": Capability(
                    "unsupported", "", "models come from the inference connection catalog"
                ),
                "effort_settings": Capability("unknown", "", "not wired"),
                "credential_writeback": Capability("unsupported", "", "static API key"),
                "usage": Capability("supported", "result.usage"),
            },
        )

    def prepare(self, context: TurnContext, credentials: dict[str, Any]) -> PreparedHarness:
        home = private_dir(context.home)
        config_dir = private_dir(home / CONFIG_REL)
        inference = resolve_inference(context, credentials, PROTOCOLS)
        env = base_env(
            home,
            CLAUDE_CONFIG_DIR=str(config_dir),
            ANTHROPIC_BASE_URL=inference.base_url,
            ANTHROPIC_API_KEY=inference.api_key,
            ANTHROPIC_MODEL=inference.model,
            # Background/small-model calls must also go to the user's endpoint and model.
            ANTHROPIC_SMALL_FAST_MODEL=inference.model,
            ANTHROPIC_DEFAULT_HAIKU_MODEL=inference.model,
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            DISABLE_AUTOUPDATER="1",
            # The default ten backoff retries would hold a Turn for minutes on a bad key.
            CLAUDE_CODE_MAX_RETRIES="3",
            # The executor is the sandbox; the CLI otherwise refuses bypass mode as root.
            IS_SANDBOX="1",
        )
        return PreparedHarness(
            home=home, env=env, secrets=[inference.api_key], model=inference.model
        )

    def _argv(
        self, context: TurnContext, prepared: PreparedHarness, session: str | None
    ) -> list[str]:
        argv = [
            *_bin(),
            "-p",
            argv_text(context.prompt),
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model",
            prepared.model or context.model or "",
            "--dangerously-skip-permissions",
        ]
        return argv + (["--resume", session] if session else [])

    def start_turn(self, context: TurnContext, prepared: PreparedHarness) -> NativeInvocation:
        return NativeInvocation(self._argv(context, prepared, None), context.worktree, prepared.env)

    def resume_turn(
        self, context: TurnContext, prepared: PreparedHarness, binding: dict[str, Any]
    ) -> NativeInvocation:
        native_id = binding.get("native_id")
        if not native_id or binding.get("provider_id") != "claude":
            raise HarnessError("context_unavailable", "no compatible Claude Code session")
        return NativeInvocation(
            self._argv(context, prepared, native_id), context.worktree, prepared.env
        )

    def new_state(self, context: TurnContext) -> dict[str, Any]:
        return messages_stream.new_state(context)

    def normalize(self, line: str, state: dict[str, Any]) -> list[dict[str, Any]]:
        return messages_stream.normalize("claude", line, state)

    def finish(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return messages_stream.finish("claude.result", state)

    def classify_outcome(self, evidence: dict[str, Any], state: dict[str, Any]) -> HarnessOutcome:
        return messages_stream.classify(evidence, state)

    def discover(self, prepared: PreparedHarness) -> NativeInvocation | None:
        return None

    def native_state_paths(self, home: Path) -> list[Path]:
        projects = home / CONFIG_REL / "projects"
        return [projects] if projects.exists() else []

    def release(self, prepared: PreparedHarness) -> list[str]:
        return scrub(prepared.credential_files)
