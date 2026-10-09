"""Grok Build official CLI Harness (``grok -p --output-format streaming-messages-json``).

Inference is bring-your-own-key: the Turn's endpoint is a ``[model.sbx]`` block in
``~/.grok/config.toml`` whose ``env_key`` names the variable holding the key, so no
credential reaches disk and no xAI login is involved. Grok Build 1.0.50 sends custom
models to ``{base_url}/chat/completions`` (OpenAI Chat Completions), verified with
DeepSeek.

Sessions live in ``~/.grok/sessions/<url-encoded cwd>/<session id>/``; resume
(``-r <id>``) therefore needs the same worktree path, which the runtime keeps stable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runtime.harnesses import messages_stream
from runtime.harnesses.protocol import (
    INFERENCE_ALIAS,
    INFERENCE_KEY_ENV,
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

ADAPTER_VERSION = "grok-harness/1"
PROTOCOLS = ("openai_chat",)
CONFIG_REL = Path(".grok")


def _bin() -> list[str]:
    return cli_bin("GROK_BIN", "grok")


class GrokHarness:
    provider_id = "grok"

    def __init__(self, cli_version: str = "unverified") -> None:
        self.cli_version = cli_version

    def describe(self) -> HarnessManifest:
        return HarnessManifest(
            provider_id="grok",
            adapter_version=ADAPTER_VERSION,
            cli_version=self.cli_version,
            distribution="npm:@xai-official/grok",
            transport="jsonl",
            support_tier="supported",
            credential_methods=["inference_api key via config.toml model env_key"],
            inference_protocols=list(PROTOCOLS),
            native_state_versions=["grok-sessions-1"],
            capabilities={
                "native_resume": Capability("supported", "grok -p -r <session_id>"),
                "native_state_export": Capability("supported", "~/.grok/sessions"),
                "account_portable_resume": Capability(
                    "unknown", "", "not verified across inference connections"
                ),
                "event_stream": Capability("supported", "--output-format streaming-messages-json"),
                "interrupt": Capability("unsupported", "", "supervisor process-group stop only"),
                "steer": Capability("unsupported", "", "single-turn mode has no injection channel"),
                "interactive_approval": Capability("unsupported", "", "--always-approve approves"),
                "mcp": Capability("unknown", "", "grok mcp not wired"),
                "skills": Capability("unknown"),
                "attachments": Capability("unknown", "", "--prompt-json not wired"),
                "structured_output": Capability(
                    "unsupported", "", "--json-schema not wired; prompt-only", "prompt_only"
                ),
                "model_discovery": Capability(
                    "unsupported", "", "models come from the inference connection catalog"
                ),
                "effort_settings": Capability("unknown", "", "--reasoning-effort not wired"),
                "credential_writeback": Capability("unsupported", "", "static API key"),
                "usage": Capability("supported", "result.usage"),
            },
        )

    def prepare(self, context: TurnContext, credentials: dict[str, Any]) -> PreparedHarness:
        home = private_dir(context.home)
        config_dir = private_dir(home / CONFIG_REL)
        inference = resolve_inference(context, credentials, PROTOCOLS)
        # JSON string syntax is valid TOML basic-string syntax for these values.
        config = "\n".join(
            [
                "[models]",
                f"default = {json.dumps(INFERENCE_ALIAS)}",
                "",
                f"[model.{INFERENCE_ALIAS}]",
                f"model = {json.dumps(inference.model)}",
                f"base_url = {json.dumps(inference.base_url)}",
                f"env_key = {json.dumps(INFERENCE_KEY_ENV)}",
                'name = "Custom inference"',
                "",
            ]
        )
        (config_dir / "config.toml").write_text(config)
        env = base_env(home, **{INFERENCE_KEY_ENV: inference.api_key})
        return PreparedHarness(
            home=home, env=env, secrets=[inference.api_key], model=INFERENCE_ALIAS
        )

    def _argv(self, context: TurnContext, session: str | None) -> list[str]:
        argv = [
            *_bin(),
            "-p",
            argv_text(context.prompt),
            "-m",
            INFERENCE_ALIAS,
            "--output-format",
            "streaming-messages-json",
            "--always-approve",
            "--sandbox",
            "off",
            "--cwd",
            str(context.worktree),
        ]
        return argv + (["-r", session] if session else [])

    def start_turn(self, context: TurnContext, prepared: PreparedHarness) -> NativeInvocation:
        return NativeInvocation(self._argv(context, None), context.worktree, prepared.env)

    def resume_turn(
        self, context: TurnContext, prepared: PreparedHarness, binding: dict[str, Any]
    ) -> NativeInvocation:
        native_id = binding.get("native_id")
        if not native_id or binding.get("provider_id") != "grok":
            raise HarnessError("context_unavailable", "no compatible Grok Build session")
        # An id with no local transcript makes the CLI look it up remotely, which waits
        # for an xAI login that BYOK never has. Refuse instead of hanging the Turn.
        sessions = prepared.home / CONFIG_REL / "sessions"
        if not any(sessions.glob(f"*/{native_id}")):
            raise HarnessError("context_unavailable", "Grok Build session is not on this runtime")
        return NativeInvocation(self._argv(context, native_id), context.worktree, prepared.env)

    def new_state(self, context: TurnContext) -> dict[str, Any]:
        return messages_stream.new_state(context)

    def normalize(self, line: str, state: dict[str, Any]) -> list[dict[str, Any]]:
        return messages_stream.normalize("grok", line, state)

    def finish(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return messages_stream.finish("grok.result", state)

    def classify_outcome(self, evidence: dict[str, Any], state: dict[str, Any]) -> HarnessOutcome:
        return messages_stream.classify(evidence, state)

    def discover(self, prepared: PreparedHarness) -> NativeInvocation | None:
        return None

    def native_state_paths(self, home: Path) -> list[Path]:
        sessions = home / CONFIG_REL / "sessions"
        return [sessions] if sessions.exists() else []

    def release(self, prepared: PreparedHarness) -> list[str]:
        return scrub(prepared.credential_files)
