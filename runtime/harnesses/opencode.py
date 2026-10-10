"""OpenCode official CLI Harness (``opencode run --format json``).

Inference is bring-your-own-key: the Turn's endpoint is registered as the custom
provider ``sbx`` in an ``opencode.json`` that references the key by environment
variable name only (verified against opencode-ai 1.18.35 with DeepSeek over all three
protocols). Sessions created before generic inference keep their OpenCode Zen
``auth.json`` lane.

Verified against opencode-ai 1.18.34: ``run [message] --format json -m
provider/model --dir <worktree> --auto [-s <session>]``. Every frame carries
``sessionID``; ``text``/``reasoning`` parts are cumulative snapshots keyed by
``part.id`` (replacement semantics); ``tool_use`` carries ``callID`` and
``state.status``; ``step_finish`` carries token usage. Native state lives in
``$XDG_DATA_HOME/opencode`` next to ``auth.json``; only non-credential files
are native state.
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

ADAPTER_VERSION = "opencode-harness/2"
PROTOCOLS = ("openai_chat", "anthropic_messages", "openai_responses")
# AI SDK package OpenCode loads per protocol, and the path its base URL must end with.
_SDK = {
    "openai_chat": ("@ai-sdk/openai-compatible", ""),
    "openai_responses": ("@ai-sdk/openai", ""),
    "anthropic_messages": ("@ai-sdk/anthropic", "/v1"),
}
# Per-SDK model option that carries a reasoning effort to the endpoint. Anthropic-style
# endpoints only have thinking on/off, so the single value ``none`` turns it off.
_REASONING = {
    "openai_chat": lambda effort: {"reasoningEffort": effort},
    "openai_responses": lambda effort: {"reasoningEffort": effort},
    "anthropic_messages": lambda effort: (
        {"thinking": {"type": "disabled"}} if effort == "none" else {}
    ),
}
DATA_REL = Path(".local/share/opencode")
AUTH_FILE = "auth.json"
_NOT_NATIVE_STATE = {AUTH_FILE, "log"}


def _bin() -> list[str]:
    return cli_bin("OPENCODE_BIN", "opencode")


def qualify_model(model: str | None) -> str | None:
    """Legacy Zen lane only: bare ids belong to the built-in ``opencode`` provider."""
    if not model:
        return None
    return model if "/" in model else f"opencode/{model}"


class OpenCodeHarness:
    provider_id = "opencode"

    def __init__(self, cli_version: str = "unverified") -> None:
        self.cli_version = cli_version

    def describe(self) -> HarnessManifest:
        ev = f"opencode-ai {self.cli_version}"
        return HarnessManifest(
            provider_id="opencode",
            adapter_version=ADAPTER_VERSION,
            cli_version=self.cli_version,
            distribution="npm:opencode-ai",
            transport="jsonl",
            support_tier="supported",
            credential_methods=["inference_api key via custom provider config"],
            inference_protocols=list(PROTOCOLS),
            native_state_versions=["opencode-sqlite-1"],
            capabilities={
                "native_resume": Capability("supported", f"{ev}: run --session <id>"),
                "native_state_export": Capability("supported", "opencode.db + snapshot dir"),
                "account_portable_resume": Capability(
                    "unknown", "", "not verified across inference connections"
                ),
                "event_stream": Capability("supported", "--format json"),
                "interrupt": Capability("unsupported", "", "supervisor process-group stop only"),
                "steer": Capability("unsupported", "", "run mode has no injection channel"),
                "interactive_approval": Capability("unsupported", "", "--auto approves"),
                "mcp": Capability("unknown"),
                "skills": Capability("unknown"),
                "attachments": Capability("unknown", "", "-f not wired"),
                "structured_output": Capability("unsupported", "", "prompt-only", "prompt_only"),
                "model_discovery": Capability(
                    "unsupported", "", "models come from the inference connection catalog"
                ),
                "effort_settings": Capability(
                    "supported",
                    "model options reasoningEffort / thinking",
                    "custom API: thinking off (none) on chat and Anthropic-style endpoints whose "
                    "probe showed the control works; graded levels are not offered",
                ),
                "credential_writeback": Capability("unsupported", "", "static API key"),
                "usage": Capability("supported", "step_finish.tokens"),
            },
        )

    def _data_dir(self, home: Path) -> Path:
        return home / DATA_REL

    def prepare(self, context: TurnContext, credentials: dict[str, Any]) -> PreparedHarness:
        home = private_dir(context.home)
        data = private_dir(self._data_dir(home))
        config_dir = private_dir(home / ".config" / "opencode")
        env = base_env(
            home,
            XDG_DATA_HOME=str(home / ".local" / "share"),
            XDG_CONFIG_HOME=str(home / ".config"),
            XDG_CACHE_HOME=str(home / ".cache"),
            XDG_STATE_HOME=str(home / ".local" / "state"),
            OPENCODE_DISABLE_AUTOUPDATE="1",
        )
        files: list[Path] = []
        legacy = (credentials.get("opencode_zen") or {}).get("api_key")
        if legacy and not context.inference:
            payload = json.dumps({"opencode": {"type": "api", "key": legacy}})
            files.append(write_secret_file(data / AUTH_FILE, payload))
            return PreparedHarness(home=home, env=env, secrets=[legacy], credential_files=files)
        inference = resolve_inference(context, credentials, PROTOCOLS)
        package, suffix = _SDK[inference.protocol]
        config = {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "share": "disabled",
            "provider": {
                INFERENCE_ALIAS: {
                    "npm": package,
                    "name": "Custom inference",
                    "options": {
                        "baseURL": inference.base_url + suffix,
                        "apiKey": f"{{env:{INFERENCE_KEY_ENV}}}",
                    },
                    "models": {
                        inference.model: {
                            "name": inference.model,
                            # Sent as the provider's reasoning effort; only a value the
                            # control plane verified for this endpoint and model arrives.
                            **(
                                {"options": _REASONING[inference.protocol](context.effort)}
                                if context.effort
                                else {}
                            ),
                        }
                    },
                }
            },
        }
        path = config_dir / "sbx-inference.json"
        path.write_text(json.dumps(config, indent=2))
        env.update({"OPENCODE_CONFIG": str(path), INFERENCE_KEY_ENV: inference.api_key})
        prepared = PreparedHarness(home=home, env=env, secrets=[inference.api_key])
        prepared.model = f"{INFERENCE_ALIAS}/{inference.model}"
        return prepared

    def _argv(
        self, context: TurnContext, prepared: PreparedHarness, session: str | None
    ) -> list[str]:
        argv = [*_bin(), "run", argv_text(context.prompt), "--format", "json"]
        model = prepared.model or qualify_model(context.model)
        if model:
            argv += ["-m", model]
        if session:
            argv += ["--session", session]
        return argv + ["--dir", str(context.worktree), "--auto"]

    def start_turn(self, context: TurnContext, prepared: PreparedHarness) -> NativeInvocation:
        return NativeInvocation(self._argv(context, prepared, None), context.worktree, prepared.env)

    def resume_turn(
        self, context: TurnContext, prepared: PreparedHarness, binding: dict[str, Any]
    ) -> NativeInvocation:
        native_id = binding.get("native_id")
        if not native_id or binding.get("provider_id") != "opencode":
            raise HarnessError("context_unavailable", "no compatible OpenCode native session")
        return NativeInvocation(
            self._argv(context, prepared, native_id), context.worktree, prepared.env
        )

    def new_state(self, context: TurnContext) -> dict[str, Any]:
        expected = (context.native_binding or {}).get("native_id")
        return {
            "expected": expected,
            "native_id": None,
            "mismatch": False,
            "parts": {},
            "tools": {},
            "errors": [],
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cached_input_tokens": 0,
            },
            "saw_usage": False,
            "bad_frames": 0,
        }

    def normalize(self, line: str, state: dict[str, Any]) -> list[dict[str, Any]]:
        line = line.strip()
        if not line:
            return []
        try:
            frame = json.loads(line)
        except ValueError:
            state["bad_frames"] += 1
            return [
                obs(
                    "diagnostic.reported",
                    category="malformed_frame",
                    message="unparseable CLI line",
                )
            ]
        if not isinstance(frame, dict):
            state["bad_frames"] += 1
            return []
        out: list[dict[str, Any]] = []
        part = frame.get("part") if isinstance(frame.get("part"), dict) else {}
        sid = frame.get("sessionID") or part.get("sessionID")
        if sid and state["native_id"] is None:
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
                    provider_id="opencode",
                    native_id=sid,
                    resumed=bool(state["expected"]),
                )
            )
        kind = frame.get("type")
        if kind in ("text", "reasoning"):
            key = str(part.get("id") or f"{kind}-{len(state['parts'])}")
            revision = state["parts"].get(key, 0) + 1
            state["parts"][key] = revision
            out.append(
                obs(
                    "message.part_added" if revision == 1 else "message.part_updated",
                    part_key=key,
                    kind="text" if kind == "text" else "reasoning",
                    mode="replace",
                    revision=revision,
                    content=bounded(str(part.get("text") or ""), 64000),
                )
            )
        elif kind == "tool_use":
            call = str(part.get("callID") or part.get("id") or f"tool-{len(state['tools'])}")
            tool_state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(tool_state.get("status") or "")
            seen = call in state["tools"]
            terminal = status in ("completed", "error")
            payload = {
                "tool_id": call,
                "name": str(part.get("tool") or "tool"),
                "status": status or "pending",
                "title": bounded(str(tool_state.get("title") or ""), 300),
                "input": bounded(tool_state.get("input"), 2000),
            }
            if not seen:
                state["tools"][call] = status
                out.append(obs("tool.started", **payload))
            if terminal and state["tools"].get(call) != "done":
                state["tools"][call] = "done"
                out.append(
                    obs(
                        "tool.completed",
                        **payload,
                        output=bounded(tool_state.get("output"), 4000),
                        error=status == "error",
                    )
                )
            elif seen and not terminal:
                out.append(obs("tool.updated", **payload))
        elif kind == "step_finish":
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else None
            if tokens:
                usage = state["usage"]
                usage["input_tokens"] += int(tokens.get("input") or 0)
                usage["output_tokens"] += int(tokens.get("output") or 0)
                usage["reasoning_tokens"] += int(tokens.get("reasoning") or 0)
                usage["cached_input_tokens"] += int((tokens.get("cache") or {}).get("read") or 0)
                state["saw_usage"] = True
        elif kind == "error":
            err = frame.get("error") if isinstance(frame.get("error"), dict) else {}
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            message = str(
                data.get("message")
                or err.get("message")
                or frame.get("message")
                or "provider error"
            )
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
        if not state["saw_usage"]:
            return []  # missing usage is absent, never synthetic zero
        return [
            obs("usage.observed", source="opencode.step_finish", complete=True, **state["usage"])
        ]

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
        if evidence["exit_code"] == 0 and not state["errors"]:
            return HarnessOutcome("success")
        health = classify_text(errors + " " + stderr)
        if "session" in stderr.lower() and "not found" in stderr.lower():
            return HarnessOutcome(
                "failure", error_code="context_unavailable", message="native session not found"
            )
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
        data = self._data_dir(home)
        if not data.exists():
            return []
        return [p for p in data.iterdir() if p.name not in _NOT_NATIVE_STATE]

    def release(self, prepared: PreparedHarness) -> list[str]:
        return scrub(prepared.credential_files)
