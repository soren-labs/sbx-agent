"""Codex official CLI Harness (``codex exec --json``).

Inference is bring-your-own-key: the Turn's endpoint is written to ``config.toml`` as
the model provider ``sbx`` with ``env_key`` naming the variable that holds the key, so
no credential reaches disk. Codex 0.162 speaks only the OpenAI Responses wire API
(``wire_api = "chat"`` was removed upstream), verified with DeepSeek ``/responses``.
Native threads live in ``CODEX_HOME/sessions``, the only native state.

A subscription Turn (Machine Slot) instead points ``CODEX_HOME`` at the official login
on the mounted Slot Volume: no key is passed, nothing is written to the profile's
``config.toml``, and the model and ``model_reasoning_effort`` travel as CLI overrides.
Its native threads live on that Volume, so they follow the Slot across Worker VMs.

Sessions created before generic inference keep their uploaded ``auth.json`` lane.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    bounded,
    classify_text,
    cli_bin,
    obs,
    resolve_inference,
)
from runtime.security.credentials import private_dir, scrub, write_secret_file

ADAPTER_VERSION = "codex-harness/3"
PROTOCOLS = ("openai_responses",)


def _bin() -> list[str]:
    return cli_bin("CODEX_BIN", "codex")


class CodexHarness:
    provider_id = "codex"

    def __init__(self, cli_version: str = "unverified") -> None:
        self.cli_version = cli_version

    def describe(self) -> HarnessManifest:
        return HarnessManifest(
            provider_id="codex",
            adapter_version=ADAPTER_VERSION,
            cli_version=self.cli_version,
            distribution="npm:@openai/codex",
            transport="jsonl",
            support_tier="supported",
            credential_methods=[
                "inference_api key via model_providers env_key",
                "official login on a Machine Slot volume",
            ],
            inference_protocols=list(PROTOCOLS),
            native_state_versions=["codex-sessions-1"],
            capabilities={
                "native_resume": Capability("supported", "codex exec resume <thread>"),
                "native_state_export": Capability("supported", "CODEX_HOME/sessions"),
                "account_portable_resume": Capability("unknown"),
                "event_stream": Capability("supported", "exec --json"),
                "interrupt": Capability("unsupported", "", "supervisor stop only"),
                "steer": Capability("unsupported"),
                "interactive_approval": Capability("unsupported", "", "bypass flag"),
                "mcp": Capability("unknown"),
                "skills": Capability("unknown"),
                "attachments": Capability("unknown"),
                "structured_output": Capability("unsupported", "", "prompt-only", "prompt_only"),
                "model_discovery": Capability(
                    "supported",
                    "app-server model/list",
                    "subscription: the Machine Slot's authenticated catalog; "
                    "custom API: the inference connection catalog",
                ),
                "effort_settings": Capability(
                    "supported",
                    "-c model_reasoning_effort",
                    "subscription only, limited to the efforts the selected model lists; with a "
                    "custom API the CLI does not forward the setting (measured), so none is offered",
                ),
                "credential_writeback": Capability("unsupported", "", "static API key"),
                "usage": Capability("supported", "turn.completed.usage"),
            },
        )

    def prepare(self, context: TurnContext, credentials: dict[str, Any]) -> PreparedHarness:
        home = private_dir(context.home)
        codex_home = private_dir(home / ".codex")
        env = base_env(home, CODEX_HOME=str(codex_home))
        if (context.inference or {}).get("mode") == "subscription":
            return self._prepare_subscription(context, home)
        bundle = (credentials.get("codex") or {}).get("auth_json")
        if bundle and not context.inference:
            files, secrets = [write_secret_file(codex_home / "auth.json", bundle)], []
            try:
                secrets += [v for v in _strings(json.loads(bundle)) if len(v) >= 12]
            except ValueError:
                pass
            return PreparedHarness(home=home, env=env, secrets=secrets, credential_files=files)
        inference = resolve_inference(context, credentials, PROTOCOLS)
        # JSON string syntax is valid TOML basic-string syntax for these values.
        config = "\n".join(
            [
                f"model = {json.dumps(inference.model)}",
                f"model_provider = {json.dumps(INFERENCE_ALIAS)}",
                "",
                f"[model_providers.{INFERENCE_ALIAS}]",
                'name = "Custom inference"',
                f"base_url = {json.dumps(inference.base_url)}",
                f"env_key = {json.dumps(INFERENCE_KEY_ENV)}",
                'wire_api = "responses"',
                "",
            ]
        )
        (codex_home / "config.toml").write_text(config)
        env[INFERENCE_KEY_ENV] = inference.api_key
        return PreparedHarness(
            home=home, env=env, secrets=[inference.api_key], model=inference.model
        )

    def _prepare_subscription(self, context: TurnContext, home: Path) -> PreparedHarness:
        """Official login on the Slot Volume; the CLI is its only reader."""
        route = context.inference or {}
        profile = {k: str(v) for k, v in (route.get("env") or {}).items() if k != "HOME"}
        if route.get("provider_id") != "codex" or not profile.get("CODEX_HOME"):
            raise HarnessError("connection_required", "no Codex machine slot was provided")
        if not Path(profile["CODEX_HOME"]).is_dir():
            raise HarnessError("connection_required", "the machine slot profile is not mounted")
        env = base_env(home, **profile)
        # File-backed auth (a VM has no keyring); the effort is the model's native setting.
        args = ["-c", 'cli_auth_credentials_store="file"']
        if context.effort:
            args += ["-c", f"model_reasoning_effort={json.dumps(context.effort)}"]
        return PreparedHarness(home=home, env=env, cli_args=args)

    def _common(self, context: TurnContext, prepared: PreparedHarness) -> list[str]:
        flags = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        model = prepared.model or context.model
        if model:
            flags += ["-m", model]
        return [*flags, *prepared.cli_args]

    def start_turn(self, context: TurnContext, prepared: PreparedHarness) -> NativeInvocation:
        argv = [
            *_bin(),
            "exec",
            *self._common(context, prepared),
            "-C",
            str(context.worktree),
            argv_text(context.prompt),
        ]
        return NativeInvocation(argv, context.worktree, prepared.env)

    def resume_turn(
        self, context: TurnContext, prepared: PreparedHarness, binding: dict[str, Any]
    ) -> NativeInvocation:
        if binding.get("provider_id") != "codex" or not binding.get("native_id"):
            raise HarnessError("context_unavailable", "no compatible Codex thread")
        argv = [
            *_bin(),
            "exec",
            "resume",
            *self._common(context, prepared),
            binding["native_id"],
            argv_text(context.prompt),
        ]
        return NativeInvocation(argv, context.worktree, prepared.env)

    def new_state(self, context: TurnContext) -> dict[str, Any]:
        return {
            "expected": (context.native_binding or {}).get("native_id"),
            "native_id": None,
            "mismatch": False,
            "items": {},
            "errors": [],
            "usage": None,
            "completed": False,
        }

    def normalize(self, line: str, state: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            frame = json.loads(line)
        except ValueError:
            return (
                [
                    obs(
                        "diagnostic.reported",
                        category="malformed_frame",
                        message="unparseable CLI line",
                    )
                ]
                if line.strip()
                else []
            )
        if not isinstance(frame, dict):
            return []
        out: list[dict[str, Any]] = []
        kind = frame.get("type")
        if kind == "thread.started" and frame.get("thread_id"):
            sid = frame["thread_id"]
            state["native_id"] = sid
            if state["expected"] and sid != state["expected"]:
                state["mismatch"] = True
            out.append(
                obs(
                    "execution.native_bound",
                    provider_id="codex",
                    native_id=sid,
                    resumed=bool(state["expected"]),
                )
            )
        elif kind in ("item.started", "item.updated", "item.completed"):
            item = frame.get("item") if isinstance(frame.get("item"), dict) else {}
            item_id = str(item.get("id") or len(state["items"]))
            item_type = item.get("type")
            if item_type == "error":
                # A CLI notice (e.g. unknown model metadata), not a provider failure.
                if kind == "item.completed":
                    out.append(
                        obs(
                            "diagnostic.reported",
                            category="cli_notice",
                            message=bounded(str(item.get("message") or ""), 1000),
                        )
                    )
            elif item_type in ("agent_message", "reasoning"):
                revision = state["items"].get(item_id, 0) + 1
                state["items"][item_id] = revision
                out.append(
                    obs(
                        "message.part_added" if revision == 1 else "message.part_updated",
                        part_key=item_id,
                        kind="text" if item_type == "agent_message" else "reasoning",
                        mode="replace",
                        revision=revision,
                        content=bounded(str(item.get("text") or ""), 64000),
                    )
                )
            else:
                name = "shell" if item_type == "command_execution" else str(item_type or "tool")
                payload = {
                    "tool_id": item_id,
                    "name": name,
                    "status": str(item.get("status") or kind.split(".")[1]),
                    "title": bounded(str(item.get("command") or ""), 300),
                    "input": bounded(item.get("changes") or item.get("command"), 2000),
                }
                if item_id not in state["items"]:
                    state["items"][item_id] = 0
                    out.append(obs("tool.started", **payload))
                if kind == "item.completed":
                    out.append(
                        obs(
                            "tool.completed",
                            **payload,
                            output=bounded(item.get("aggregated_output"), 4000),
                            error=item.get("status") == "failed",
                        )
                    )
                elif kind == "item.updated":
                    out.append(obs("tool.updated", **payload))
        elif kind == "turn.completed":
            state["completed"] = True
            usage = frame.get("usage") if isinstance(frame.get("usage"), dict) else None
            if usage:
                counts = {k: int(v) for k, v in usage.items() if isinstance(v, int)}
                # Codex counts cached tokens inside input_tokens; every Harness reports
                # input_tokens as the uncached part so totals mean the same thing.
                cached = counts.get("cached_input_tokens", 0)
                counts["input_tokens"] = max(0, counts.get("input_tokens", 0) - cached)
                state["usage"] = counts
        elif kind in ("turn.failed", "error"):
            err = frame.get("error") if isinstance(frame.get("error"), dict) else {}
            message = str(err.get("message") or frame.get("message") or "provider error")
            state["errors"].append(message)
            out.append(
                obs(
                    "diagnostic.reported",
                    category="provider_error",
                    message=bounded(message, 1000),
                    credential_health=classify_text(message),
                )
            )
        return out

    def finish(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        if not state["usage"]:
            return []
        return [
            obs("usage.observed", source="codex.turn.completed", complete=True, **state["usage"])
        ]

    def classify_outcome(self, evidence: dict[str, Any], state: dict[str, Any]) -> HarnessOutcome:
        if state["mismatch"]:
            return HarnessOutcome("failure", error_code="context_mismatch")
        if evidence.get("exit_code") is None:
            return HarnessOutcome("unknown", "unknown", "outcome_unknown")
        if evidence["exit_code"] == 0 and state["completed"] and not state["errors"]:
            return HarnessOutcome("success")
        health = classify_text(
            " ".join(state["errors"]) + " " + str(evidence.get("stderr_tail") or "")
        )
        if health == "invalid":
            return HarnessOutcome(
                "failure", "invalid", "credential_invalid", retry_advice="replace_credential"
            )
        if health == "rate_limited":
            return HarnessOutcome(
                "failure", "rate_limited", "rate_limited", retry_advice="retry_later"
            )
        return HarnessOutcome(
            "failure", "ok", "provider_failed", " ".join(state["errors"])[:500], "retry"
        )

    def discover(self, prepared: PreparedHarness) -> NativeInvocation | None:
        return None

    def native_state_paths(self, home: Path) -> list[Path]:
        sessions = home / ".codex" / "sessions"
        return [sessions] if sessions.exists() else []

    def release(self, prepared: PreparedHarness) -> list[str]:
        return scrub(prepared.credential_files)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []
