"""``runner turn`` and ``runner stop``.

P2 (SOR-62/SOR-72): provider dispatch through ``AgentAdapter``. Native stdout
lines go to ``events.raw.jsonl``; ``adapter.translate`` output (canonical
events, Codex shape) goes to ``events.jsonl`` and runner stdout. Codex is the
identity translation, so its stream is unchanged.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

from runtime.runner.adapter import get_adapter
from runtime.runner.codex import iter_codex_stdout, start_codex
from runtime.runner.constants import (
    DEFAULT_MAX_SECONDS,
    EXIT_AUTH_INVALID,
    EXIT_BAD_JSON,
    EXIT_CODEX,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_TIMEOUT,
    NOOP_EVENT_TYPE,
    STATUS_AUTH_INVALID,
    STATUS_BAD_JSON,
    STATUS_CODEX_ERROR,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    TERM_GRACE_S,
)
from runtime.runner.events import TurnState, redact_line, redact_obj, redact_text
from runtime.runner.workspace import (
    atomic_write,
    codex_home,
    emit,
    emit_native,
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


def _stderr_tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:]


def _finish_status(
    *, timed_out: bool, bad_json: bool, cli_rc: int | None, health: str
) -> tuple[str, int]:
    if timed_out:
        return STATUS_TIMEOUT, EXIT_TIMEOUT
    if bad_json:
        return STATUS_BAD_JSON, EXIT_BAD_JSON
    if cli_rc is None or cli_rc != 0:
        if health == "auth_invalid":
            return STATUS_AUTH_INVALID, EXIT_AUTH_INVALID
        return STATUS_CODEX_ERROR, EXIT_CODEX
    return STATUS_SUCCESS, EXIT_OK


def cmd_turn(*, n: int, message_file: str, max_seconds: int = DEFAULT_MAX_SECONDS) -> int:
    root = work_root()
    ensure_layout(root)
    home = codex_home(root)
    started = time.monotonic()
    session = load_session(root)
    provider = session.get("provider") or "codex"
    try:
        adapter = get_adapter(provider)
    except KeyError as exc:
        emit(root, {"type": "sbx.error", "message": str(exc)})
        return EXIT_INTERNAL
    native_id = session.get("native_session_id") or session.get("codex_session_id")
    requested_id = str(native_id) if native_id else None
    state = TurnState(thread_id=native_id)
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

    if n == 1:
        emit(
            root,
            {
                "type": "sbx.session_meta",
                "provider": provider,
                "model": session.get("model"),
                "account_id": session.get("account_id"),
            },
        )
    emit(root, {"type": "sbx.turn_started", "n": n})

    model = session.get("model")
    if requested_id:
        argv = adapter.resume_argv(prompt, requested_id)
    else:
        argv = adapter.first_turn_argv(prompt, model if isinstance(model, str) and model else "")
    stderr_path = root / "turns" / f"{n}.stderr"

    try:
        proc = start_codex(argv, work=root, home=home, stderr_path=stderr_path)
    except OSError as exc:
        emit(root, {"type": "sbx.error", "message": f"failed to start provider CLI: {exc}"})
        status, code = _finish_status(timed_out=False, bad_json=False, cli_rc=1, health="unknown")
        _write_turn_finished(
            root,
            n=n,
            session=session,
            state=state,
            status=status,
            code=code,
            health="unknown",
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

    def _record_thread() -> None:
        if (
            state.thread_id
            and (requested_id is None or state.thread_id == requested_id)
            and session.get("native_session_id") != state.thread_id
        ):
            session["native_session_id"] = state.thread_id
            session["codex_session_id"] = state.thread_id
            session["pid"] = proc.pid
            save_session(root, session)

    observed_id: str | None = None  # thread.started emitted on this turn's stream

    for line in iter_codex_stdout(
        proc, max_seconds=max_seconds, grace_s=TERM_GRACE_S, on_timeout=_on_timeout
    ):
        safe = redact_line(line)
        if safe:
            emit_native(root, safe)
        events = adapter.translate(line)
        if not events and line.strip():
            state.bad_json_lines += 1
            emit(root, {"type": "sbx.error", "message": "bad json in event stream"})
        for event in events:
            if not isinstance(event, dict) or event.get("type") == NOOP_EVENT_TYPE:
                continue
            event = redact_obj(event)
            emit(root, event)
            if event.get("type") == "thread.started":
                tid = event.get("thread_id")
                if isinstance(tid, str) and tid:
                    observed_id = tid
            state.consume_obj(event)
        _record_thread()

    cli_rc = proc.wait()
    duration = round(time.monotonic() - started, 3)
    health = "ok" if cli_rc == 0 else adapter.health_from(cli_rc, _stderr_tail(stderr_path))
    # Stale resume: the provider CLI exited 0 but never confirmed the requested
    # session id (agy warns on stderr and opens a new conversation). Never
    # adopt a replacement id; fail the turn instead of forking the session.
    stale_resume = requested_id is not None and observed_id != requested_id
    if stale_resume:
        state.thread_id = None
    status, code = _finish_status(
        timed_out=timed_out,
        bad_json=state.bad_json_lines > 0,
        cli_rc=cli_rc,
        health=health,
    )
    if stale_resume and code == EXIT_OK:
        emit(
            root,
            {
                "type": "sbx.error",
                "message": f"provider did not resume session {requested_id}",
            },
        )
        status, code = STATUS_CODEX_ERROR, EXIT_CODEX
    session = load_session(root)
    if state.thread_id:
        session["native_session_id"] = state.thread_id
        session["codex_session_id"] = state.thread_id
    session["turn"] = n
    _write_turn_finished(
        root,
        n=n,
        session=session,
        state=state,
        status=status,
        code=code,
        health=health,
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
    health: str,
    duration_s: float,
) -> None:
    session["pid"] = None
    session["turn"] = n
    if state.thread_id:
        session["native_session_id"] = state.thread_id
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
        "native_session_id": state.thread_id or session.get("native_session_id"),
        "status": status,
        "exit_code": code,
        "health": health,
        "duration_s": duration_s,
        "usage": dict(state.usage),
        "message": redact_text(state.last_message),
        "bad_json_lines": state.bad_json_lines,
    }
    atomic_write(turn_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
