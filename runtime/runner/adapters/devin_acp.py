"""Internal ACP stdio bridge for the Devin CLI (SOR-72).

Spawned by ``DevinAdapter`` as::

    python -m runtime.runner.adapters.devin_acp [--resume SID] [--model M] -- PROMPT

The pinned Devin CLI's ``-p`` print mode exposes neither a session id nor a
structured event stream, so this bridge drives the official Agent Client
Protocol server (``devin acp``, JSON-RPC over stdio) and re-emits the native
NDJSON line protocol consumed by ``DevinAdapter.translate``:

    session.started / turn.started / reasoning / assistant_message /
    tool_call / tool_result / turn.completed / turn.failed / error

External runner contracts are unchanged: the bridge reads its prompt from
argv, takes stdin as ``/dev/null`` (it owns the child's stdio), writes NDJSON
to stdout, relays the child's stderr to its own stderr, and exits 0 on a
completed turn / nonzero otherwise. On SIGTERM/SIGINT it kills the
``devin acp`` process group so ``runner turn`` soft-timeout and
``runner stop`` behave like the other providers.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from typing import Any

from runtime.runner.adapters.devin import devin_bin_tokens

# Credential/key material that must never reach `devin acp`: the restored
# ~/.local/share/devin/credentials.toml file is the only auth source, and
# ACP_BACKEND must not change the server's credential policy.
_CHILD_ENV_DENYLIST = (
    "ACP_BACKEND",
    "DEVIN_API_KEY",
    "DEVIN_V3_API_KEY",
    "DEVIN_LEGACY_API_KEY",
    "DEVIN_ORG_ID",
    "DEVIN_MODEL",
    "DEVIN_REFUSAL_FALLBACK",
    "WINDSURF_API_KEY",
    "SBX_ACCOUNT_CREDENTIAL",
)

# Update types that carry no canonical meaning (mode/config/command palette
# noise and session-load history replay handled separately).
_SKIP_UPDATES = {
    "user_message_chunk",
    "session_info_update",
    "config_option_update",
    "current_mode_update",
    "available_commands_update",
    "plan",
    "usage_update",  # token fields tracked for the final turn.completed
}


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _CHILD_ENV_DENYLIST}


def _content_text(content: Any) -> str:
    """Flatten ACP ``content`` (single block dict or block array) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        inner = content.get("content")
        if isinstance(inner, dict):
            resource = inner.get("resource")
            if isinstance(resource, dict) and isinstance(resource.get("text"), str):
                return resource["text"]
            if isinstance(inner.get("text"), str):
                return inner["text"]
        return ""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, dict):
            resource = inner.get("resource")
            if isinstance(resource, dict) and isinstance(resource.get("text"), str):
                parts.append(resource["text"])
            elif isinstance(inner.get("text"), str):
                parts.append(inner["text"])
        elif isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


