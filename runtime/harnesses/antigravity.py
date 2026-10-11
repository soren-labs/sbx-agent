"""Antigravity official CLI Harness (``agy -p --output-format stream-json``).

Subscription only: a Turn runs on a Machine Slot whose Volume holds the official Google
sign-in. ``agy`` has no home override, so the CLI's ``HOME`` is the Slot profile itself;
no key or token is passed, read or written here. Verified against a signed-in Antigravity
CLI 1.3.3 (recorded stream in ``tests/fixtures/harnesses/antigravity``):

    {"event":"init","conversation_id":...,"init":{...}}
    {"event":"step_update","step_update":{"step_index","state","step_type","text_delta",...}}
    {"event":"result","result":{"status","response","error","usage",...}}

A print-mode run exits non-zero on failure (3 for a model or agent error, with an
``AGY_ERROR: {...}`` line on stderr); the ``result`` status is the authority either way.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

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
    bounded,
    classify_text,
    cli_bin,
    obs,
)
from runtime.security.credentials import private_dir

ADAPTER_VERSION = "antigravity-harness/1"
# Signed-out wording of the CLI, in addition to the generic needles.
AUTH_NEEDLES = (
    "please sign in",
    "authentication required",
    "authentication expired",
    "log back in",
)


def _bin() -> list[str]:
    return cli_bin("AGY_BIN", "agy")


class AntigravityHarness:
    provider_id = "antigravity"

    def __init__(self, cli_version: str = "unverified") -> None:
        self.cli_version = cli_version

    def describe(self) -> HarnessManifest:
        return HarnessManifest(
            provider_id="antigravity",
            adapter_version=ADAPTER_VERSION,
            cli_version=self.cli_version,
            distribution="archive:antigravity-cli",
            transport="jsonl",
            support_tier="supported",
            credential_methods=["official login on a Machine Slot volume"],
            # No custom-API mode is wired: the CLI's own API-key mode is a different product path.
            inference_protocols=[],
            native_state_versions=["antigravity-conversations-1"],
            capabilities={
                "native_resume": Capability("supported", "agy -p --conversation <id>"),
                "native_state_export": Capability(
                    "unsupported", "", "conversations stay on the Machine Slot volume"
                ),
                "account_portable_resume": Capability("unknown"),
                "event_stream": Capability("supported", "--output-format stream-json"),
                "interrupt": Capability("unsupported", "", "supervisor process-group stop only"),
                "steer": Capability("unsupported", "", "--input-format stream-json not wired"),
                "interactive_approval": Capability(
                    "unsupported", "", "--dangerously-skip-permissions approves"
                ),
                "mcp": Capability("unknown", "", "agy mcp not wired"),
                "skills": Capability("unknown"),
                "attachments": Capability("unknown"),
                "structured_output": Capability(
                    "unsupported", "", "--json-schema not wired; prompt-only", "prompt_only"
                ),
                "model_discovery": Capability(
                    "supported",
                    "agy models",
                    "the Machine Slot's authenticated list; text output, slug and display name",
                ),
                "effort_settings": Capability(
                    "unsupported",
                    "",
                    "effort is part of the model: the CLI lists one slug per effort variant and "
                    "rejects --effort next to it",
                ),
                "credential_writeback": Capability(
                    "supported", "", "the CLI refreshes its login on the Machine Slot volume"
                ),
                "usage": Capability("supported", "result.usage"),
            },
        )

    def prepare(self, context: TurnContext, credentials: dict[str, Any]) -> PreparedHarness:
        route = context.inference or {}
        if route.get("mode") != "subscription" or route.get("provider_id") != "antigravity":
            raise HarnessError("connection_required", "Antigravity runs on a machine slot only")
        profile = {k: str(v) for k, v in (route.get("env") or {}).items()}
        home = Path(profile.get("HOME") or "")
        if not profile.get("HOME") or not home.is_dir():
            raise HarnessError("connection_required", "the machine slot profile is not mounted")
        if context.effort:
            # ``--model <variant> --effort <level>`` is refused by the CLI (measured, 1.3.3).
            raise HarnessError(
                "validation_failed", "Antigravity sets reasoning effort through the model variant"
            )
        private_dir(context.home)
        return PreparedHarness(home=home, env=base_env(home, **profile))

    def _argv(
        self, context: TurnContext, prepared: PreparedHarness, conversation: str | None
    ) -> list[str]:
        argv = [
            *_bin(),
            "-p",
            argv_text(context.prompt),
            "--output-format",
            "stream-json",
            # Print mode has no prompts: without this, tools that need approval are denied.
            "--dangerously-skip-permissions",
            "--print-timeout",
            f"{int(context.deadline_seconds)}s",
        ]
        if context.model:
            argv += ["--model", context.model]
        argv += prepared.cli_args
        return argv + (["--conversation", conversation] if conversation else [])

    def start_turn(self, context: TurnContext, prepared: PreparedHarness) -> NativeInvocation:
        return NativeInvocation(self._argv(context, prepared, None), context.worktree, prepared.env)

    def resume_turn(
        self, context: TurnContext, prepared: PreparedHarness, binding: dict[str, Any]
    ) -> NativeInvocation:
        native_id = binding.get("native_id")
        if not native_id or binding.get("provider_id") != "antigravity":
            raise HarnessError("context_unavailable", "no compatible Antigravity conversation")
        return NativeInvocation(
            self._argv(context, prepared, str(native_id)), context.worktree, prepared.env
        )

    def new_state(self, context: TurnContext) -> dict[str, Any]:
        return {
            "expected": (context.native_binding or {}).get("native_id"),
            "native_id": None,
            "mismatch": False,
            "parts": {},
            "tools": {},
            "errors": [],
            "usage": None,
            "status": None,
        }

    def _bind(self, state: dict[str, Any], conversation: Any) -> list[dict[str, Any]]:
        if not isinstance(conversation, str) or not conversation or state["native_id"]:
            return []
        state["native_id"] = conversation
        if state["expected"] and conversation != state["expected"]:
            state["mismatch"] = True
        return [
            obs(
                "execution.native_bound",
                provider_id="antigravity",
                native_id=conversation,
                resumed=bool(state["expected"]),
            )
        ]

    def normalize(self, line: str, state: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            frame = json.loads(line)
        except ValueError:
            if not line.strip():
                return []
            return [
                obs(
                    "diagnostic.reported",
                    category="malformed_frame",
                    message="unparseable CLI line",
                )
            ]
        if not isinstance(frame, dict):
            return []
        kind = frame.get("event")
        body = frame.get(kind) if isinstance(frame.get(kind), dict) else {}
        out = self._bind(state, frame.get("conversation_id") or body.get("conversation_id"))
        if kind == "step_update":
            out += self._step(body, state)
        elif kind == "result":
            state["status"] = str(body.get("status") or "")
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
            if usage:
                counts = {k: int(v) for k, v in usage.items() if isinstance(v, int)}
                cached = counts.get("cache_read_tokens", 0)
                state["usage"] = {
                    # The CLI counts cache reads inside input_tokens; every Harness reports
                    # input_tokens as the uncached part.
                    "input_tokens": max(0, counts.get("input_tokens", 0) - cached),
                    "cached_input_tokens": cached,
                    "output_tokens": counts.get("output_tokens", 0),
                    "reasoning_tokens": counts.get("thinking_tokens", 0),
                }
            if state["status"] != "SUCCESS":
                message = str(body.get("error") or state["status"] or "provider error")
                state["errors"].append(message)
                out.append(
                    obs(
                        "diagnostic.reported",
                        category="provider_error",
                        message=bounded(message, 1000),
                        credential_health=_health(message),
                    )
                )
        return out

    def _step(self, step: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
        key = f"step-{step.get('step_index')}"
        step_type = step.get("step_type")
        done = step.get("state") == "DONE"
        if step_type == "agent_response":
            delta = step.get("text_delta")
            if not isinstance(delta, str) or not delta:
                return []
            revision = state["parts"].get(key, 0) + 1
            state["parts"][key] = revision
            return [
                obs(
                    "message.part_added" if revision == 1 else "message.part_updated",
                    part_key=key,
                    kind="text",
                    mode="append",
                    revision=revision,
                    content=bounded(delta, 64000),
                )
            ]
        if step_type != "tool":
            return []
        info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
        name = str(step.get("tool_name") or info.get("name") or "tool")
        # The CLI prints the parameters as text, e.g. ``{'CommandLine': 'cat hello.txt'}``.
        parameters = info.get("parameters")
        shown = re.search(r"'(?:CommandLine|TargetFile)': '([^']*)'", str(parameters or ""))
        title = shown.group(1) if shown else None
        payload = {
            "tool_id": key,
            "name": name,
            "status": "completed" if done else "in_progress",
            "title": bounded(str(title or name), 300),
            "input": bounded(parameters, 2000),
        }
        out = []
        if key not in state["tools"]:
            state["tools"][key] = False
            out.append(obs("tool.started", **payload))
        if done and not state["tools"][key]:
            state["tools"][key] = True
            out.append(
                obs(
                    "tool.completed",
                    **payload,
                    output=bounded(info.get("output") or info.get("result"), 4000),
                    error=bool(info.get("error")),
                )
            )
        return out

    def finish(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        if not state["usage"]:
            return []  # missing usage is absent, never synthetic zero
        return [obs("usage.observed", source="antigravity.result", complete=True, **state["usage"])]

    def classify_outcome(self, evidence: dict[str, Any], state: dict[str, Any]) -> HarnessOutcome:
        if state["mismatch"]:
            return HarnessOutcome("failure", error_code="context_mismatch")
        if evidence.get("exit_code") is None:
            return HarnessOutcome("unknown", "unknown", "outcome_unknown")
        if evidence["exit_code"] == 0 and state["status"] == "SUCCESS" and not state["errors"]:
            return HarnessOutcome("success")
        text = " ".join(state["errors"]) + " " + str(evidence.get("stderr_tail") or "")
        health = _health(text)
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
        # Conversations live with the login on the Slot Volume and are never checkpointed.
        return []

    def release(self, prepared: PreparedHarness) -> list[str]:
        return []


def _health(text: str) -> str:
    if any(needle in text.lower() for needle in AUTH_NEEDLES):
        return "invalid"
    return classify_text(text)
