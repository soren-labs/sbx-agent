#!/usr/bin/env python3
"""Fake ``devin acp`` JSON-RPC stdio server for bridge tests (SOR-72).

Implements the slice of ACP the bridge uses: ``initialize``,
``session/new``, ``session/load`` (with history replay), ``session/set_mode``,
``session/prompt`` (session/update notifications + stopReason result).

Scenarios via ``FAKE_ACP_SCENARIO``: success, resume, nonzero, hang,
badjson, slow, auth_invalid. ``FAKE_ACP_SLOW_SECONDS`` overrides the slow
pause (default 40). ``FAKE_ACP_SESSION_ID`` overrides the session id.
``FAKE_ACP_SPY_OUT`` writes a JSON dump of interesting env/argv facts.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

SCENARIO = os.environ.get("FAKE_ACP_SCENARIO", "success")
SESSION_ID = os.environ.get("FAKE_ACP_SESSION_ID", "fake-acp-session-01")
MODES = {
    "currentModeId": "accept-edits",
    "availableModes": [{"id": "accept-edits"}, {"id": "bypass"}],
}


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _reply(rid: object, result: dict) -> None:
    _emit({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid: object, code: int, message: str) -> None:
    _emit({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _update(update: dict) -> None:
    _emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": SESSION_ID, "update": update},
        }
    )


def _term(_signum: int, _frame: object) -> None:
    sys.exit(0)


def _run_prompt(rid: object) -> None:
    if SCENARIO == "hang":
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
        time.sleep(3600)
        return
    if SCENARIO == "badjson":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
    if SCENARIO == "nonzero":
        _error(rid, -32000, "prompt exploded")
        return
    if SCENARIO == "slow":
        _update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "slow start"},
            }
        )
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
        time.sleep(float(os.environ.get("FAKE_ACP_SLOW_SECONDS", "40")))
        signal.signal(signal.SIGTERM, _term)
        _reply(rid, {"stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1}})
        return
    _update(
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "thinking about it"},
        }
    )
    _update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "exec:0",
            "kind": "execute",
            "title": "Ran echo",
            "rawInput": {"command": "echo hi"},
            "_meta": {"cognition.ai/inferenceToolName": "exec"},
        }
    )
    _update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "exec:0",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": "hi"}}],
            "_meta": {"terminal_exit": {"exit_code": 0}},
        }
    )
    _update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "exec:0",
            "status": "completed",
        }
    )
    _update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Hello "},
        }
    )
    _update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "world"},
        }
    )
    _update(
        {
            "sessionUpdate": "usage_update",
            "used": 100,
            "size": 262000,
            "_meta": {
                "cognition.ai/inputTokens": 90,
                "cognition.ai/outputTokens": 10,
                "cognition.ai/cachedReadTokens": 50,
            },
        }
    )
    _reply(
        rid,
        {
            "stopReason": "end_turn",
            "usage": {
                "totalTokens": 100,
                "inputTokens": 90,
                "outputTokens": 10,
                "cachedReadTokens": 50,
            },
        },
    )


def main() -> None:
    pid_file = os.environ.get("FAKE_ACP_PID_FILE")
    if pid_file:
        Path(pid_file).write_text(str(os.getpid()))
    spy = os.environ.get("FAKE_ACP_SPY_OUT")
    if spy:
        Path(spy).write_text(
            json.dumps(
                {
                    "argv": sys.argv[1:],
                    "ACP_BACKEND": "ACP_BACKEND" in os.environ,
                    "DEVIN_API_KEY": "DEVIN_API_KEY" in os.environ,
                    "DEVIN_V3_API_KEY": "DEVIN_V3_API_KEY" in os.environ,
                    "DEVIN_LEGACY_API_KEY": "DEVIN_LEGACY_API_KEY" in os.environ,
                    "DEVIN_ORG_ID": "DEVIN_ORG_ID" in os.environ,
                    "WINDSURF_API_KEY": "WINDSURF_API_KEY" in os.environ,
                    "SBX_ACCOUNT_CREDENTIAL": "SBX_ACCOUNT_CREDENTIAL" in os.environ,
                }
            )
        )
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            _reply(rid, {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
        elif method == "session/new":
            if SCENARIO == "auth_invalid":
                _error(rid, -32000, "401 Unauthorized: invalid or expired API key")
            else:
                _reply(rid, {"sessionId": SESSION_ID, "modes": MODES})
        elif method == "session/load":
            # History replay: prior-turn updates arrive before the response,
            # and the response carries no sessionId.
            _update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "REPLAYED OLD MESSAGE"},
                }
            )
            _reply(rid, {"modes": MODES})
        elif method == "session/set_mode":
            _reply(rid, {})
        elif method == "session/prompt":
            _run_prompt(rid)
        elif rid is not None:
            _error(rid, -32601, f"unknown method {method}")


if __name__ == "__main__":
    main()
