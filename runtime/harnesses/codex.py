"""Codex official CLI Harness (``codex exec --json``). Optional, experimental lane.

Never required for onboarding. Credential is an uploaded native ``auth.json``
materialized into an isolated ``CODEX_HOME``; native threads live alongside it,
so only ``sessions/`` is native state.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
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
    bounded,
    classify_text,
    obs,
)
from runtime.security.credentials import private_dir, scrub, write_secret_file

ADAPTER_VERSION = "codex-harness/1"


def _bin() -> list[str]:
    tokens = shlex.split(os.environ.get("CODEX_BIN", "codex")) or ["codex"]
    if len(tokens) == 1 and tokens[0].endswith(".py"):
        return [sys.executable, tokens[0]]
    return tokens


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
            support_tier="experimental",
            credential_methods=["codex native auth.json upload"],
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
                "model_discovery": Capability("unknown"),
                "effort_settings": Capability("unknown"),
                "credential_writeback": Capability("unknown", "", "refresh writeback not enabled"),
                "usage": Capability("supported", "turn.completed.usage"),
            },
        )

    def prepare(self, context: TurnContext, credentials: dict[str, Any]) -> PreparedHarness:
        home = private_dir(context.home)
        codex_home = private_dir(home / ".codex")
        bundle = (credentials.get("codex") or {}).get("auth_json")
        files, secrets = [], []
        if bundle:
            files.append(write_secret_file(codex_home / "auth.json", bundle))
            try:
                parsed = json.loads(bundle)
                secrets += [v for v in _strings(parsed) if len(v) >= 12]
            except ValueError:
                pass
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
        }
        return PreparedHarness(home=home, env=env, secrets=secrets, credential_files=files)

    def _common(self, context: TurnContext) -> list[str]:
        flags = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        if context.model:
            flags += ["-m", context.model]
        return flags

    def start_turn(self, context: TurnContext, prepared: PreparedHarness) -> NativeInvocation:
        argv = [
            *_bin(),
            "exec",
            *self._common(context),
            "-C",
            str(context.worktree),
            context.prompt,
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
            *self._common(context),
            binding["native_id"],
            context.prompt,
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
            if item_type in ("agent_message", "reasoning"):
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
                state["usage"] = {k: int(v) for k, v in usage.items() if isinstance(v, int)}
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