class AcpBridge:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self._next_id = 0
        self._tools: dict[str, dict[str, Any]] = {}
        self._usage: dict[str, Any] = {}
        self._buf_kind: str | None = None
        self._buf_text: list[str] = []
        self.prompting = False

    # -- native event emission ---------------------------------------------

    def _flush_buffer(self) -> None:
        if not self._buf_kind:
            return
        text = "".join(self._buf_text)
        kind = self._buf_kind
        self._buf_kind = None
        self._buf_text = []
        if text:
            _emit({"type": kind, "text": text})

    def _note_chunk(self, kind: str, text: str) -> None:
        if self._buf_kind != kind:
            self._flush_buffer()
            self._buf_kind = kind
        self._buf_text.append(text)

    def _on_update(self, params: dict[str, Any]) -> None:
        if not self.prompting:
            return  # session/load replays history before we prompt; ignore it
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self._note_chunk("assistant_message", _content_text(update.get("content")))
            return
        if kind == "agent_thought_chunk":
            self._note_chunk("reasoning", _content_text(update.get("content")))
            return
        if kind == "usage_update":
            meta = update.get("_meta")
            if isinstance(meta, dict):
                self._usage = {
                    key: meta[key]
                    for key in (
                        "cognition.ai/inputTokens",
                        "cognition.ai/outputTokens",
                        "cognition.ai/cachedReadTokens",
                        "cognition.ai/cacheWriteTokens",
                    )
                    if key in meta
                }
            return
        if kind == "tool_call":
            self._flush_buffer()
            tool_id = str(update.get("toolCallId") or "")
            meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
            tool_name = str(meta.get("cognition.ai/inferenceToolName") or "")
            call_kind = str(update.get("kind") or "")
            raw_input = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
            title = str(update.get("title") or "")
            self._tools[tool_id] = {
                "tool": tool_name or call_kind,
                "kind": call_kind,
                "input": raw_input,
                "title": title,
            }
            _emit(
                {
                    "type": "tool_call",
                    "id": tool_id,
                    "tool": tool_name or call_kind,
                    "kind": call_kind,
                    "input": raw_input,
                    "title": title,
                }
            )
            return
        if kind == "tool_call_update":
            tool_id = str(update.get("toolCallId") or "")
            status = str(update.get("status") or "")
            if status not in ("completed", "failed"):
                return
            self._flush_buffer()
            meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
            call = self._tools.get(tool_id, {})
            exit_info = meta.get("terminal_exit")
            exit_code = exit_info.get("exit_code") if isinstance(exit_info, dict) else None
            if status == "failed" and exit_code is None:
                exit_code = 1
            _emit(
                {
                    "type": "tool_result",
                    "id": tool_id,
                    "tool": call.get("tool", ""),
                    "kind": call.get("kind", ""),
                    "input": call.get("input", {}),
                    "status": status,
                    "exit_code": exit_code,
                    "output": _content_text(update.get("content")),
                }
            )
            return
        if kind in _SKIP_UPDATES:
            return
        # Unknown update kinds are ignored (forward-compatible).

    # -- JSON-RPC plumbing ---------------------------------------------------

    def _send(self, method: str, params: dict[str, Any]) -> int:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return self._next_id

    def _answer_request(self, msg: dict[str, Any]) -> None:
        """Answer an agent->client request (permissions auto-allowed)."""
        assert self.proc.stdin is not None
        method = msg.get("method")
        if method == "session/request_permission":
            options = (msg.get("params") or {}).get("options") or []
            option = next(
                (
                    opt
                    for opt in options
                    if isinstance(opt, dict) and "allow" in str(opt.get("kind", ""))
                ),
                options[0] if options else {},
            )
            reply = {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {
                    "outcome": {"outcome": "selected", "optionId": option.get("optionId", "allow")}
                },
            }
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32601, "message": f"unsupported client method {method}"},
            }
        self.proc.stdin.write(json.dumps(reply) + "\n")
        self.proc.stdin.flush()

    def _pump(self, request_id: int) -> dict[str, Any] | None:
        """Read messages until the response for ``request_id`` arrives."""
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None  # child exited / closed stdout
            stripped = line.strip()
            if not stripped:
                continue
            try:
                msg = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg and "method" in msg:
                try:
                    self._answer_request(msg)
                except (BrokenPipeError, OSError):
                    return None
                continue
            if "method" in msg:
                if msg["method"] == "session/update":
                    params = msg.get("params")
                    if isinstance(params, dict):
                        self._on_update(params)
                continue
            if msg.get("id") == request_id:
                return msg


