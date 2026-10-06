#!/usr/bin/env python3
"""Deterministic stand-in for the official ``opencode`` CLI (argv + JSON frame shapes
recorded from opencode-ai 1.18.34). Directives inside the prompt control behavior:

``[hang]`` block until killed · ``[slow:N]`` sleep N s mid-stream · ``[fail]`` provider
error + exit 1 · ``[write:path=text]`` write a file under --dir · ``[recall]`` reply with
all earlier prompts of the native session · ``[flood:N]`` emit N text frames ·
``[result:{json}]`` reply with a fenced JSON result block.
Native sessions persist in ``$XDG_DATA_HOME/opencode/opencode.db`` (JSON here).
An ``auth.json`` key of ``invalid`` yields the real 401-style error frame.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    args = sys.argv[1:]
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "opencode"
    if args[:1] == ["--version"]:
        print("1.18.34-fake")
        return 0
    if args[:2] == ["models", "opencode"]:
        print("opencode/big-pickle\nopencode/gpt-5.4-mini\nopencode/mimo-v2.5-free")
        return 0
    if args[:1] != ["run"]:
        print("unknown command", file=sys.stderr)
        return 2
    prompt = args[1]
    opts = dict(zip(args[2::2], args[3::2], strict=False))
    workdir = Path(opts.get("--dir", "."))
    auth = data / "auth.json"
    db = data / "opencode.db"
    sessions = json.loads(db.read_text()) if db.exists() else {}
    requested = opts.get("--session") or opts.get("-s")
    if requested and requested not in sessions:
        print(f"Error: Session not found: {requested}", file=sys.stderr)
        return 1
    sid = requested or f"ses_{uuid.uuid4().hex[:20]}"
    msg = f"msg_{uuid.uuid4().hex[:12]}"
    key = json.loads(auth.read_text())["opencode"]["key"] if auth.exists() else None
    if not key or key.startswith("invalid"):
        emit(
            {
                "type": "error",
                "sessionID": sid,
                "error": {
                    "name": "APIError",
                    "data": {"message": "Unauthorized: invalid api key (401)"},
                },
            }
        )
        return 1
    history = sessions.get(sid, [])
    sessions[sid] = history + [prompt]
    data.mkdir(parents=True, exist_ok=True)
    db.write_text(json.dumps(sessions))
    emit(
        {
            "type": "step_start",
            "sessionID": sid,
            "part": {
                "id": f"prt_{uuid.uuid4().hex[:8]}",
                "sessionID": sid,
                "messageID": msg,
                "type": "step-start",
            },
        }
    )
    if "[fail]" in prompt:
        emit(
            {
                "type": "error",
                "sessionID": sid,
                "error": {"name": "APIError", "data": {"message": "model overloaded upstream"}},
            }
        )
        return 1
    for match in re.finditer(r"\[write:([^=\]]+)=([^\]]*)\]", prompt):
        path, text = match.group(1), match.group(2)
        call = f"call_{uuid.uuid4().hex[:8]}"
        base = {
            "type": "tool_use",
            "sessionID": sid,
            "part": {
                "type": "tool",
                "tool": "write",
                "callID": call,
                "id": f"prt_{call}",
                "sessionID": sid,
                "messageID": msg,
            },
        }
        base["part"]["state"] = {
            "status": "running",
            "input": {"filePath": str(workdir / path), "content": text},
        }
        emit(base)
        target = workdir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n")
        base["part"]["state"] = {
            "status": "completed",
            "input": {"filePath": str(workdir / path), "content": text},
            "output": "Wrote file successfully.",
            "title": path,
        }
        emit(base)
    slow = re.search(r"\[slow:(\d+(?:\.\d+)?)\]", prompt)
    if slow:
        time.sleep(float(slow.group(1)))
    if "[hang]" in prompt:
        while True:
            time.sleep(1)
    flood = re.search(r"\[flood:(\d+)\]", prompt)
    for i in range(int(flood.group(1)) if flood else 0):
        emit(
            {
                "type": "text",
                "sessionID": sid,
                "part": {"id": f"flood-{i}", "type": "text", "text": f"line {i}", "sessionID": sid},
            }
        )
    reply = f"ACK: {prompt}"
    if "[recall]" in prompt:
        reply += "\nEARLIER: " + " | ".join(history)
    result = re.search(r"\[result:(\{.*\})\]", prompt)
    if result:
        reply += "\n```json\n" + result.group(1) + "\n```"
    part_id = f"prt_{uuid.uuid4().hex[:8]}"
    half = max(1, len(reply) // 2)
    emit(
        {
            "type": "text",
            "sessionID": sid,
            "part": {
                "id": part_id,
                "type": "text",
                "text": reply[:half],
                "sessionID": sid,
                "messageID": msg,
            },
        }
    )
    emit(
        {
            "type": "text",
            "sessionID": sid,
            "part": {
                "id": part_id,
                "type": "text",
                "text": reply,
                "sessionID": sid,
                "messageID": msg,
            },
        }
    )
    emit(
        {
            "type": "step_finish",
            "sessionID": sid,
            "part": {
                "id": f"prt_{uuid.uuid4().hex[:8]}",
                "reason": "stop",
                "type": "step-finish",
                "sessionID": sid,
                "tokens": {
                    "input": 120,
                    "output": 12,
                    "reasoning": 3,
                    "cache": {"read": 40, "write": 0},
                },
                "cost": 0,
            },
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
