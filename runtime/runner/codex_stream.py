"""Codex app-server stdio transport emitting the existing runner event shape.

Text deltas become cumulative item updates immediately. No invented chunks and
no automatic re-execution after a failed start (a turn may already have effects).
The frozen CodexAdapter remains the exec transport and canonical translator.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from typing import Any

from runtime.runner.codex import codex_bin_tokens
from runtime.runner.events import redact_obj, redact_text


def emit(event: dict[str, Any]) -> None:
    safe = redact_obj(event)
    print(json.dumps(safe, ensure_ascii=False), flush=True)
    if event.get("type") == "turn.failed":
        # Frozen CodexAdapter classifies account health from stderr.
        print(redact_text(json.dumps(safe.get("error", {}))), file=sys.stderr, flush=True)


class Stream:
    def __init__(self, thread_id: str | None = None) -> None:
        self.thread_id = thread_id
        self.turn_id: str | None = None
        self.items: dict[str, dict[str, Any]] = {}
        self.usage: dict[str, int] = {}
        self.completed = False
        self.ok = False

    def item(self, native: dict[str, Any]) -> dict[str, Any] | None:
        ident = native.get("id")
        kind = native.get("type")
        if not isinstance(ident, str):
            return None
        old = self.items.get(ident, {})
        if kind == "agentMessage":
            item = {"id": ident, "type": "agent_message", "text": native.get("text", "")}
        elif kind == "reasoning":
            parts = native.get("summary") or native.get("content") or []
            item = {"id": ident, "type": "reasoning", "text": "\n".join(parts)}
        elif kind == "commandExecution":
            item = {
                "id": ident,
                "type": "command_execution",
                "command": native.get("command", ""),
                "aggregated_output": native.get("aggregatedOutput") or "",
                "exit_code": native.get("exitCode"),
            }
        elif kind == "fileChange":
            changes = []
            for change in native.get("changes", []):
                raw_kind = change.get("kind", {})
                op = raw_kind.get("type", "update") if isinstance(raw_kind, dict) else raw_kind
                changes.append(
                    {
                        "path": change.get("path", ""),
                        "kind": {"add": "added", "update": "modified", "delete": "deleted"}.get(
                            op, op
                        ),
                    }
                )
            item = {"id": ident, "type": "file_change", "changes": changes}
        elif kind == "mcpToolCall":
            item = {
                "id": ident,
                "type": "mcp_tool_call",
                "server": native.get("server", ""),
                "tool": native.get("tool", ""),
                "result": native.get("result"),
                "error": native.get("error"),
            }
        else:
            return None
        # Empty terminal snapshots must not erase text already delivered.
        if old.get("text") and not item.get("text"):
            item["text"] = old["text"]
        if old.get("aggregated_output") and not item.get("aggregated_output"):
            item["aggregated_output"] = old["aggregated_output"]
        item["status"] = native.get("status", "in_progress")
        self.items[ident] = item
        return item

    def notification(self, method: str, params: dict[str, Any]) -> None:
        if self.thread_id and params.get("threadId") not in (None, self.thread_id):
            return
        incoming_turn = params.get("turnId") or params.get("turn", {}).get("id")
        if self.turn_id and incoming_turn and incoming_turn != self.turn_id:
            return
        if method == "turn/started":
            self.turn_id = incoming_turn
        if method in {"item/started", "item/completed"}:
            item = self.item(params.get("item", {}))
            if item is not None:
                done = method == "item/completed"
                if done and item["status"] == "in_progress":
                    item["status"] = "completed"
                emit({"type": "item.completed" if done else "item.started", "item": item})
        elif method in {
            "item/agentMessage/delta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
            "item/commandExecution/outputDelta",
        }:
            ident = params.get("itemId")
            delta = params.get("delta")
            if not isinstance(ident, str) or not isinstance(delta, str):
                return
            command = method == "item/commandExecution/outputDelta"
            kind = (
                "command_execution"
                if command
                else ("agent_message" if method == "item/agentMessage/delta" else "reasoning")
            )
            field = "aggregated_output" if command else "text"
            item = self.items.setdefault(ident, {"id": ident, "type": kind})
            item[field] = item.get(field, "") + delta
            item["status"] = "in_progress"
            emit({"type": "item.updated", "item": item})
        elif method == "thread/tokenUsage/updated":
            last = params.get("tokenUsage", {}).get("last", {})
            self.usage = {
                "input_tokens": last.get("inputTokens", 0),
                "cached_input_tokens": last.get("cachedInputTokens", 0),
                "output_tokens": last.get("outputTokens", 0),
                "reasoning_output_tokens": last.get("reasoningOutputTokens", 0),
            }
        elif method == "turn/completed":
            turn = params.get("turn", {})
            self.completed = True
            self.ok = turn.get("status") == "completed"
            if self.ok:
                emit({"type": "turn.completed", "usage": self.usage})
            else:
                emit(
                    {
                        "type": "turn.failed",
                        "error": turn.get("error")
                        or {"message": f"Codex turn {turn.get('status', 'failed')}"},
                    }
                )


def run(prompt: str, model: str, resume: str) -> int:
    proc = subprocess.Popen(
        [*codex_bin_tokens(), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        start_new_session=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    seq = 0

    def send(obj: dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def request(method: str, params: dict[str, Any]) -> int:
        nonlocal seq
        seq += 1
        send({"jsonrpc": "2.0", "id": seq, "method": method, "params": params})
        return seq

    def receive() -> dict[str, Any]:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("Codex app-server closed before turn completion")
        message = json.loads(line)
        if "method" in message and "id" in message:
            # approvalPolicy=never: unexpected interactive requests cannot hang.
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Interactive requests are unavailable"},
                }
            )
        return message

    def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
        ident = request(method, params)
        while True:
            msg = receive()
            if msg.get("id") == ident and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})

    def stop(_sig: int, _frame: object) -> None:
        raise InterruptedError("Codex transport interrupted")

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        rpc("initialize", {"clientInfo": {"name": "sbx_runner", "version": "0.1.0"}})
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        params: dict[str, Any] = {
            "cwd": os.getcwd(),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if model:
            params["model"] = model
        if resume:
            params["threadId"] = resume
        result = rpc("thread/resume" if resume else "thread/start", params)
        thread = result.get("thread", {}).get("id")
        if not isinstance(thread, str) or not thread:
            raise RuntimeError("Codex app-server did not return a thread id")
        emit({"type": "thread.started", "thread_id": thread})
        emit({"type": "turn.started"})
        ident = request(
            "turn/start",
            {
                "threadId": thread,
                "input": [{"type": "text", "text": prompt}],
                "cwd": os.getcwd(),
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            },
        )
        stream = Stream(thread)
        while not stream.completed:
            msg = receive()
            if msg.get("id") == ident and "error" in msg:
                raise RuntimeError(str(msg["error"]))
            if msg.get("id") == ident and "result" in msg:
                stream.turn_id = msg["result"].get("turn", {}).get("id") or stream.turn_id
            stream.notification(msg.get("method", ""), msg.get("params", {}))
        return 0 if stream.ok else 1
    except (OSError, ValueError, RuntimeError, InterruptedError) as exc:
        emit({"type": "turn.failed", "error": {"message": str(exc)}})
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="")
    parser.add_argument("--resume", default="")
    parser.add_argument("prompt")
    args = parser.parse_args()
    return run(args.prompt, args.model, args.resume)


if __name__ == "__main__":
    raise SystemExit(main())
