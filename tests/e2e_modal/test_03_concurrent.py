"""Two concurrent sessions are isolated (filesystem + event streams)."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from tests.e2e_modal.helpers import (
    client_for,
    close_session,
    create_session,
    list_sandboxes,
    post_turn,
    sandbox_read,
    write_json,
)


def _one_session(label: str, token: str) -> dict[str, object]:
    client = client_for()
    sid: str | None = None
    title = f"wp2h-conc-{label}-{int(time.time())}"
    filename = f"iso_{label}.txt"
    prompt = (
        f"Work in /work. Create a file named {filename} whose entire contents are exactly:\n"
        f"{token}\n"
        f"Do not create iso_a.txt or iso_b.txt except the one named above.\n"
        f"Reply with the exact line: CONC_{label.upper()}_DONE"
    )
    try:
        sid, cold = create_session(client, title)
        rec, turn_s = post_turn(client, sid, prompt)
        body = sandbox_read(sid, filename) or ""
        other = "iso_b.txt" if label == "a" else "iso_a.txt"
        other_body = sandbox_read(sid, other)
        return {
            "sid": sid,
            "label": label,
            "cold_s": cold,
            "turn_s": turn_s,
            "file": body,
            "other": other_body,
            "turns": rec["turns"],
            "usage": rec.get("usage") or {},
        }
    except Exception:
        if sid:
            close_session(client, sid)
        raise
    finally:
        client.close()


def test_two_sessions_isolated(client: httpx.Client) -> None:
    token_a = f"ALPHA-{int(time.time())}"
    token_b = f"BETA-{int(time.time())}"
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [
            pool.submit(_one_session, "a", token_a),
            pool.submit(_one_session, "b", token_b),
        ]
        for fut in as_completed(futs):
            results.append(fut.result())
    by_label = {str(row["label"]): row for row in results}
    try:
        assert set(by_label) == {"a", "b"}
        file_a = str(by_label["a"]["file"])
        file_b = str(by_label["b"]["file"])
        assert token_a in file_a
        assert token_b in file_b
        assert token_b not in file_a
        assert token_a not in file_b
        assert by_label["a"]["other"] in (None, "")
        assert by_label["b"]["other"] in (None, "")
        write_json(
            "concurrent.json",
            {
                "a": {
                    "cold_s": by_label["a"]["cold_s"],
                    "turn_s": by_label["a"]["turn_s"],
                    "usage": by_label["a"]["usage"],
                },
                "b": {
                    "cold_s": by_label["b"]["cold_s"],
                    "turn_s": by_label["b"]["turn_s"],
                    "usage": by_label["b"]["usage"],
                },
            },
        )
    finally:
        for row in results:
            sid = str(row["sid"])
            close_session(client, sid)
            time.sleep(1.0)
            leftover = list_sandboxes(session_id=sid)
            assert leftover == [], f"sandbox leak {sid}: {leftover}"
