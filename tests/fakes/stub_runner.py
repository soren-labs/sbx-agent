#!/usr/bin/env python3
"""Stub runner implementing docs/contracts/runner-cli.md for local tests."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "events"
REPO_ROOT = Path(__file__).resolve().parents[2]
# Local sandboxes exec this file directly; the repo root must come FIRST for
# ``runtime.runner.contract`` (SOR-130). A deployed ``/opt/sbx`` copy (or a
# .pth-installed repo root later in sys.path) lacks the new module, so mere
# membership is not enough — the repo root must outrank them.
sys.path.insert(0, str(REPO_ROOT))
DEFAULT_THREAD_ID = "01a09a36-b4fb-7f90-b96e-42adeefa05e0"
PROVIDERS = ("codex", "antigravity", "grok", "opencode", "devin")

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CODEX = 2
EXIT_TIMEOUT = 3
EXIT_BAD_JSON = 4
EXIT_AUTH_INVALID = 5


def work_root() -> Path:
    raw = os.environ.get("SBX_WORK")
    if not raw:
        print("SBX_WORK is required", file=sys.stderr)
        sys.exit(1)
    return Path(raw)


def sandbox_home(root: Path) -> Path:
    """$HOME inside the sandbox: ``$SBX_WORK/home`` (filesystem.md v2)."""
    if _home_is_sandbox(root):
        return Path(os.environ["HOME"])
    return root / "home"


def _home_is_sandbox(root: Path) -> bool:
    home = os.environ.get("HOME")
    return bool(home) and Path(home).resolve().is_relative_to(root.resolve())


def codex_home(root: Path) -> Path:
    return Path(os.environ.get("CODEX_HOME", str(root / ".codex")))


def pid_file(root: Path) -> Path:
    return root / "runner.pid"


def emit(root: Path, obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    events = root / "events.jsonl"
    with events.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def emit_raw(root: Path, line: str) -> None:
    """Canonical events go to events.jsonl; the same native line is mirrored
    verbatim to events.raw.jsonl (Codex native == canonical)."""
    events = root / "events.jsonl"
    with events.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    with (root / "events.raw.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def restore_credential_blob(root: Path) -> list[str]:
    """Restore ``SBX_ACCOUNT_CREDENTIAL`` below ``$SBX_WORK/home`` (mode 600).

    Returns the list of restored relative paths. Paths escaping the sandbox
    home are rejected.
    """
    raw = os.environ.get("SBX_ACCOUNT_CREDENTIAL")
    if not raw:
        return []
    blob = json.loads(raw)
    files = blob.get("files") or {}
    home = sandbox_home(root)
    restored: list[str] = []
    for relpath, content in files.items():
        target = (home / relpath).resolve()
        if not target.is_relative_to(home.resolve()):
            print(f"credential path escapes HOME: {relpath}", file=sys.stderr)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        target.chmod(0o600)
        restored.append(relpath)
    return restored


def cmd_init(args: argparse.Namespace) -> int:
    root = work_root()
    root.mkdir(parents=True, exist_ok=True)

    # SOR-179/204: mirror runtime.runner.bootstrap — a provider/effort
    # combination with no native mapping fails init explicitly;
    # SBX_EFFORT_SURFACE carries the discovered capability surface.
    from runtime.runner.effort import effort_error, normalize_effort

    try:
        effort = normalize_effort(args.reasoning_effort)
        surface = os.environ.get("SBX_EFFORT_SURFACE")
        if surface is not None and effort is not None:
            allowed = {tok.strip() for tok in surface.split(",") if tok.strip()}
            refusal = (
                None
                if effort in allowed
                else f"account does not support reasoning_effort {effort!r}"
            )
        else:
            refusal = effort_error(args.provider, effort)
    except ValueError as exc:
        refusal = str(exc)
    if refusal is not None:
        print(f"runner init: {refusal}", file=sys.stderr)
        return EXIT_INTERNAL
    (root / "inbox").mkdir(exist_ok=True)
    (root / "turns").mkdir(exist_ok=True)
    home = codex_home(root)
    home.mkdir(parents=True, exist_ok=True)

    credential_files = restore_credential_blob(root)

    # Mirror runtime.runner.bootstrap: a non-codex provider's adapter
    # prepares the restored HOME before any turn (e.g. agy's onboarding
    # marker is synthesized when the blob didn't carry one, SOR-258).
    if args.provider != "codex":
        from runtime.runner.adapter import get_adapter

        get_adapter(args.provider).prepare_home(sandbox_home(root), args.model)

    (home / "config.toml").write_text(
        (
            f'model = "{args.model}"\n'
            'approval_policy = "never"\n'
            'sandbox_mode = "danger-full-access"\n'
            "\n"
            "[shell_environment_policy]\n"
            'exclude = ["CODEX_AUTH_JSON"]\n'
        ),
        encoding="utf-8",
    )

    src_auth = root / "auth.json"
    if args.auth == "auth_json" and src_auth.is_file():
        payload = json.loads(src_auth.read_text(encoding="utf-8"))
    elif args.auth == "auth_json":
        payload = {
            "auth_mode": "auth_json",
            "tokens": {
                "access_token": "REDACTED",
                "refresh_token": "REDACTED",
                "id_token": "REDACTED",
            },
        }
    else:
        payload = {
            "auth_mode": "provider",
            "provider": "chatgpt",
            "tokens": {"access_token": "REDACTED"},
        }
    _redact_tokens(payload)
    auth_path = home / "auth.json"
    auth_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    auth_path.chmod(0o600)

    (root / "AGENTS.md").write_text(
        "# Sandbox AGENTS.md\n\nGenerated by `runner init`. Do not put secrets here.\n",
        encoding="utf-8",
    )
    (root / "events.jsonl").write_text("", encoding="utf-8")
    (root / "events.raw.jsonl").write_text("", encoding="utf-8")
    (root / "session.json").write_text(
        json.dumps(
            {
                "provider": args.provider,
                "account_id": args.account_id or os.environ.get("SBX_ACCOUNT_ID"),
                "model": args.model,
                # SOR-179: canonical effort bound to every turn.
                "reasoning_effort": effort,
                "native_session_id": None,
                "codex_session_id": None,  # v1 compatibility alias
                "turn": 0,
                "credential_files": credential_files,
                # SOR-129: mirror runtime.runner.bootstrap — declared MCP
                # refs reach the sandbox as SBX_MCP_SERVERS (devin only);
                # session.json records server names, never config values.
                "mcp_servers": _declared_mcp_names() if args.provider == "devin" else [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return EXIT_OK


def _declared_mcp_names() -> list:
    """Names in ``SBX_MCP_SERVERS`` (SOR-129) + the SOR-77 Linear gate."""
    names: list = []
    raw = os.environ.get("SBX_MCP_SERVERS")
    if raw:
        for entry in json.loads(raw):
            name = entry.get("name") if isinstance(entry, dict) else None
            if name and name not in names:
                names.append(name)
    if os.environ.get("SBX_LINEAR_API_KEY") and "linear" not in names:
        names.append("linear")
    return names


def _redact_tokens(obj: object) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"access_token", "refresh_token", "id_token", "token", "api_key", "password"}:
                obj[key] = "REDACTED"
            else:
                _redact_tokens(value)
    elif isinstance(obj, list):
        for item in obj:
            _redact_tokens(item)


def _scenario() -> str:
    return os.environ.get("FAKE_CODEX_SCENARIO", "success")


def _load_session(root: Path) -> dict:
    path = root / "session.json"
    if not path.is_file():
        return {"codex_session_id": None, "native_session_id": None, "turn": 0}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_session(root: Path, session: dict) -> None:
    # Atomic replace: control-plane readers must never observe a torn file
    # (``write_text`` truncates first, so a concurrent read can catch the
    # empty window and fail its JSON parse).
    tmp = root / "session.json.tmp"
    tmp.write_text(json.dumps(session) + "\n", encoding="utf-8")
    os.replace(tmp, root / "session.json")


def cmd_turn(args: argparse.Namespace) -> int:
    root = work_root()
    start = time.monotonic()
    pid_file(root).write_text(str(os.getpid()), encoding="utf-8")

    def _on_term(_signum: int, _frame: object) -> None:
        sys.exit(EXIT_OK)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    inbox = root / "inbox"
    inbox.mkdir(exist_ok=True)
    (root / "turns").mkdir(exist_ok=True)
    message = Path(args.message_file).read_text(encoding="utf-8")
    (inbox / f"{args.n}.md").write_text(message, encoding="utf-8")

    session = _load_session(root)
    if args.n == 1:
        emit(
            root,
            {
                "type": "sbx.session_meta",
                "provider": session.get("provider") or "codex",
                "model": session.get("model"),
                "account_id": session.get("account_id"),
                "reasoning_effort": session.get("reasoning_effort"),
            },
        )
    emit(root, {"type": "sbx.turn_started", "n": args.n})

    scenario = _scenario()
    fixture = FIXTURE_DIR / f"{scenario}.jsonl"
    if not fixture.is_file():
        emit(root, {"type": "sbx.error", "message": f"missing fixture {scenario}"})
        return EXIT_BAD_JSON

    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    thread_id = _load_session(root).get("codex_session_id") or DEFAULT_THREAD_ID
    last_message = ""
    last_error = ""
    timed_out = False
    bad_json_lines = 0

    lines = fixture.read_text(encoding="utf-8").splitlines()
    for index, raw in enumerate(lines):
        if time.monotonic() - start >= args.max_seconds:
            timed_out = True
            break
        stripped = raw.strip()
        if not stripped:
            continue
        if scenario == "slow" and index == 1:
            delay = float(os.environ.get("FAKE_CODEX_SLOW_SECONDS", "40"))
            remaining = args.max_seconds - (time.monotonic() - start)
            time.sleep(min(delay, max(0.0, remaining)))
            if time.monotonic() - start >= args.max_seconds:
                timed_out = True
                emit_raw(root, stripped)
                break
        emit_raw(root, stripped)
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            bad_json_lines += 1
            last_error = "bad json in event stream"
            emit(root, {"type": "sbx.error", "message": "bad json in event stream"})
            continue
        if obj.get("type") == "thread.started" and obj.get("thread_id"):
            thread_id = obj["thread_id"]
        if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
            usage = {
                "input_tokens": int(obj["usage"].get("input_tokens", 0)),
                "cached_input_tokens": int(obj["usage"].get("cached_input_tokens", 0)),
                "output_tokens": int(obj["usage"].get("output_tokens", 0)),
            }
            for opt in ("cache_write_input_tokens", "reasoning_output_tokens"):
                if opt in obj["usage"]:
                    usage[opt] = int(obj["usage"][opt])
        if obj.get("type") == "error":
            err = obj.get("error")
            msg = obj.get("message")
            if isinstance(err, dict):
                msg = err.get("message") or msg
            elif isinstance(err, str) and err:
                msg = err
            if isinstance(msg, str) and msg:
                last_error = msg
        if obj.get("type") == "turn.failed":
            err = obj.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            if isinstance(msg, str) and msg:
                last_error = msg
        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
            last_message = item["text"]
        if isinstance(item, dict) and item.get("type") == "error" and item.get("message"):
            last_error = str(item["message"])
        if scenario == "hang" and index == 0:
            remaining = args.max_seconds - (time.monotonic() - start)
            time.sleep(max(0.0, remaining if remaining < 3600 else 3600))
            if time.monotonic() - start >= args.max_seconds:
                timed_out = True
            break

    # Observability floor (SOR-82 A4): keep the turn alive long enough for
    # ``GET run`` pollers to observe RUNNING; never extends past --max-seconds.
    floor = float(os.environ.get("FAKE_CODEX_TURN_SECONDS", "0") or 0)
    if floor > 0:
        elapsed = time.monotonic() - start
        headroom = args.max_seconds - elapsed
        time.sleep(max(0.0, min(floor - elapsed, headroom)))

    duration = round(time.monotonic() - start, 3)
    session = _load_session(root)
    session["codex_session_id"] = thread_id
    session["native_session_id"] = thread_id
    session["turn"] = args.n
    _write_session(root, session)

    if timed_out:
        status = "timeout"
        code = EXIT_TIMEOUT
    elif bad_json_lines:
        status = "bad_json"
        code = EXIT_BAD_JSON
    elif scenario == "auth_invalid":
        status = "auth_invalid"
        code = EXIT_AUTH_INVALID
    elif scenario == "nonzero":
        status = "codex_error"
        code = EXIT_CODEX
    else:
        status = "success"
        code = EXIT_OK

    emit(
        root,
        {
            "type": "sbx.turn_finished",
            "status": status,
            "exit_code": code,
            "duration_s": duration,
            "usage": usage,
        },
    )
    payload = {
        "n": args.n,
        "codex_session_id": thread_id,
        "native_session_id": thread_id,
        "status": status,
        "usage": usage,
        "message": last_message,
        "error": (last_error or None) if status != "success" else None,
        "bad_json_lines": bad_json_lines,
        "exit_code": code,
    }
    if args.output_contract is not None:
        # SOR-130: mirror runtime.runner.turn — evaluate the final message
        # against the contract and record the verdict on the turn payload.
        from runtime.runner.contract import (
            ContractError,
            evaluate_output,
            load_contract_file,
        )

        try:
            contract = load_contract_file(args.output_contract)
        except (OSError, ContractError) as exc:
            emit(root, {"type": "sbx.error", "message": f"invalid output contract: {exc}"})
            return EXIT_INTERNAL
        meta = {
            "enforcement": contract["enforcement"],
            "schema_digest": contract["schema_digest"],
        }
        if status == "success":
            verdict = evaluate_output(last_message, contract["schema"])
            payload["structured_output"] = verdict["value"]
            payload["output_contract"] = {
                **meta,
                "status": verdict["status"],
                "extraction": verdict["extraction"],
                "violations": verdict["violations"],
            }
        else:
            payload["structured_output"] = None
            payload["output_contract"] = {
                **meta,
                "status": "skipped",
                "extraction": None,
                "violations": [],
            }
    turn_path = root / "turns" / f"{args.n}.json"
    turn_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        pid_file(root).unlink(missing_ok=True)
    except TypeError:
        if pid_file(root).exists():
            pid_file(root).unlink()
    return code


def cmd_export_credentials(_args: argparse.Namespace) -> int:
    """Print a credential blob to stdout; empty stdout when unchanged.

    Reads the restored files listed in ``session.json.credential_files`` and
    compares them with the ``SBX_ACCOUNT_CREDENTIAL`` blob. A rewritten
    credential file (e.g. refreshed tokens) produces a new blob on stdout.
    """
    root = work_root()
    session = _load_session(root)
    relpaths = session.get("credential_files") or []
    home = sandbox_home(root)
    files: dict[str, str] = {}
    for relpath in relpaths:
        target = (home / relpath).resolve()
        if target.is_relative_to(home.resolve()) and target.is_file():
            files[relpath] = target.read_text(encoding="utf-8")
    if not files:
        return EXIT_OK
    new_blob = {"provider": session.get("provider") or "codex", "files": files}
    old_raw = os.environ.get("SBX_ACCOUNT_CREDENTIAL")
    if old_raw:
        try:
            old_blob = json.loads(old_raw)
        except json.JSONDecodeError:
            old_blob = None
        if old_blob == new_blob:
            return EXIT_OK
    print(json.dumps(new_blob, ensure_ascii=False))
    return EXIT_OK


def cmd_stop(_args: argparse.Namespace) -> int:
    root = work_root()
    path = pid_file(root)
    if not path.is_file():
        return EXIT_OK
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return EXIT_OK
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return EXIT_OK


def main() -> None:
    parser = argparse.ArgumentParser(prog="runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--auth", choices=["auth_json", "provider"], default="auth_json")
    p_init.add_argument("--model", required=True)
    p_init.add_argument("--provider", choices=PROVIDERS, default="codex")
    p_init.add_argument("--account-id", default=None)
    p_init.add_argument("--reasoning-effort", default=None)

    p_turn = sub.add_parser("turn")
    p_turn.add_argument("--n", type=int, required=True)
    p_turn.add_argument("--message-file", required=True)
    p_turn.add_argument("--max-seconds", type=int, default=900)
    p_turn.add_argument("--output-contract", default=None)

    sub.add_parser("stop")
    sub.add_parser("export-credentials")

    args = parser.parse_args()
    if args.cmd == "init":
        code = cmd_init(args)
    elif args.cmd == "turn":
        code = cmd_turn(args)
    elif args.cmd == "export-credentials":
        code = cmd_export_credentials(args)
    else:
        code = cmd_stop(args)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
