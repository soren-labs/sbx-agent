"""Build Codex argv / env and run it with a soft timeout."""

from __future__ import annotations

import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from runtime.runner.constants import TERM_GRACE_S
from runtime.runner.workspace import sandbox_home


def codex_bin_tokens() -> list[str]:
    raw = os.environ.get("CODEX_BIN", "codex")
    tokens = shlex.split(raw)
    if not tokens:
        tokens = ["codex"]
    if len(tokens) == 1 and tokens[0].endswith(".py"):
        return [sys.executable, tokens[0]]
    return tokens


def build_codex_argv(
    *,
    work: Path,
    prompt: str,
    thread_id: str | None,
    model: str | None,
) -> list[str]:
    """Build ``codex exec`` / ``codex exec resume`` argv.

    Prompt is a positional argument (never ``-``). First turn matches
    ``runner-cli.md`` plus P0: ``-C $SBX_WORK``. Resume matches P0 / Codex
    CLI 0.153.0: ``codex exec resume`` does **not** accept ``-C`` (cwd is
    already ``work`` in ``start_codex``).
    """
    cmd = [*codex_bin_tokens()]
    if thread_id:
        cmd += [
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            thread_id,
            prompt,
        ]
        return cmd
    cmd += [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(work),
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if model:
        cmd += ["-m", model]
    cmd.append(prompt)
    return cmd


# Env vars that must never reach the provider CLI subprocess: injected
# credential blobs and provider key material (runner-cli.md §凭证注入; the
# Devin CLI must authenticate from its restored credentials file only, and
# ACP_BACKEND must not leak into `devin acp`).
CHILD_ENV_DENYLIST: tuple[str, ...] = (
    "CODEX_AUTH_JSON",
    "SBX_ACCOUNT_CREDENTIAL",
    "ACP_BACKEND",
    "DEVIN_API_KEY",
    "DEVIN_V3_API_KEY",
    "DEVIN_LEGACY_API_KEY",
    "DEVIN_ORG_ID",
    "DEVIN_MODEL",
    "DEVIN_REFUSAL_FALLBACK",
    "WINDSURF_API_KEY",
)


def child_env(work: Path, home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in CHILD_ENV_DENYLIST}
    env["SBX_WORK"] = str(work)
    env["CODEX_HOME"] = str(home)
    # Provider CLIs always see the sandbox $HOME ($SBX_WORK/home), never the
    # runner's inherited HOME — the restored credential blob is the only
    # auth source (filesystem.md).
    env["HOME"] = str(sandbox_home(work))
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _term(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return


def _kill(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return


def iter_codex_stdout(
    proc: subprocess.Popen[str],
    *,
    max_seconds: float,
    grace_s: float = TERM_GRACE_S,
    on_timeout: Callable[[], None] | None = None,
) -> Iterator[str]:
    """Yield stdout lines; SIGTERM at ``max_seconds``, SIGKILL after grace."""
    assert proc.stdout is not None
    q: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:
                q.put(line.rstrip("\n"))
        finally:
            q.put(None)

    reader = threading.Thread(target=_reader, daemon=True, name="codex-stdout")
    reader.start()

    deadline = time.monotonic() + max(0.0, max_seconds)
    timed_out = False
    grace_deadline: float | None = None
    saw_sentinel = False

    try:
        while True:
            now = time.monotonic()
            if not timed_out and now >= deadline and proc.poll() is None:
                timed_out = True
                if on_timeout is not None:
                    on_timeout()
                _term(proc)
                grace_deadline = time.monotonic() + grace_s
            if timed_out and grace_deadline is not None and now >= grace_deadline:
                _kill(proc)

            wait = 0.2
            if not timed_out:
                wait = min(0.2, max(0.05, deadline - now))
            elif grace_deadline is not None:
                wait = min(0.2, max(0.05, grace_deadline - now))

            try:
                item = q.get(timeout=wait)
            except queue.Empty:
                continue
            if item is None:
                saw_sentinel = True
                break
            yield item
    finally:
        if not saw_sentinel:
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    break
                yield item
        reader.join(timeout=1.0)
        proc.wait()


def start_codex(
    argv: list[str],
    *,
    work: Path,
    home: Path,
    stderr_path: Path,
) -> subprocess.Popen[str]:
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_f = stderr_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(work),
            env=child_env(work, home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_f,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception:
        stderr_f.close()
        raise
    stderr_f.close()
    return proc
