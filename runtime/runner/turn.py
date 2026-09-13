"""``runner turn`` and ``runner stop``."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

from runtime.runner.codex import build_codex_argv, iter_codex_stdout, start_codex
from runtime.runner.constants import (
    DEFAULT_MAX_SECONDS,
    EXIT_BAD_JSON,
    EXIT_CODEX,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_TIMEOUT,
    STATUS_BAD_JSON,
    STATUS_CODEX_ERROR,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    TERM_GRACE_S,
)
from runtime.runner.events import TurnState, redact_line, redact_text
from runtime.runner.workspace import (
    atomic_write,
    codex_home,
    emit,
    emit_raw,
    ensure_layout,
    load_session,
    save_session,
    work_root,
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cmd_stop() -> int:
    root = work_root()
    session = load_session(root)
    raw_pid = session.get("pid")
    if raw_pid is None:
        return EXIT_OK
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        session["pid"] = None
        save_session(root, session)
        return EXIT_OK
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        session["pid"] = None
        save_session(root, session)
        return EXIT_OK
    deadline = time.monotonic() + TERM_GRACE_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline and _pid_alive(pid):
            time.sleep(0.05)
    session = load_session(root)
    session["pid"] = None
    save_session(root, session)
    return EXIT_OK


def _finish_status(*, timed_out: bool, bad_json: bool, codex_rc: int | None) -> tuple[str, int]:
    if timed_out:
        return STATUS_TIMEOUT, EXIT_TIMEOUT
    if bad_json:
        return STATUS_BAD_JSON, EXIT_BAD_JSON
    if codex_rc is None or codex_rc != 0:
        return STATUS_CODEX_ERROR, EXIT_CODEX
    return STATUS_SUCCESS, EXIT_OK


def cmd_turn(*, n: int, message_file: str, max_seconds: int = DEFAULT_MAX_SECONDS) -> int:
    root = work_root()
    ensure_layout(root)
    home = codex_home(root)
    started = time.monotonic()
    session = load_session(root)
    state = TurnState(thread_id=session.get("codex_session_id"))
    timed_out = False
    proc = None

    def _on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        emit(root, {"type": "sbx.error", "message": f"turn {n} exceeded {max_seconds}s"})

    try:
        src = Path(message_file)
        prompt = src.read_text(encoding="utf-8")
    except OSError as exc:
        emit(root, {"type": "sbx.error", "message": f"cannot read message file: {exc}"})
        return EXIT_INTERNAL

    inbox = root / "inbox" / f"{n}.md"
    atomic_write(inbox, prompt)

    emit(root, {"type": "sbx.turn_started", "n": n})

    thread_id = session.get("codex_session_id")
    model = session.get("model")
    argv = build_codex_argv(
        work=root,
        prompt=prompt,
        thread_id=thread_id if thread_id else None,
        model=model if isinstance(model, str) and model else None,
    )
    stderr_path = root / "turns" / f"{n}.stderr"

    try:
        proc = start_codex(argv, work=root, home=home, stderr_path=stderr_path)
    except OSError as exc:
        emit(root, {"type": "sbx.error", "message": f"failed to start Codex: {exc}"})
        status, code = _finish_status(timed_out=False, bad_json=False, codex_rc=1)
        _write_turn_finished(
            root,
            n=n,
            session=session,
            state=state,
            status=status,
            code=code,
            duration_s=round(time.monotonic() - started, 3),
        )
        return code

    session["pid"] = proc.pid
    save_session(root, session)

    def _forward_term(_signum: int, _frame: object) -> None:
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                return

    signal.signal(signal.SIGTERM, _forward_term)
    signal.signal(signal.SIGINT, _forward_term)

    for line in iter_codex_stdout(
        proc, max_seconds=max_seconds, grace_s=TERM_GRACE_S, on_timeout=_on_timeout
    ):
        safe = redact_line(line)
        if safe:
            emit_raw(root, safe)
        _, bad = state.consume_line(line)
        if bad:
            emit(root, {"type": "sbx.error", "message": "bad json in event stream"})
        if state.thread_id and session.get("codex_session_id") != state.thread_id:
            session["codex_session_id"] = state.thread_id
            session["pid"] = proc.pid
            save_session(root, session)

    codex_rc = proc.wait()
    duration = round(time.monotonic() - started, 3)
    status, code = _finish_status(
        timed_out=timed_out,
        bad_json=state.bad_json_lines > 0,
        codex_rc=codex_rc,
    )
    session = load_session(root)
    if state.thread_id:
        session["codex_session_id"] = state.thread_id
    session["turn"] = n
    _write_turn_finished(
        root,
        n=n,
        session=session,
        state=state,
        status=status,
        code=code,
        duration_s=duration,
    )
    return code


def _write_turn_finished(
    root: Path,
    *,
    n: int,
    session: dict,
    state: TurnState,
    status: str,
    code: int,
    duration_s: float,
) -> None:
    session["pid"] = None
    session["turn"] = n
    if state.thread_id:
        session["codex_session_id"] = state.thread_id
    save_session(root, session)
    emit(
        root,
        {
            "type": "sbx.turn_finished",
            "status": status,
            "exit_code": code,
            "duration_s": duration_s,
            "usage": dict(state.usage),
        },
    )
    turn_path = root / "turns" / f"{n}.json"
    payload = {
        "n": n,
        "codex_session_id": state.thread_id or session.get("codex_session_id"),
        "status": status,
        "exit_code": code,
        "duration_s": duration_s,
        "usage": dict(state.usage),
        "message": redact_text(state.last_message),
        "bad_json_lines": state.bad_json_lines,
    }
    atomic_write(turn_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
