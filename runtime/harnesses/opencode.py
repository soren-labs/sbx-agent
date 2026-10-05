"""OpenCode Harness adapter (RFC 167 §03).

Drives the official ``opencode`` CLI: ``opencode run <prompt> --format json
[-m provider/model] [--session <native_id>] --dir <worktree> --auto``.
Normalization maps the official CLI's event stream to the unified Turn
observation shape —
sessionID → native context, cumulative ``text``/``reasoning`` parts flushed
at tool/step boundaries, ``tool_use`` keyed by ``callID``, ``step_finish``
usage accumulation, ``error`` terminal frames, stale ``--session`` →
``context_mismatch`` (never fork a new conversation).

Credentials: the Zen API key is materialized as
``$XDG_DATA_HOME/opencode/auth.json`` from the CredentialBundle *files* map
(or passed as ``OPENCODE_API_KEY`` env). Host HOME is never inherited.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from protocol.capabilities import (
    CAPABILITY_NAMES,
    CapabilityEntry,
    CapabilityObservation,
    HarnessManifest,
)
from protocol.errors import (
    WIRE_CONTEXT_MISMATCH,
    WIRE_CREDENTIAL_INVALID,
    WIRE_PROVIDER_ERROR,
    WIRE_RATE_LIMITED,
)
from protocol.events import Observation, ObservationKind, Usage
from protocol.manifests import NativeContextBinding, NativeStateManifest

from . import native_state
from .protocol import (
    ADAPTER_VERSION,
    ContextMismatch,
    CredentialBundle,
    HarnessOutcome,
    NativeInvocation,
    NormalizeState,
    OutcomeKind,
    PreparedHarness,
    ProcessEvidence,
    TurnContext,
    UnsupportedCapability,
)
from .transports.jsonl import parse_line

PROVIDER_ID = "opencode"

# filesystem.md: credential lives at the XDG data path.
AUTH_REL = ".local/share/opencode/auth.json"
# Opencode persists native session state under the XDG data dir's
# project-scoped storage — the export allowlist (auth.json is denied
# globally by native_state.DENY_PATTERNS).
STATE_PREFIXES = ("project", "storage")

_AUTH_NEEDLES = (
    "incorrect api key",
    "invalid api key",
    "unauthorized",
    "unauthenticated",
    "authentication",
    "no credentials",
    "not signed in",
    "401",
)
_RATE_NEEDLES = (
    "429",
    "rate_limit",
    "rate limit",
    "too many requests",
    "quota",
    "resource_exhausted",
    "exhausted",
    "overloaded",
)

_FILE_TOOLS = {
    "edit",
    "write",
    "patch",
    "apply_patch",
    "multiedit",
    "multi_edit",
    "str_replace",
    "str_replace_editor",
    "write_to_file",
    "replace_file_content",
}
_TERMINAL_TOOL_STATUSES = ("completed", "error")
_STEP_CONTINUE = "tool-calls"

# Models verified free to call in MVP evidence runs; substring "free" also
# marks a listing entry free.
KNOWN_FREE_MODELS = frozenset({"opencode/big-pickle"})


def opencode_bin_tokens() -> list[str]:
    raw = os.environ.get("OPENCODE_BIN", "opencode")
    tokens = shlex.split(raw)
    if not tokens:
        tokens = ["opencode"]
    if len(tokens) == 1 and tokens[0].endswith(".py"):
        return [sys.executable, tokens[0]]
    return tokens


def _cli_version(env: dict[str, str]) -> str | None:
    try:
        proc = subprocess.run(
            [*opencode_bin_tokens(), "--version"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


class OpencodeHarness:
    """Harness for the official OpenCode CLI (JSONL stdout transport)."""

    provider_id = PROVIDER_ID

    def __init__(self, state_root: Path) -> None:
        self._state_root = Path(state_root)

    # -- contract -----------------------------------------------------

    def describe(self) -> HarnessManifest:
        cli_version = _cli_version(self._base_env())
        caps: list[CapabilityEntry] = []
        evidence = "opencode-adapter-1"
        for name in CAPABILITY_NAMES:
            caps.append(
                CapabilityEntry.unknown(name, evidence_id=evidence, cli_version=cli_version)
            )
        known = {
            # One-shot `opencode run` emits NDJSON observations.
            "event_stream": CapabilityEntry.supported(
                "event_stream", evidence_id=evidence, cli_version=cli_version
            ),
            # Verified: `opencode run --session <id>` resumes the same
            # native session; a mismatched sessionID is context_mismatch.
            "native_resume": CapabilityEntry.supported(
                "native_resume", evidence_id=evidence, cli_version=cli_version
            ),
            "native_state_export": CapabilityEntry.supported(
                "native_state_export", evidence_id=evidence, cli_version=cli_version
            ),
            "model_discovery": CapabilityEntry.supported(
                "model_discovery", evidence_id=evidence, cli_version=cli_version
            ),
            # `run` is a single invocation; usage tokens ride step_finish.
            "usage": CapabilityEntry.supported(
                "usage", evidence_id=evidence, cli_version=cli_version
            ),
            # Honest negatives for the one-shot transport.
            "steer": CapabilityEntry.unsupported(
                "steer", evidence_id=evidence, limitation="one-shot run; no interactive input"
            ),
            "interrupt": CapabilityEntry.unsupported(
                "interrupt", evidence_id=evidence, limitation="native cancel via supervisor stop"
            ),
            "interactive_approval": CapabilityEntry.unsupported(
                "interactive_approval", evidence_id=evidence, limitation="--auto bypasses approvals"
            ),
            "structured_output": CapabilityEntry.unsupported(
                "structured_output", evidence_id=evidence, limitation="no native schema enforcement"
            ),
            "credential_writeback": CapabilityEntry.unsupported(
                "credential_writeback", evidence_id=evidence, limitation="static API key"
            ),
        }
        merged = {c.name: c for c in caps}
        merged.update(known)
        return HarnessManifest(
            provider_id=PROVIDER_ID,
            adapter_version=ADAPTER_VERSION,
            cli_version=cli_version,
            transport="jsonl",
            support_tier="verified",
            capabilities=tuple(merged[n] for n in CAPABILITY_NAMES),
        )

    def prepare(self, context: TurnContext, credentials: CredentialBundle) -> PreparedHarness:
        home = self._state_root / "homes" / context.session_id
        data_home = home / ".local" / "share"
        config_home = home / ".config"
        data_home.mkdir(parents=True, exist_ok=True)
        config_home.mkdir(parents=True, exist_ok=True)
        (data_home / "opencode").mkdir(parents=True, exist_ok=True)
        (data_home / "opencode").chmod(0o700)
        written: list[str] = []
        for rel, content in credentials.files.items():
            target = home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            target.chmod(0o600)
            written.append(rel)
        auth = home / AUTH_REL
        if auth.is_file():
            auth.chmod(0o600)
        env = self._base_env()
        env.update(
            {
                "HOME": str(home),
                "XDG_DATA_HOME": str(data_home),
                "XDG_CONFIG_HOME": str(config_home),
            }
        )
        env.update(credentials.env)
        return PreparedHarness(
            home=home,
            xdg_data_home=data_home,
            xdg_config_home=config_home,
            env=env,
            provider_id=PROVIDER_ID,
            files_written=tuple(written),
        )

    def start_turn(self, context: TurnContext, prepared: PreparedHarness) -> NativeInvocation:
        argv = [*opencode_bin_tokens(), "run", context.prompt, "--format", "json"]
        if context.model:
            argv += ["-m", context.model]
        argv += ["--dir", str(context.worktree_root), "--auto"]
        return NativeInvocation(
            argv=tuple(argv),
            cwd=Path(context.worktree_root),
            env=dict(prepared.env),
        )

    def resume_turn(
        self,
        context: TurnContext,
        prepared: PreparedHarness,
        binding: NativeContextBinding,
    ) -> NativeInvocation:
        if binding is None or binding.provider_id != PROVIDER_ID or not binding.native_id:
            raise ContextMismatch("missing/incompatible native binding for opencode resume")
        invocation = self.start_turn(context, prepared)
        argv = list(invocation.argv)
        argv += ["--session", binding.native_id]
        state = NativeInvocation(
            argv=tuple(argv),
            cwd=invocation.cwd,
            env=invocation.env,
            stdin=invocation.stdin,
            transport=invocation.transport,
            kind=invocation.kind,
        )
        return state

    def normalize(self, frame: str, state: NormalizeState) -> list[Observation]:
        obj, bad = parse_line(frame)
        if bad or obj is None:
            return []
        now = time.time()
        observations: list[Observation] = []
        sid = self._session_marker(obj)
        if sid and not state.data.get("thread_seen"):
            expected = state.data.get("expected_native_id")
            if expected is not None and sid != expected:
                state.data["stale"] = True
                observations.append(
                    Observation(
                        kind=ObservationKind.TURN_FAILED,
                        native_session_id=sid,
                        error={
                            "code": WIRE_CONTEXT_MISMATCH,
                            "message": (
                                f'resume failed: session "{expected}" not found; '
                                f'opencode reported session "{sid}"'
                            ),
                        },
                        observed_at=now,
                    )
                )
                return observations
            state.data["thread_seen"] = True
            state.data["native_session_id"] = sid
            observations.append(
                Observation(
                    kind=ObservationKind.THREAD_STARTED,
                    native_session_id=sid,
                    observed_at=now,
                )
            )
        kind = obj.get("type")
        if state.data.get("stale"):
            if kind == "error":
                message = self._error_message(obj)
                observations.append(
                    Observation(
                        kind=ObservationKind.DIAGNOSTIC, error={"message": message}, observed_at=now
                    )
                )
            elif kind == "step_finish":
                observations.extend(self._on_step_finish(obj, state, now))
            return observations
        if kind == "step_start":
            observations.extend(self._ensure_turn_started(state, now))
        elif kind in ("reasoning", "text"):
            observations.extend(
                self._on_part(
                    obj, "reasoning" if kind == "reasoning" else "agent_message", state, now
                )
            )
        elif kind == "tool_use":
            observations.extend(self._on_tool_use(obj, state, now))
        elif kind == "step_finish":
            observations.extend(self._on_step_finish(obj, state, now))
        elif kind == "error":
            observations.extend(self._on_error(obj, state, now))
        else:
            observations.append(Observation(kind=ObservationKind.NOOP, observed_at=now))
        return observations

    def classify_outcome(self, evidence: ProcessEvidence) -> HarnessOutcome:
        completed = any(o.kind is ObservationKind.TURN_COMPLETED for o in evidence.observations)
        failed = [o for o in evidence.observations if o.kind is ObservationKind.TURN_FAILED]
        native_id = self._last_native_id(evidence.observations)
        binding = None
        if native_id:
            binding = NativeContextBinding(
                provider_id=PROVIDER_ID,
                native_id=native_id,
                lineage_id=native_id,
            )
        if completed:
            return HarnessOutcome(outcome=OutcomeKind.SUCCESS, native_binding=binding)
        if evidence.cancel_requested:
            return HarnessOutcome(
                outcome=OutcomeKind.INTERRUPTED,
                reason="cancel_requested",
                native_binding=binding,
            )
        for obs in failed:
            error = obs.error or {}
            code = str(error.get("code", ""))
            if code == WIRE_CONTEXT_MISMATCH:
                return HarnessOutcome(
                    outcome=OutcomeKind.FAILURE,
                    reason="context_mismatch",
                    native_binding=binding,
                )
            text = str(error.get("message", "")).lower()
            if any(n in text for n in _AUTH_NEEDLES):
                return HarnessOutcome(
                    outcome=OutcomeKind.FAILURE,
                    reason="credential_invalid",
                    credential_health="invalid",
                    retry_advice="manual",
                    native_binding=binding,
                )
            if any(n in text for n in _RATE_NEEDLES):
                return HarnessOutcome(
                    outcome=OutcomeKind.FAILURE,
                    reason="provider_error",
                    credential_health="rate_limited",
                    retry_advice="retry_later",
                    native_binding=binding,
                )
        tail = (evidence.stderr_tail or "").lower()
        if any(n in tail for n in _AUTH_NEEDLES):
            return HarnessOutcome(
                outcome=OutcomeKind.FAILURE,
                reason="credential_invalid",
                credential_health="invalid",
                retry_advice="manual",
                native_binding=binding,
            )
        if any(n in tail for n in _RATE_NEEDLES):
            return HarnessOutcome(
                outcome=OutcomeKind.FAILURE,
                reason="provider_error",
                credential_health="rate_limited",
                retry_advice="retry_later",
                native_binding=binding,
            )
        if evidence.timed_out:
            return HarnessOutcome(
                outcome=OutcomeKind.INTERRUPTED,
                reason="deadline_exceeded",
                retry_advice="none",
                native_binding=binding,
            )
        if evidence.exit_code is None:
            return HarnessOutcome(
                outcome=OutcomeKind.UNKNOWN,
                reason="outcome_unknown",
                retry_advice="reallocate",
                native_binding=binding,
            )
        return HarnessOutcome(
            outcome=OutcomeKind.FAILURE,
            reason="provider_error",
            detail={"exit_code": evidence.exit_code},
            native_binding=binding,
        )

    def discover(self, prepared: PreparedHarness) -> CapabilityObservation:
        try:
            proc = subprocess.run(
                [*opencode_bin_tokens(), "models"],
                env=prepared.env,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return CapabilityObservation(
                provider_id=PROVIDER_ID, source="cli", observed_at=_iso_now()
            )
        if proc.returncode != 0:
            return CapabilityObservation(
                provider_id=PROVIDER_ID, source="cli", observed_at=_iso_now()
            )
        models = tuple(
            line.strip() for line in proc.stdout.splitlines() if line.strip() and " " not in line
        )
        free = tuple(m for m in models if "free" in m or m in KNOWN_FREE_MODELS)
        return CapabilityObservation(
            provider_id=PROVIDER_ID,
            models=models,
            free_models=free,
            source="cli",
            observed_at=_iso_now(),
        )

    def interrupt(self, execution_id: str) -> None:
        raise UnsupportedCapability("interrupt", PROVIDER_ID)

    def steer(self, context: TurnContext, text: str) -> None:
        raise UnsupportedCapability("steer", PROVIDER_ID)

    def respond_approval(self, request_id: str, decision: str) -> None:
        raise UnsupportedCapability("interactive_approval", PROVIDER_ID)

    def export_native_state(
        self, binding: NativeContextBinding, prepared: PreparedHarness | None = None
    ) -> NativeStateManifest | None:
        home = prepared.home if prepared is not None else None
        if home is None:
            homes = list((self._state_root / "homes").glob("*"))
            home = homes[0] if homes else None
            if home is None:
                return None
        return native_state.export_allowlisted(
            root=home / ".local" / "share" / "opencode",
            prefixes=STATE_PREFIXES,
            provider_id=PROVIDER_ID,
            native_id=binding.native_id,
            lineage_id=binding.lineage_id,
            cli_version=binding.cli_version,
        )

    def validate_native_state(
        self, manifest: NativeStateManifest, prepared: PreparedHarness | None = None
    ) -> bool:
        home = prepared.home if prepared is not None else None
        if home is None:
            homes = list((self._state_root / "homes").glob("*"))
            if not homes:
                return False
            home = homes[0]
        return native_state.validate_manifest(manifest, home / ".local" / "share" / "opencode")

    def export_refreshed_credentials(self, base_version: str) -> CredentialBundle | None:
        # Static API keys MUST NOT write back.
        return None

    def release(self, prepared: PreparedHarness) -> list[str]:
        """Scrub credential files; native state under XDG data dir persists
        for verified resume."""
        failures: list[str] = []
        auth = prepared.home / AUTH_REL
        if auth.exists():
            try:
                auth.write_bytes(b"")
                auth.unlink()
            except OSError:
                failures.append(AUTH_REL)
        return failures

    # -- internals ----------------------------------------------------

    def _base_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TERM")}
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("TERM", "dumb")
        return env

    def _session_marker(self, obj: dict[str, Any]) -> str | None:
        sid = obj.get("sessionID")
        if isinstance(sid, str) and sid:
            return sid
        part = obj.get("part")
        if isinstance(part, dict):
            sid = part.get("sessionID")
            if isinstance(sid, str) and sid:
                return sid
        return None

    def _ensure_turn_started(self, state: NormalizeState, now: float) -> list[Observation]:
        if state.data.get("turn_started"):
            return []
        state.data["turn_started"] = True
        return [
            Observation(
                kind=ObservationKind.TURN_STARTED,
                native_session_id=state.data.get("native_session_id"),
                observed_at=now,
            )
        ]

    def _on_part(
        self, obj: dict, item_type: str, state: NormalizeState, now: float
    ) -> list[Observation]:
        part = obj.get("part")
        if not isinstance(part, dict):
            return [Observation(kind=ObservationKind.NOOP, observed_at=now)]
        part_id = part.get("id")
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return [Observation(kind=ObservationKind.NOOP, observed_at=now)]
        key = str(part_id) if part_id is not None else f"{item_type}:{state.item_seq}"
        parts: dict[str, tuple[str, str]] = state.data.setdefault("parts", {})
        order: list[str] = state.data.setdefault("part_order", [])
        if key not in parts:
            order.append(key)
        parts[key] = (item_type, text)  # cumulative snapshot: latest wins
        return self._ensure_turn_started(state, now) or [
            Observation(kind=ObservationKind.NOOP, observed_at=now)
        ]

    def _flush_parts(self, state: NormalizeState, now: float) -> list[Observation]:
        parts: dict[str, tuple[str, str]] = state.data.get("parts", {})
        order: list[str] = state.data.get("part_order", [])
        observations = [
            Observation(
                kind=ObservationKind.ITEM_COMPLETED,
                native_session_id=state.data.get("native_session_id"),
                item={"id": key, "type": item_type, "status": "completed", "text": text},
                observed_at=now,
            )
            for key, (item_type, text) in ((key, parts[key]) for key in order)
        ]
        state.data["parts"] = {}
        state.data["part_order"] = []
        return observations

    def _tool_state(self, obj: dict) -> tuple[str, str, dict, dict]:
        part = obj.get("part")
        if not isinstance(part, dict):
            part = {}
        state = part.get("state")
        if not isinstance(state, dict):
            state = {}
        call_id = part.get("callID") or part.get("id")
        tool = str(part.get("tool") or state.get("tool") or "")
        raw_input = state.get("input")
        if not isinstance(raw_input, dict):
            raw_input = {}
        return str(call_id or ""), tool, state, raw_input

    def _tool_item(
        self, item_id: str, tool: str, raw_input: dict, state: dict, *, status: str
    ) -> dict:
        if tool in _FILE_TOOLS:
            path = (
                raw_input.get("filePath")
                or raw_input.get("file_path")
                or raw_input.get("path")
                or raw_input.get("TargetFile")
                or ""
            )
            return {
                "id": item_id,
                "type": "file_change",
                "changes": [{"path": str(path), "kind": "update"}],
                "status": status,
            }
        command = raw_input.get("command") or raw_input.get("CommandLine")
        if not command:
            title = state.get("title")
            command = title if isinstance(title, str) and title else tool
        if not command and raw_input:
            import json as _json

            command = _json.dumps(raw_input, ensure_ascii=False)
        output = state.get("output")
        metadata = state.get("metadata")
        exit_code = metadata.get("exit") if isinstance(metadata, dict) else None
        return {
            "id": item_id,
            "type": "command_execution",
            "command": str(command or ""),
            "aggregated_output": output if isinstance(output, str) else "",
            "status": status,
            "exit_code": exit_code if isinstance(exit_code, int) else None,
        }

    def _on_tool_use(self, obj: dict, state: NormalizeState, now: float) -> list[Observation]:
        call_id, tool, tool_state, raw_input = self._tool_state(obj)
        status = str(tool_state.get("status") or "")
        item_id = call_id or state.next_item_id()
        open_tools: dict = state.data.setdefault("open_tools", {})
        done_tools: set = state.data.setdefault("done_tools", set())
        if item_id in done_tools:
            return [Observation(kind=ObservationKind.NOOP, observed_at=now)]
        sid = state.data.get("native_session_id")
        opened = item_id in open_tools
        if status not in _TERMINAL_TOOL_STATUSES:
            if opened:
                return [Observation(kind=ObservationKind.NOOP, observed_at=now)]
            observations = self._ensure_turn_started(state, now) + self._flush_parts(state, now)
            open_tools[item_id] = tool
            observations.append(
                Observation(
                    kind=ObservationKind.ITEM_STARTED,
                    native_session_id=sid,
                    item=self._tool_item(
                        item_id, tool, raw_input, tool_state, status="in_progress"
                    ),
                    observed_at=now,
                )
            )
            return observations
        observations = self._ensure_turn_started(state, now) + self._flush_parts(state, now)
        done_tools.add(item_id)
        open_tools.pop(item_id, None)
        if not opened:
            observations.append(
                Observation(
                    kind=ObservationKind.ITEM_STARTED,
                    native_session_id=sid,
                    item=self._tool_item(
                        item_id, tool, raw_input, tool_state, status="in_progress"
                    ),
                    observed_at=now,
                )
            )
        observations.append(
            Observation(
                kind=ObservationKind.ITEM_COMPLETED,
                native_session_id=sid,
                item=self._tool_item(
                    item_id,
                    tool,
                    raw_input,
                    tool_state,
                    status="completed" if status == "completed" else "failed",
                ),
                observed_at=now,
            )
        )
        return observations

    def _on_step_finish(self, obj: dict, state: NormalizeState, now: float) -> list[Observation]:
        part = obj.get("part")
        if not isinstance(part, dict):
            part = {}
        self._add_tokens(state, part.get("tokens"))
        observations = self._flush_parts(state, now)
        reason = str(part.get("reason") or "")
        if reason == _STEP_CONTINUE or state.data.get("turn_terminal"):
            return observations or [Observation(kind=ObservationKind.NOOP, observed_at=now)]
        state.data["turn_terminal"] = True
        sid = state.data.get("native_session_id")
        if state.data.get("stale"):
            observations.append(
                Observation(
                    kind=ObservationKind.TURN_FAILED,
                    native_session_id=sid,
                    error={
                        "code": WIRE_CONTEXT_MISMATCH,
                        "message": (
                            f'resume failed: session "{state.data.get("expected_native_id")}" '
                            "not found"
                        ),
                    },
                    observed_at=now,
                )
            )
        elif reason in ("", "stop"):
            observations.append(
                Observation(
                    kind=ObservationKind.TURN_COMPLETED,
                    native_session_id=sid,
                    usage=self._usage(state),
                    observed_at=now,
                )
            )
        else:
            observations.append(
                Observation(
                    kind=ObservationKind.TURN_FAILED,
                    native_session_id=sid,
                    error={
                        "code": WIRE_PROVIDER_ERROR,
                        "message": f"opencode turn ended with reason {reason}",
                    },
                    observed_at=now,
                )
            )
        return observations

    def _on_error(self, obj: dict, state: NormalizeState, now: float) -> list[Observation]:
        observations = self._ensure_turn_started(state, now) + self._flush_parts(state, now)
        message = self._error_message(obj)
        code = WIRE_PROVIDER_ERROR
        lowered = message.lower()
        if any(n in lowered for n in _AUTH_NEEDLES):
            code = WIRE_CREDENTIAL_INVALID
        elif any(n in lowered for n in _RATE_NEEDLES):
            code = WIRE_RATE_LIMITED
        state.data["turn_terminal"] = True
        observations.append(
            Observation(
                kind=ObservationKind.TURN_FAILED,
                native_session_id=state.data.get("native_session_id"),
                error={"code": code, "message": message},
                observed_at=now,
            )
        )
        return observations

    @staticmethod
    def _error_message(obj: dict) -> str:
        err = obj.get("error")
        if isinstance(err, dict):
            data = err.get("data")
            if isinstance(data, dict):
                msg = data.get("message")
                if isinstance(msg, str) and msg:
                    return msg
            msg = err.get("message")
            if isinstance(msg, str) and msg:
                return msg
            import json as _json

            return _json.dumps(err, ensure_ascii=False)
        if isinstance(err, str) and err:
            return err
        msg = obj.get("message")
        if isinstance(msg, str) and msg:
            return msg
        return "opencode turn failed"

    @staticmethod
    def _add_tokens(state: NormalizeState, tokens: Any) -> None:
        if not isinstance(tokens, dict):
            return
        acc: dict = state.data.setdefault("usage", {})
        acc["input_tokens"] = acc.get("input_tokens", 0) + _as_int(tokens.get("input"))
        acc["output_tokens"] = acc.get("output_tokens", 0) + _as_int(tokens.get("output"))
        acc["reasoning_output_tokens"] = acc.get("reasoning_output_tokens", 0) + _as_int(
            tokens.get("reasoning")
        )
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            acc["cached_input_tokens"] = acc.get("cached_input_tokens", 0) + _as_int(
                cache.get("read")
            )
            acc["cache_write_input_tokens"] = acc.get("cache_write_input_tokens", 0) + _as_int(
                cache.get("write")
            )

    @staticmethod
    def _usage(state: NormalizeState) -> Usage | None:
        acc: dict = state.data.get("usage", {})
        if not acc:
            return None
        return Usage(
            input_tokens=acc.get("input_tokens"),
            cached_input_tokens=acc.get("cached_input_tokens"),
            cache_write_input_tokens=acc.get("cache_write_input_tokens"),
            output_tokens=acc.get("output_tokens"),
            reasoning_output_tokens=acc.get("reasoning_output_tokens"),
            source="cli",
        )

    @staticmethod
    def _last_native_id(observations: tuple[Observation, ...]) -> str | None:
        for obs in observations:
            if obs.native_session_id:
                return obs.native_session_id
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _iso_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.UTC).isoformat()


def homes_cleanup(state_root: Path) -> None:
    shutil.rmtree(Path(state_root) / "homes", ignore_errors=True)