def _killpg(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


def _term_child(proc: subprocess.Popen[str]) -> None:
    """SIGTERM the ``devin acp`` process group so it can flush session state."""
    _killpg(proc, signal.SIGTERM)


def _kill_child(proc: subprocess.Popen[str]) -> None:
    _killpg(proc, signal.SIGKILL)


def _shutdown_child(proc: subprocess.Popen[str], grace_s: float = 3.0) -> None:
    _term_child(proc)
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    _kill_child(proc)
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def run_bridge(*, prompt: str, model: str, resume_id: str | None) -> int:
    env = _child_env()
    work = os.environ.get("SBX_WORK") or os.getcwd()
    argv = [*devin_bin_tokens(), "acp"]
    if model:
        argv += ["--model", model]
    try:
        proc = subprocess.Popen(
            argv,
            cwd=work,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"failed to start devin acp: {exc}", file=sys.stderr)
        _emit({"type": "error", "error": {"code": "spawn_failed", "message": str(exc)}})
        return 1

    def _relay_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    threading.Thread(target=_relay_stderr, daemon=True, name="acp-stderr").start()

    def _on_term(_signum: int, _frame: object) -> None:
        _kill_child(proc)
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    bridge = AcpBridge(proc)

    def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            request_id = bridge._send(method, params)
        except (BrokenPipeError, OSError) as exc:
            print(f"devin acp write failed: {exc}", file=sys.stderr)
            return None
        return bridge._pump(request_id)

    try:
        init = _rpc(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            },
        )
        if not init or "error" in init:
            message = (init or {}).get("error", {}).get("message", "initialize failed")
            print(f"devin acp initialize failed: {message}", file=sys.stderr)
            _emit({"type": "error", "error": {"code": "initialize", "message": str(message)}})
            return 1

        if resume_id:
            session = _rpc("session/load", {"sessionId": resume_id, "cwd": work, "mcpServers": []})
        else:
            session = _rpc("session/new", {"cwd": work, "mcpServers": []})
        if not session or "error" in session:
            message = (session or {}).get("error", {}).get("message", "session setup failed")
            print(f"devin acp session setup failed: {message}", file=sys.stderr)
            _emit({"type": "error", "error": {"code": "session", "message": str(message)}})
            return 1

        result = session.get("result") or {}
        session_id = result.get("sessionId") or resume_id
        if not session_id:
            print("devin acp returned no sessionId", file=sys.stderr)
            _emit(
                {
                    "type": "error",
                    "error": {"code": "session", "message": "no sessionId in session response"},
                }
            )
            return 1
        _emit(
            {
                "type": "session.started",
                "session_id": session_id,
                "model": model or "",
                "resumed": bool(resume_id),
            }
        )

        modes = (result.get("modes") or {}).get("availableModes") or []
        if any(m.get("id") == "bypass" for m in modes if isinstance(m, dict)):
            _rpc("session/set_mode", {"sessionId": session_id, "modeId": "bypass"})

        _emit({"type": "turn.started"})
        bridge.prompting = True
        reply = _rpc(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
        )
        bridge._flush_buffer()
        if not reply or "error" in reply:
            message = (reply or {}).get("error", {}).get("message", "prompt failed")
            print(f"devin acp prompt failed: {message}", file=sys.stderr)
            _emit({"type": "turn.failed", "error": {"message": str(message)}})
            return 1
        prompt_result = reply.get("result") or {}
        stop_reason = prompt_result.get("stopReason")
        if stop_reason != "end_turn":
            _emit(
                {
                    "type": "turn.failed",
                    "error": {"message": f"prompt ended with stopReason {stop_reason!r}"},
                }
            )
            return 1
        usage: dict[str, Any] = {}
        raw_usage = prompt_result.get("usage")
        if isinstance(raw_usage, dict):
            usage = {
                "input_tokens": raw_usage.get("inputTokens", 0),
                "output_tokens": raw_usage.get("outputTokens", 0),
                "cache_read_tokens": raw_usage.get("cachedReadTokens", 0),
            }
            if raw_usage.get("cacheWriteTokens") is not None:
                usage["cache_write_tokens"] = raw_usage["cacheWriteTokens"]
        elif bridge._usage:
            usage = {
                "input_tokens": bridge._usage.get("cognition.ai/inputTokens", 0),
                "output_tokens": bridge._usage.get("cognition.ai/outputTokens", 0),
                "cache_read_tokens": bridge._usage.get("cognition.ai/cachedReadTokens", 0),
            }
        _emit({"type": "turn.completed", "status": "success", "usage": usage})
        return 0
    finally:
        _shutdown_child(proc)


def main(argv: list[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    prompt = ""
    if "--" in tokens:
        idx = tokens.index("--")
        prompt = " ".join(tokens[idx + 1 :])
        tokens = tokens[:idx]
    parser = argparse.ArgumentParser(prog="devin_acp")
    parser.add_argument("--model", default="")
    parser.add_argument("--resume", default=None)
    args, _unknown = parser.parse_known_args(tokens)
    return run_bridge(prompt=prompt, model=args.model, resume_id=args.resume)


if __name__ == "__main__":
    raise SystemExit(main())
