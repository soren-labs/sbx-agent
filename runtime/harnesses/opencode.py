"""Official OpenCode CLI. Adapted from baseline adapter's recorded native semantics.

No model API, no reasoning loop, no Codex-shaped intermediate event vocabulary.
Source protocol: anomalyco/opencode v1.18.29 cli/cmd/run.ts and auth/index.ts.
"""

import json
import os
from pathlib import Path

from protocol.capabilities import NAMES, Capability, HarnessManifest
from protocol.runtime import ProtocolError

from runtime.harnesses.protocol import HarnessOutcome, NativeInvocation


class OpenCodeHarness:
    def __init__(
        self,
        binary=("opencode",),
        cli_version="1.18.29",
        distribution_digest=(
            "sha512-syIDVwlrYTgTOXzZe9SkInJWethbq6l3SNC762UeXyO0a9V0wGfd+"
            "U4yACvppwNBnhIsl0j2QPYYCyLpNaSomg=="
        ),
    ):
        self.binary = tuple(binary)
        self.cli_version = cli_version
        self.distribution_digest = distribution_digest

    def describe(self):
        caps = {name: Capability() for name in NAMES}
        if self.cli_version != "1.18.29":
            return HarnessManifest(
                "opencode",
                "1",
                self.cli_version,
                self.distribution_digest,
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
            self.cli_version,
            self.distribution_digest,
            "jsonl",
            "experimental",
            caps,
        )

    def prepare(self, context, credential_bundle):
        if context.settings.get("effort") or context.settings.get("steer"):
            raise ProtocolError("unsupported_capability")
        home = context.home
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_STATE_HOME": str(home / ".state"),
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_SHARE": "true",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
        }
        for name, value in context.settings.get("env", {}).items():
            if name in {"HOME", "PATH"} or name.startswith(("SBX_", "XDG_", "OPENCODE_")):
                raise ProtocolError("forbidden")
            env[name] = value
        if credential_bundle:
            key = credential_bundle.get("api_key")
            if not isinstance(key, str) or not key:
                raise ProtocolError("credential_invalid")
            # Official supported env; auth file never persists static key.
            env["OPENCODE_AUTH_CONTENT"] = json.dumps({"opencode": {"type": "api", "key": key}})
        return env

    def start_turn(self, context, env):
        argv = [
            *self.binary,
            "run",
            context.prompt,
            "--format",
            "json",
            "--dir",
            str(context.worktree),
            "--auto",
        ]
        if context.model:
            argv += ["--model", context.model]
        return NativeInvocation(argv, context.worktree, env)

    def resume_turn(self, context, env):
        self.validate_native_state(context.home, context.native_id)
        invocation = self.start_turn(context, env)
        return NativeInvocation(
            invocation.argv + ["--session", context.native_id], invocation.cwd, invocation.env
        )

    def normalize(self, frame, state):
        if not isinstance(frame, dict):
            raise ProtocolError("malformed_frame")
        result = []
        sid = frame.get("sessionID") or frame.get("part", {}).get("sessionID")
        if sid and not state.get("native_id"):
            if state.get("expected_native_id") and sid != state["expected_native_id"]:
                raise ProtocolError("context_mismatch")
            state["native_id"] = sid
            result.append({"type": "execution.native_bound", "payload": {"native_id": sid}})
        if sid and state.get("native_id") != sid:
            raise ProtocolError("context_mismatch")
        kind, part = frame.get("type"), frame.get("part", {})
        if kind == "text":
            pid = part.get("id") or "text"
            revisions = state.setdefault("revisions", {})
            revisions[pid] = revisions.get(pid, 0) + 1
            text = str(part.get("text", ""))
            state.setdefault("parts", {})[pid] = text
            result.append(
                {
                    "type": "message.part_updated",
                    "payload": {
                        "part_id": pid,
                        "kind": "text",
                        "revision": revisions[pid],
                        "mode": "replace",
                        "text": text,
                    },
                }
            )
        elif kind == "tool_use":
            status = part.get("state", {}).get("status", "unknown")
            result.append(
                {
                    "type": "tool.completed"
                    if status in {"completed", "error"}
                    else "tool.started",
                    "payload": {
                        "tool_id": part.get("callID") or part.get("id"),
                        "name": part.get("tool"),
                        "state": part.get("state", {}),
                    },
                }
            )
        elif kind == "step_finish":
            if part.get("tokens") is not None:
                result.append(
                    {
                        "type": "usage.observed",
                        "payload": {
                            "source": "official_cli",
                            "tokens": part["tokens"],
                            "cost": part.get("cost"),
                        },
                    }
                )
            if part.get("reason") != "tool-calls":
                state["terminal"] = part.get("reason") in {None, "", "stop"}
                if not state["terminal"]:
                    state["error"] = "provider_failed"
                result.append(
                    {
                        "type": "execution.observed_terminal",
                        "payload": {"success": state["terminal"]},
                    }
                )
        elif kind == "error":
            text = json.dumps(frame.get("error", frame))
            lower = text.lower()
            state["error"] = (
                "credential_invalid"
                if any(
                    x in lower
                    for x in ("401", "unauthorized", "invalid api key", "incorrect api key")
                )
                else "rate_limited"
                if any(x in lower for x in ("429", "rate limit", "quota"))
                else "provider_failed"
            )
            result.append({"type": "diagnostic.reported", "payload": {"code": state["error"]}})
        return result

    def classify_outcome(self, evidence, state):
        verdict = (
            "failure"
            if state.get("error") or evidence.get("exit_code") not in {0, None}
            else "success"
            if evidence.get("exit_code") == 0 and state.get("terminal") and state.get("native_id")
            else "unknown"
        )
        return HarnessOutcome(
            verdict,
            "invalid" if state.get("error") == "credential_invalid" else "unknown",
            state.get("error", "none"),
            state.get("native_id"),
            bool(state.get("terminal")),
        )

    def export_native_state(self, home: Path):
        base = home / ".local/share/opencode"
        return [p for p in base.glob("opencode.db*") if p.is_file() and not p.is_symlink()]

    def validate_native_state(self, home, native_id):
        if not native_id or not self.export_native_state(home):
            raise ProtocolError("context_unavailable")

    def release(self, context):
        for rel in (".local/share/opencode/auth.json",):
            (context.home / rel).unlink(missing_ok=True)
        for path in (context.home / ".local/share/opencode/log").glob("*"):
            if path.is_file():
                path.unlink()
        # All provider processes have stopped before release. Env is not persisted.
        os.chmod(context.home, 0o700)
