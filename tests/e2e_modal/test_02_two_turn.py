"""Two-turn real Codex: write module+test, then fix the bug. Resume must work."""

from __future__ import annotations

import json
import time

import httpx

from tests.e2e_modal.helpers import (
    TURN1_PROMPT,
    TURN2_PROMPT,
    assistant_texts,
    close_session,
    create_session,
    list_sandboxes,
    post_turn,
    sandbox_ls,
    sandbox_read,
    usage_numbers,
    write_json,
)


def test_two_turn_write_module_then_fix_bug(client: httpx.Client) -> None:
    title = f"wp2h-two-turn-{int(time.time())}"
    sid = None
    try:
        sid, cold_s = create_session(client, title)
        assert cold_s <= 60.0, f"cold start {cold_s:.3f}s exceeds 60s"

        runtime_ls = sandbox_ls(sid, "/opt/sbx/runtime")
        assert "runner" in runtime_ls
        assert "__main__.py" in sandbox_ls(sid, "/opt/sbx/runtime/runner")

        rec1, turn1_s = post_turn(client, sid, TURN1_PROMPT)
        adder1 = sandbox_read(sid, "adder.py") or ""
        test1 = sandbox_read(sid, "test_adder.py") or ""
        turn1_meta = sandbox_read(sid, "turns/1.json") or "{}"
        assert "def add" in adder1
        assert "a - b" in adder1 or "a-b" in adder1
        assert "test_add" in test1 or "TestAdd" in test1
        meta1 = json.loads(turn1_meta) if turn1_meta.strip().startswith("{") else {}
        duration1 = float(meta1.get("duration_s") or turn1_s)
        assert duration1 > 1.0, f"turn1 too fast ({duration1}); likely failed immediately"
        assert rec1["turns"] >= 1
        joined1 = "\n".join(assistant_texts(rec1))
        assert joined1.strip(), "turn1 produced an empty assistant message"

        rec2, turn2_s = post_turn(client, sid, TURN2_PROMPT)
        adder2 = sandbox_read(sid, "adder.py") or ""
        turn2_meta = sandbox_read(sid, "turns/2.json") or "{}"
        meta2 = json.loads(turn2_meta) if turn2_meta.strip().startswith("{") else {}
        duration2 = float(meta2.get("duration_s") or turn2_s)
        assert duration2 > 1.0, f"turn2 duration {duration2}s looks like resume -C failure (~0.06s)"
        assert "a + b" in adder2 or "a+b" in adder2
        assert rec2["turns"] >= 2
        joined2 = "\n".join(assistant_texts(rec2))
        assert joined2.strip(), "turn2 produced an empty assistant message"
        assert "TURN2_TESTS_PASSED" in joined2 or "OK" in joined2 or "passed" in joined2.lower()

        usage = usage_numbers(rec2)
        write_json(
            "timings.json",
            {
                "session_id": sid,
                "cold_start_s": round(cold_s, 3),
                "turn1_s": round(turn1_s, 3),
                "turn2_s": round(turn2_s, 3),
                "turn1_duration_s": round(duration1, 3),
                "turn2_duration_s": round(duration2, 3),
                "usage": usage,
                "cost_estimate_usd": rec2.get("cost_estimate_usd"),
            },
        )

        closed = close_session(client, sid)
        assert closed is not None
        assert closed["status"] == "closed"
        time.sleep(2.0)
        leftover = list_sandboxes(session_id=sid)
        assert leftover == [], f"sandbox leak after close: {leftover}"
        sid = None
    finally:
        if sid:
            close_session(client, sid)
