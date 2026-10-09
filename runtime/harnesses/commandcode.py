"""Command Code official CLI Harness (``cmd -p --output-format json``).

Inference is bring-your-own-key: the Turn's endpoint is the custom provider ``sbx`` in
``~/.commandcode/providers.json`` whose ``apiKey`` is an environment reference (the CLI
refuses raw secrets in that file). Verified against command-code 1.79.2 with DeepSeek
over all three protocols.

The CLI runs with ``--local-only`` (``CMD_LOCAL_ONLY=1``), the documented BYOK mode in
which it refuses every Command Code backend call. Its headless entrypoint still checks
that *some* Command Code account key is present before starting, so the adapter sets
``COMMAND_CODE_API_KEY`` to a fixed non-secret placeholder that local-only mode never
sends anywhere. No Command Code account, login or subscription is used.

Transcripts live in ``~/.commandcode/projects/<cwd slug>/<session>.jsonl``; resume
(``--resume <id>``) therefore needs the same worktree path, which the runtime keeps stable.
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
from runtime.security.credentials import private_dir, scrub

ADAPTER_VERSION = "commandcode-harness/1"
PROTOCOLS = ("openai_chat", "anthropic_messages", "openai_responses")
CONFIG_REL = Path(".commandcode")
LOCAL_ONLY_PLACEHOLDER = "sbx-local-only-byok"
_API = {
    "openai_chat": "openai-completions",
    "openai_responses": "openai-responses",
    "anthropic_messages": "anthropic-messages",
}
# Streaming snapshots are forwarded only after this much new text, plus the final one.
SNAPSHOT_STEP = 400


def _bin() -> list[str]:
    return cli_bin("COMMANDCODE_BIN", "cmd")


class CommandCodeHarness:
    provider_id = "commandcode"

    def __init__(self, cli_version: str = "unverified") -> None:
        self.cli_version = cli_version

    def describe(self) -> HarnessManifest:
        return HarnessManifest(
            provider_id="commandcode",
            adapter_version=ADAPTER_VERSION,
            cli_version=self.cli_version,
            distribution="npm:command-code",
            transport="jsonl",
            support_tier="supported",
            credential_methods=["inference_api key via providers.json env reference"],
            inference_protocols=list(PROTOCOLS),
            native_state_versions=["commandcode-projects-jsonl-1"],
            capabilities={
                "native_resume": Capability("supported", "cmd -p --resume <session_id>"),
                "native_state_export": Capability("supported", "~/.commandcode/projects"),
                "account_portable_resume": Capability(
                    "unknown", "", "not verified across inference connections"
                ),
                "event_stream": Capability("supported", "--output-format json"),
                "interrupt": Capability("unsupported", "", "supervisor process-group stop only"),
                "steer": Capability("unsupported", "", "print mode has no injection channel"),
                "interactive_approval": Capability("unsupported", "", "--yolo approves"),
                "mcp": Capability("unknown", "", "cmd mcp not wired"),
                "skills": Capability("unknown", "", "--skill not wired"),
                "attachments": Capability("unknown", "", "not wired"),
                "structured_output": Capability("unsupported", "", "prompt-only", "prompt_only"),
                "model_discovery": Capability(
                    "unsupported", "", "models come from the inference connection catalog"
                ),
                "effort_settings": Capability("unknown", "", "--effort not wired"),
                "credential_writeback": Capability("unsupported", "", "static API key"),
                "usage": Capability("supported", "result.usage"),
            },
        )

    def prepare(self, context: TurnContext, credentials: dict[str, Any]) -> PreparedHarness:
        home = private_dir(context.home)
        config_dir = private_dir(home / CONFIG_REL)
        inference = resolve_inference(context, credentials, PROTOCOLS)
        providers = {
            "provider": {
                INFERENCE_ALIAS: {
                    "name": "Custom inference",
                    "api": _API[inference.protocol],
                    "baseURL": inference.base_url,
                    "apiKey": f"${INFERENCE_KEY_ENV}",
                    "models": {inference.model: {}},
                }
            }
        }
        (config_dir / "providers.json").write_text(json.dumps(providers, indent=2))
        env = base_env(
            home,
            CMD_LOCAL_ONLY="1",
            COMMAND_CODE_API_KEY=LOCAL_ONLY_PLACEHOLDER,
            **{INFERENCE_KEY_ENV: inference.api_key},
        )
        return PreparedHarness(
            home=home,
            env=env,
            secrets=[inference.api_key],
            model=f"{INFERENCE_ALIAS}/{inference.model}",
        )

    def _argv(
        self, context: TurnContext, prepared: PreparedHarness, session: str | None
    ) -> list[str]:
        argv = [
            *_bin(),
            "-p",
            argv_text(context.prompt),
            "-m",
            prepared.model or "",
            "--output-format",
            "json",
            "--yolo",
            "--trust",
            "--skip-onboarding",
            "--no-auto-update",
            "--local-only",
        ]
        return argv + (["--resume", session] if session else [])

    def start_turn(self, context: TurnContext, prepared: PreparedHarness) -> NativeInvocation:
        return NativeInvocation(self._argv(context, prepared, None), context.worktree, prepared.env)

    def resume_turn(
        self, context: TurnContext, prepared: PreparedHarness, binding: dict[str, Any]
    ) -> NativeInvocation:
        native_id = binding.get("native_id")
        if not native_id or binding.get("provider_id") != "commandcode":
            raise HarnessError("context_unavailable", "no compatible Command Code session")
        return NativeInvocation(
            self._argv(context, prepared, native_id), context.worktree, prepared.env
        )

    def new_state(self, context: TurnContext) -> dict[str, Any]:
        return {
            "expected": (context.native_binding or {}).get("native_id"),
            "native_id": None,
            "mismatch": False,
            "message": 0,
            "parts": {},
            "tools": {},
            "errors": [],
            "usage": None,
            "completed": False,
        }

    def _bind(self, sid: Any, state: dict[str, Any], out: list[dict[str, Any]]) -> None:
        if not isinstance(sid, str) or not sid or state["native_id"] is not None:
            return
        state["native_id"] = sid
        if state["expected"] and sid != state["expected"]:
            state["mismatch"] = True
            out.append(
                obs(
                    "diagnostic.reported",
                    category="context_mismatch",
                    message="resumed CLI reported a different native session",
                )
            )
        out.append(
            obs(
                "execution.native_bound",
                provider_id="commandcode",
                native_id=sid,
                resumed=bool(state["expected"]),
            )
        )

    def _parts(
        self, content: Any, state: dict[str, Any], out: list[dict[str, Any]], *, final: bool
    ) -> None:
        for index, block in enumerate(content if isinstance(content, list) else []):
            if not isinstance(block, dict) or block.get("type") not in ("text", "thinking"):
                continue
            text = str(block.get("text") or block.get("thinking") or "")
            if not text:
                continue
            key = f"m{state['message']}-{index}"
            seen = state["parts"].get(key)
            if seen and (
                text == seen["text"] or (not final and len(text) - seen["sent"] < SNAPSHOT_STEP)
            ):
                seen["text"] = text if final else seen["text"]
                continue
            revision = (seen["revision"] if seen else 0) + 1
            state["parts"][key] = {"text": text, "sent": len(text), "revision": revision}
            out.append(
                obs(
                    "message.part_added" if revision == 1 else "message.part_updated",
                    part_key=key,
                    kind="text" if block["type"] == "text" else "reasoning",
                    mode="replace",
                    revision=revision,
                    content=bounded(text, 64000),
                )
            )

    def _error(self, text: str, state: dict[str, Any], out: list[dict[str, Any]]) -> None:
        if text in state["errors"]:
            return
        state["errors"].append(text)
        out.append(
            obs(
                "diagnostic.reported",
                category="provider_error",
                message=bounded(text, 1000),
                credential_health=classify_text(text),
            )
        )

    def normalize(self, line: str, state: dict[str, Any]) -> list[dict[str, Any]]:
        line = line.strip()
        if not line:
            return []
        try:
            frame = json.loads(line)
        except ValueError:
            return [
                obs(
                    "diagnostic.reported",
                    category="malformed_frame",
                    message="unparseable CLI line",
                )
            ]
        if not isinstance(frame, dict):
            return []
        out: list[dict[str, Any]] = []
        if frame.get("type") == "result":
            self._bind(frame.get("sessionId"), state, out)
            state["completed"] = True
            usage = frame.get("usage") if isinstance(frame.get("usage"), dict) else None
            if usage and any(usage.values()):
                state["usage"] = {
                    "input_tokens": int(usage.get("inputTokens") or 0),
                    "output_tokens": int(usage.get("outputTokens") or 0),
                    "cached_input_tokens": int(usage.get("cacheReadTokens") or 0),
                }
            if frame.get("subtype") != "success":
                self._error(str(frame.get("error") or "run failed"), state, out)
            return out
        event = frame.get("event") if isinstance(frame.get("event"), dict) else {}
        kind = event.get("type")
        if kind == "run_start":
            self._bind(event.get("sessionId"), state, out)
        elif kind == "message_start":
            state["message"] += 1
        elif kind in ("message_update", "message_end"):
            self._parts(event.get("content"), state, out, final=kind == "message_end")
        elif kind in ("tool_queued", "tool_running", "tool_update", "tool_completed"):
            call = str(event.get("toolCallId") or f"tool-{len(state['tools'])}")
            known = state["tools"].get(call)
            if known is None:
                tool_input = event.get("input")
                known = state["tools"][call] = {
                    "tool_id": call,
                    "name": str(event.get("toolName") or "tool"),
                    "title": bounded(_title(tool_input), 300),
                    "input": bounded(tool_input, 2000),
                    "done": False,
                }
                out.append(obs("tool.started", **_public(known), status="pending"))
            if known["done"]:
                return out
            if kind == "tool_completed":
                known["done"] = True
                failed = bool(event.get("isError") or event.get("error"))
                out.append(
                    obs(
                        "tool.completed",
                        **_public(known),
                        status="error" if failed else "completed",
                        output=bounded(_text(event.get("result")), 4000),
                        error=failed,
                    )
                )
            elif kind == "tool_running":
                out.append(obs("tool.updated", **_public(known), status="running"))
        elif kind == "run_error":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            self._error(str(message or "run failed"), state, out)
        return out

    def finish(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        if not state["usage"]:
            return []  # missing usage is absent, never synthetic zero
        return [obs("usage.observed", source="commandcode.result", complete=True, **state["usage"])]

    def classify_outcome(self, evidence: dict[str, Any], state: dict[str, Any]) -> HarnessOutcome:
        stderr = str(evidence.get("stderr_tail") or "")
        errors = " ".join(state["errors"])
        if state["mismatch"]:
            return HarnessOutcome(
                "failure", error_code="context_mismatch", message="native session mismatch"
            )
        if evidence.get("exit_code") is None:
            return HarnessOutcome(
                "unknown", "unknown", "outcome_unknown", "process stopped without exit status"
            )
        if evidence["exit_code"] == 0 and state["completed"] and not state["errors"]:
            return HarnessOutcome("success")
        lower = (errors + " " + stderr).lower()
        if "session" in lower and ("not found" in lower or "no session" in lower):
            return HarnessOutcome(
                "failure", error_code="context_unavailable", message="native session not found"
            )
        health = classify_text(lower)
        if health == "invalid":
            return HarnessOutcome(
                "failure",
                "invalid",
                "credential_invalid",
                "provider rejected the credential",
                "replace_credential",
            )
        if health == "rate_limited":
            return HarnessOutcome(
                "failure", "rate_limited", "rate_limited", "provider rate limited", "retry_later"
            )
        return HarnessOutcome(
            "failure", "ok", "provider_failed", (errors or "CLI exited non-zero")[:500], "retry"
        )

    def discover(self, prepared: PreparedHarness) -> NativeInvocation | None:
        return None

    def native_state_paths(self, home: Path) -> list[Path]:
        projects = home / CONFIG_REL / "projects"
        return [projects] if projects.exists() else []

    def release(self, prepared: PreparedHarness) -> list[str]:
        return scrub(prepared.credential_files)


def _public(tool: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in tool.items() if k != "done"}


def _text(result: Any) -> str:
    if isinstance(result, list):
        return "\n".join(
            str(block.get("text") or "") for block in result if isinstance(block, dict)
        )
    return "" if result is None else str(result)


def _title(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "description", "url"):
        if isinstance(tool_input.get(key), str):
            return tool_input[key]
    return ""
