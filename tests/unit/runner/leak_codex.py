#!/usr/bin/env python3
"""Emit a Codex-shaped stream that includes a secret field. Test-only."""

from __future__ import annotations

import json
import sys

print(
    json.dumps({"type": "thread.started", "thread_id": "01a09a36-b4fb-7f90-b96e-42adeefa05e0"}),
    flush=True,
)
print(json.dumps({"type": "turn.started"}), flush=True)
print(
    json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "agent_message",
                "text": "ok",
                "api_key": "sk-THISLEAKEDVALUE12",
            },
        }
    ),
    flush=True,
)
print(
    json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 10000,
                "cache_write_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
            },
        }
    ),
    flush=True,
)
