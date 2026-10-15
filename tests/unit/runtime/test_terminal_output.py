"""Terminal output offsets never split a UTF-8 character (issue #227 AUD08)."""

from __future__ import annotations

import threading

from runtime.daemon.terminal import Terminal, _incomplete_tail


class _Proc:
    def __init__(self, exited: bool = False) -> None:
        self.exited = exited

    def poll(self) -> int | None:
        return 0 if self.exited else None


def _terminal(buffer: bytes, base: int = 0, exited: bool = False) -> Terminal:
    term = object.__new__(Terminal)
    term.buffer = bytearray(buffer)
    term.base = base
    term.lock = threading.Lock()
    term.proc = _Proc(exited)
    return term


def test_incomplete_tail_lengths() -> None:
    wide = "界".encode()  # 3 bytes
    emoji = "🙂".encode()  # 4 bytes
    assert _incomplete_tail(b"abc") == 0
    assert _incomplete_tail(b"a" + wide) == 0
    assert _incomplete_tail(b"a" + wide[:1]) == 1
    assert _incomplete_tail(b"a" + wide[:2]) == 2
    assert _incomplete_tail(emoji[:3]) == 3
    assert _incomplete_tail(emoji) == 0
    assert _incomplete_tail(b"\x1b[31m") == 0


def test_split_character_waits_for_the_next_read() -> None:
    data = "ok 界".encode()
    term = _terminal(data[:-1])
    first = term.read(0)
    assert first["data"] == "ok " and first["offset"] == 3
    term.buffer += data[-1:]
    second = term.read(first["offset"])
    assert second["data"] == "界" and second["offset"] == len(data)
    assert "�" not in first["data"] + second["data"]


def test_exited_process_flushes_partial_bytes_and_trimmed_head_is_skipped() -> None:
    assert _terminal("界".encode()[:2], exited=True).read(0)["data"] == "�"
    # The buffer was trimmed in the middle of a character: its orphaned bytes are dropped.
    trimmed = _terminal("界x".encode()[1:], base=1)
    chunk = trimmed.read(0)
    assert chunk["data"] == "x" and chunk["truncated"] is True
