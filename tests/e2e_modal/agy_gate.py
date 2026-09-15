"""SOR-62 gate: real Modal e2e for the PRODUCTION Antigravity adapter.

Host-executed. Creates one real Modal Sandbox on a derived image (the
production ``sbx_runtime_image()`` plus the host's ``agy`` binary — the
named ``sbx-runtime`` image does not ship ``agy`` yet) and injects the
account credential via an ephemeral Secret carrying the exact production
``SBX_ACCOUNT_CREDENTIAL`` blob contract. It then drives
``python -m runtime.runner`` exactly like the control plane does:

    init -> turn 1 -> turn 2 (--conversation resume) -> turn 3 (stale id)
         -> export-credentials -> terminate -> leftover scan

Coverage: real two-turn continuity (the model must recall a phrase without
re-reading files), canonical event shape (``sbx.session_meta`` once,
``thread.started`` id stability, ``turn.completed`` usage,
``sbx.turn_finished``), stale-resume protection (runner must fail the turn
with exit 2 and never adopt the replacement conversation id), state export
(empty or a valid antigravity blob; contents never logged), leak checks
(token / id_token / JWT / sk- patterns over every captured stream and
artifact), and cleanup (zero tagged sandboxes left running).

Secret hygiene: credential material is never printed. Only a sha256-16
fingerprint, file relpaths, and booleans are recorded. ``runner
export-credentials`` stdout is captured without echoing. Prerequisites are
checked up front; without them the script exits 2 with verdict SKIP — it
never reports a real-account PASS it did not execute.

Run (from repo root, real agy + ~/.modal.toml required):

    uv run python -m tests.e2e_modal.agy_gate
    uv run python -m tests.e2e_modal.agy_gate --model gemini-3.8-flash-low

Exit codes: 0 = PASS, 1 = FAIL, 2 = prerequisites missing (SKIP).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import modal  # noqa: E402
from runtime.image import sbx_runtime_image  # noqa: E402
from runtime.runner.adapters.antigravity import OAUTH_TOKEN_REL  # noqa: E402

from tests.e2e_modal.helpers import artifacts_dir, leak_reason, write_json  # noqa: E402

WORK = "/work"
HOME_DIR = f"{WORK}/home"
APP_NAME = "sbx-agy-gate"
GATE_TAG = "sor-62-agy"
DEFAULT_MODEL = "gemini-3.8-flash-low"
DEFAULT_AGY_BIN = Path.home() / ".local/bin/agy"
DEFAULT_TOKEN_FILE = Path.home() / OAUTH_TOKEN_REL
RUNNER = ("python", "-m", "runtime.runner")
MARKER_FILE = f"{HOME_DIR}/agy_gate_marker.txt"
STALE_ID = "00000000-0000-4000-8000-000000000000"

RESULTS: dict[str, Any] = {}
CHECKS: list[dict[str, Any]] = []


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def record(key: str, value: object) -> None:
    RESULTS[key] = value
    print(f"[result] {key} = {json.dumps(value, ensure_ascii=False, default=str)}", flush=True)


def check(name: str, ok: bool, detail: str | None = None) -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    mark = "ok" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def sh(sb: modal.Sandbox, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    proc = sb.exec("bash", "-c", cmd, timeout=timeout)
    out = proc.stdout.read()
    err = proc.stderr.read()
    rc = proc.wait()
    return rc, out, err


def runner(sb: modal.Sandbox, *args: str, timeout: int = 700) -> tuple[int, str, str]:
    """Run ``python -m runtime.runner``, streaming canonical events live.

    Only safe for ``init``/``turn``: runner stdout there is the canonical
    event stream (redacted). ``export-credentials`` prints the credential
    blob on stdout — use ``runner_quiet`` for that.
    """
    proc = sb.exec(*RUNNER, *args, timeout=timeout)
    lines: list[str] = []
    for line in proc.stdout:
        lines.append(line)
        print(f"  [runner] {line.rstrip()[:300]}", flush=True)
    err = proc.stderr.read()
    rc = proc.wait()
    return rc, "".join(lines), err


def runner_quiet(sb: modal.Sandbox, *args: str, timeout: int = 120) -> tuple[int, str, str]:
    """Like ``runner`` but never echoes stdout (may carry secrets)."""
    proc = sb.exec(*RUNNER, *args, timeout=timeout)
    out = proc.stdout.read()
    err = proc.stderr.read()
    rc = proc.wait()
    return rc, out, err


def write_remote(sb: modal.Sandbox, dest: str, content: str) -> int:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    script = (
        "import base64, pathlib;"
        f"p=pathlib.Path({dest!r});"
        "p.parent.mkdir(parents=True, exist_ok=True);"
        f"p.write_bytes(base64.b64decode({encoded!r}))"
    )
    proc = sb.exec("python", "-c", script, timeout=30)
    for _ in proc.stdout:
        pass
    return proc.wait()


def read_remote(sb: modal.Sandbox, dest: str) -> str | None:
    rc, out, _err = sh(sb, f"cat {dest}")
    return out if rc == 0 else None


def load_credential(args: argparse.Namespace) -> tuple[str, str] | None:
    """Return ``(blob_json, source_label)``; never logs contents.

    Resolution order: ``SBX_ACCOUNT_CREDENTIAL`` (must already be an
    antigravity blob), then the token file (``--token-file``).
    """
    raw = os.environ.get("SBX_ACCOUNT_CREDENTIAL")
    if raw:
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if blob.get("provider") != "antigravity" or not isinstance(blob.get("files"), dict):
            return None
        return raw, "env:SBX_ACCOUNT_CREDENTIAL"
    token_file = Path(args.token_file).expanduser()
    if not token_file.is_file():
        return None
    blob = json.dumps(
        {"provider": "antigravity", "files": {OAUTH_TOKEN_REL: token_file.read_text()}}
    )
    return blob, str(token_file).replace(str(Path.home()), "~")


def credential_watch_values(blob: str) -> list[str]:
    """Secret substrings that must never appear in captured output."""
    values = [blob]
    try:
        files = json.loads(blob).get("files") or {}
    except json.JSONDecodeError:
        files = {}
    for content in files.values():
        if isinstance(content, str) and len(content) >= 8:
            values.append(content)
            try:
                token_doc = json.loads(content)
            except json.JSONDecodeError:
                token_doc = None
            if isinstance(token_doc, dict):
                for key in ("id_token", "token", "access_token", "refresh_token"):
                    val = token_doc.get(key)
                    if isinstance(val, str) and len(val) >= 8:
                        values.append(val)
    return values


def scan_leaks(label: str, text: str, secrets: list[str]) -> bool:
    """Check ``text`` for credential material; never prints the secret."""
    if not text:
        return check(f"leak:{label}", True)
    for i, secret in enumerate(secrets):
        if secret and secret in text:
            return check(f"leak:{label}", False, f"credential value #{i} present")
    why = leak_reason(text)
    if why:
        return check(f"leak:{label}", False, why)
    return check(f"leak:{label}", True)


def parse_events(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def turn_slice(events: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Events between ``sbx.turn_started{n}`` and the next turn_started."""
    out: list[dict[str, Any]] = []
    active = False
    for ev in events:
        if ev.get("type") == "sbx.turn_started":
            active = ev.get("n") == n
            continue
        if active:
            out.append(ev)
    return out


def run_gate(sb: modal.Sandbox, args: argparse.Namespace, secrets: list[str]) -> None:
    marker = f"AGY-GATE-{uuid.uuid4().hex[:8].upper()}"
    account_id = "agy-gate"
    record("marker", marker)

    rc, out, err = sh(sb, "agy --version", timeout=60)
    check("agy.version", rc == 0 and bool(out.strip()), (out or err).strip()[-120:])

    # --- runner init ------------------------------------------------------
    rc, _out, err = runner(
        sb,
        "init",
        "--provider",
        "antigravity",
        "--model",
        args.model,
        "--account-id",
        account_id,
        timeout=120,
    )
    if not check("init.rc0", rc == 0, err.strip()[-200:] if rc else None):
        return

    rc, out, _err = sh(
        sb,
        f"stat -c '%a' '{HOME_DIR}/{OAUTH_TOKEN_REL}' "
        f"'{HOME_DIR}/.gemini' '{HOME_DIR}/.gemini/antigravity-cli'",
    )
    modes = out.strip().splitlines()
    check("init.token_mode_600", rc == 0 and modes[:1] == ["600"], f"modes={modes}")
    check("init.dir_modes_700", rc == 0 and modes[1:] == ["700", "700"], f"modes={modes}")

    session = json.loads(read_remote(sb, f"{WORK}/session.json") or "{}")
    check("init.session_provider", session.get("provider") == "antigravity")
    check("init.session_account", session.get("account_id") == account_id)
    check(
        "init.credential_files",
        session.get("credential_files") == [OAUTH_TOKEN_REL],
        str(session.get("credential_files")),
    )

    # Fast auth probe: `agy models` exits 1 immediately on a bad token
    # (SOR-60 spike item 10), avoiding the ~60s OAuth wait of a full turn.
    rc, out, err = sh(sb, "agy models", timeout=120)
    check("agy.auth_probe", rc == 0, "models ok" if rc == 0 else (err or out).strip()[-200:])
    if rc != 0:
        return

    # --- turn 1 -----------------------------------------------------------
    p1 = (
        f"Remember this exact phrase for the rest of our conversation: {marker}\n"
        f"Use your file tools to create the file {MARKER_FILE} "
        "whose entire contents are exactly that phrase and nothing else.\n"
        "Then reply with exactly this line and nothing else: AGY_TURN1_DONE"
    )
    t0 = time.monotonic()
    check("turn1.prompt_written", write_remote(sb, f"{WORK}/_prompt_1.md", p1) == 0)
    rc, out1, err1 = runner(
        sb,
        "turn",
        "--n",
        "1",
        "--message-file",
        f"{WORK}/_prompt_1.md",
        timeout=args.turn_timeout,
    )
    turn1_s = round(time.monotonic() - t0, 1)
    check("turn1.rc0", rc == 0, f"rc={rc} wall={turn1_s}s {err1.strip()[-150:]}")

    events = parse_events(read_remote(sb, f"{WORK}/events.jsonl") or "")
    raw = parse_events(read_remote(sb, f"{WORK}/events.raw.jsonl") or "")
    turn1 = json.loads(read_remote(sb, f"{WORK}/turns/1.json") or "{}")
    session = json.loads(read_remote(sb, f"{WORK}/session.json") or "{}")

    meta = events[0] if events else {}
    check("turn1.session_meta_first", meta.get("type") == "sbx.session_meta")
    check("turn1.session_meta_provider", meta.get("provider") == "antigravity")
    check(
        "turn1.session_meta_account",
        meta.get("account_id") == account_id and meta.get("model") == args.model,
    )
    check("turn1.turn_started", {"type": "sbx.turn_started", "n": 1} in events)

    started = [e for e in events if e.get("type") == "thread.started"]
    conv_id = started[0].get("thread_id") if started else None
    check("turn1.thread_started_once", len(started) == 1 and bool(conv_id), str(conv_id))
    raw_init = next((e for e in raw if e.get("event") == "init"), {})
    check(
        "turn1.raw_init_id_match",
        raw_init.get("conversation_id") == conv_id,
        f"init.conversation_id={raw_init.get('conversation_id')}",
    )
    check("turn1.session_native_id", session.get("native_session_id") == conv_id)

    completed = [e for e in events if e.get("type") == "turn.completed"]
    usage = completed[-1].get("usage") if completed else {}
    check("turn1.turn_completed", bool(completed))
    check(
        "turn1.usage_fields",
        isinstance(usage, dict)
        and all(isinstance(usage.get(k), int) for k in ("input_tokens", "output_tokens")),
        str(usage),
    )
    last = events[-1] if events else {}
    check(
        "turn1.finished_success",
        last.get("type") == "sbx.turn_finished"
        and last.get("status") == "success"
        and last.get("exit_code") == 0,
    )
    check(
        "turn1.turn_doc",
        turn1.get("status") == "success" and turn1.get("native_session_id") == conv_id,
    )
    check("turn1.agent_message", bool(str(turn1.get("message") or "").strip()))

    marker_body = read_remote(sb, MARKER_FILE) or ""
    check("turn1.marker_file", marker in marker_body, f"len={len(marker_body)}")
    record("turn1", {"wall_s": turn1_s, "conversation_id": conv_id, "usage": usage})

    # --- turn 2 (real resume; continuity proven by recall) -----------------
    p2 = (
        "Without reading any files or running any commands, what exact phrase did I "
        "ask you to remember earlier in this conversation? Reply with exactly that "
        "phrase and nothing else."
    )
    t0 = time.monotonic()
    check("turn2.prompt_written", write_remote(sb, f"{WORK}/_prompt_2.md", p2) == 0)
    rc, _out2, err2 = runner(
        sb,
        "turn",
        "--n",
        "2",
        "--message-file",
        f"{WORK}/_prompt_2.md",
        timeout=args.turn_timeout,
    )
    turn2_s = round(time.monotonic() - t0, 1)
    check("turn2.rc0", rc == 0, f"rc={rc} wall={turn2_s}s {err2.strip()[-150:]}")

    events = parse_events(read_remote(sb, f"{WORK}/events.jsonl") or "")
    raw = parse_events(read_remote(sb, f"{WORK}/events.raw.jsonl") or "")
    turn2 = json.loads(read_remote(sb, f"{WORK}/turns/2.json") or "{}")
    session = json.loads(read_remote(sb, f"{WORK}/session.json") or "{}")
    slice2 = turn_slice(events, 2)

    resumed = [e for e in slice2 if e.get("type") == "thread.started"]
    check(
        "turn2.resume_same_thread",
        bool(resumed) and resumed[0].get("thread_id") == conv_id,
        str(resumed[0].get("thread_id") if resumed else None),
    )
    check("turn2.session_id_stable", session.get("native_session_id") == conv_id)
    check("turn2.session_turn_2", session.get("turn") == 2)
    check(
        "turn2.session_meta_once",
        sum(1 for e in events if e.get("type") == "sbx.session_meta") == 1,
    )
    reply = str(turn2.get("message") or "")
    check(
        "turn2.recalls_marker",
        marker in reply,
        "model recalled the phrase" if marker in reply else "reply did not contain the phrase",
    )
    raw_inits = [e for e in raw if e.get("event") == "init"]
    check(
        "turn2.raw_init_same_id",
        len(raw_inits) >= 2 and raw_inits[1].get("conversation_id") == conv_id,
    )
    record("turn2", {"wall_s": turn2_s, "usage": turn2.get("usage")})

    # --- turn 3: stale resume protection ----------------------------------
    # Corrupt session.json with a conversation id that does not exist. The
    # real CLI warns on stderr, exits 0, and silently opens a NEW
    # conversation; the runner must fail the turn (exit 2) and keep the
    # requested id rather than forking the session onto the new one.
    corrupt = (
        "import json, pathlib\n"
        f"p = pathlib.Path({f'{WORK}/session.json'!r})\n"
        "s = json.loads(p.read_text())\n"
        f"s['native_session_id'] = {STALE_ID!r}\n"
        f"s['codex_session_id'] = {STALE_ID!r}\n"
        "p.write_text(json.dumps(s) + '\\n')\n"
    )
    encoded = base64.b64encode(corrupt.encode()).decode("ascii")
    rc, _o, err = sh(sb, f"echo {encoded} | base64 -d | python3")
    check("turn3.session_corrupt_rc0", rc == 0, err.strip()[-150:] if rc else None)
    session = json.loads(read_remote(sb, f"{WORK}/session.json") or "{}")
    check("turn3.session_corrupted", session.get("native_session_id") == STALE_ID)
    check(
        "turn3.prompt_written",
        write_remote(sb, f"{WORK}/_prompt_3.md", "Reply with exactly: AGY_STALE_DONE") == 0,
    )
    rc, _out3, err3 = runner(
        sb,
        "turn",
        "--n",
        "3",
        "--message-file",
        f"{WORK}/_prompt_3.md",
        timeout=args.turn_timeout,
    )
    check("turn3.stale_rc2", rc == 2, f"rc={rc} {err3.strip()[-150:]}")

    events = parse_events(read_remote(sb, f"{WORK}/events.jsonl") or "")
    raw = parse_events(read_remote(sb, f"{WORK}/events.raw.jsonl") or "")
    turn3 = json.loads(read_remote(sb, f"{WORK}/turns/3.json") or "{}")
    session = json.loads(read_remote(sb, f"{WORK}/session.json") or "{}")
    slice3 = turn_slice(events, 3)
    raw_inits = [e for e in raw if e.get("event") == "init"]

    check("turn3.doc_codex_error", turn3.get("status") == "codex_error", str(turn3.get("status")))
    check(
        "turn3.error_event",
        any(e.get("type") == "error" and "not found" in str(e.get("message")) for e in slice3),
    )
    check("turn3.turn_failed", any(e.get("type") == "turn.failed" for e in slice3))
    check("turn3.no_thread_started", not any(e.get("type") == "thread.started" for e in slice3))
    check(
        "turn3.cli_opened_new_conv",
        len(raw_inits) >= 3 and raw_inits[2].get("conversation_id") not in (None, STALE_ID),
        f"new={raw_inits[2].get('conversation_id') if len(raw_inits) >= 3 else None}",
    )
    check(
        "turn3.session_keeps_requested_id",
        session.get("native_session_id") == STALE_ID,
        str(session.get("native_session_id")),
    )

    # --- export-credentials (stdout may be the refreshed blob: quiet) ------
    rc, out_exp, err_exp = runner_quiet(sb, "export-credentials", timeout=60)
    check("export.rc0", rc == 0, err_exp.strip()[-150:] if rc else None)
    out_exp = out_exp.strip()
    exported: dict[str, Any] = {"changed": bool(out_exp)}
    if out_exp:
        try:
            blob = json.loads(out_exp)
        except json.JSONDecodeError:
            blob = None
        check(
            "export.blob_shape",
            isinstance(blob, dict)
            and blob.get("provider") == "antigravity"
            and isinstance(blob.get("files"), dict)
            and OAUTH_TOKEN_REL in blob["files"],
            f"keys={sorted(blob['files']) if isinstance(blob, dict) else 'unparseable'}",
        )
        exported["files"] = sorted(blob["files"]) if isinstance(blob, dict) else []
        exported["hash16"] = hashlib.sha256(out_exp.encode()).hexdigest()[:16]
        secrets.append(out_exp)
    record("export_credentials", exported)

    # --- leak scan ---------------------------------------------------------
    captured: dict[str, str] = {
        "events.jsonl": read_remote(sb, f"{WORK}/events.jsonl") or "",
        "events.raw.jsonl": read_remote(sb, f"{WORK}/events.raw.jsonl") or "",
        "session.json": read_remote(sb, f"{WORK}/session.json") or "",
        "turn1.stdout": out1,
    }
    for rel in (
        "turns/1.json",
        "turns/2.json",
        "turns/3.json",
        "turns/1.stderr",
        "turns/2.stderr",
        "turns/3.stderr",
        "inbox/1.md",
        "inbox/2.md",
    ):
        captured[rel] = read_remote(sb, f"{WORK}/{rel}") or ""
    leaks_ok = True
    for label, text in captured.items():
        leaks_ok = scan_leaks(label, text, secrets) and leaks_ok
    check("leak.all_captured", leaks_ok)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument(
        "--agy-bin",
        default=str(DEFAULT_AGY_BIN),
        help="host path to the real agy binary (baked into a derived image)",
    )
    ap.add_argument(
        "--token-file",
        default=str(DEFAULT_TOKEN_FILE),
        help="host path to the antigravity oauth token file",
    )
    ap.add_argument("--app", default=APP_NAME)
    ap.add_argument(
        "--turn-timeout",
        type=int,
        default=700,
        help="per-turn sandbox exec timeout (runner --max-seconds stays 600)",
    )
    args = ap.parse_args()

    artifacts_dir()
    agy_bin = Path(args.agy_bin).expanduser()
    cred = load_credential(args)
    prereq_missing = []
    if not agy_bin.is_file():
        prereq_missing.append(f"agy binary not found: {agy_bin}")
    if cred is None:
        prereq_missing.append(
            "no antigravity credential (set SBX_ACCOUNT_CREDENTIAL or "
            f"point --token-file at {OAUTH_TOKEN_REL})"
        )
    if not (Path.home() / ".modal.toml").is_file() and not os.environ.get("MODAL_TOKEN_ID"):
        prereq_missing.append("no Modal credentials (~/.modal.toml or MODAL_TOKEN_ID)")
    if prereq_missing:
        for msg in prereq_missing:
            print(f"[skip] {msg}", flush=True)
        RESULTS["verdict"] = "SKIP"
        write_json("agy_gate.json", {"verdict": "SKIP", "missing": prereq_missing})
        return 2
    assert cred is not None
    blob, source = cred
    secrets = credential_watch_values(blob)
    run_id = f"agy-{int(time.time())}"
    record(
        "0.params",
        {
            "modal": modal.__version__,
            "model": args.model,
            "agy_bin": str(agy_bin).replace(str(Path.home()), "~"),
            "agy_bin_bytes": agy_bin.stat().st_size,
            "credential_source": source,
            "credential_hash16": hashlib.sha256(blob.encode()).hexdigest()[:16],
            "run_id": run_id,
        },
    )

    app = modal.App.lookup(args.app, create_if_missing=True)
    t0 = time.perf_counter()
    log("building/checking derived image (sbx-runtime + host agy binary)")
    image = (
        sbx_runtime_image()
        .add_local_file(str(agy_bin), "/usr/local/bin/agy")
        .run_commands("chmod 755 /usr/local/bin/agy")
    )
    image.build(app)
    record("0.image_build_or_check_s", round(time.perf_counter() - t0, 1))

    sb: modal.Sandbox | None = None
    env = {
        "SBX_WORK": WORK,
        "CODEX_HOME": f"{WORK}/.codex",
        "HOME": HOME_DIR,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": "/opt/sbx",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "SBX_ACCOUNT_ID": "agy-gate",
    }
    verdict = "FAIL"
    try:
        sb = modal.Sandbox.create(
            "sleep",
            "infinity",
            app=app,
            image=image,
            secrets=[modal.Secret.from_dict({"SBX_ACCOUNT_CREDENTIAL": blob})],
            env=env,
            cpu=(1, 2),
            memory=(1024, 4096),
            timeout=3600,
            workdir=WORK,
            tags={"gate": GATE_TAG, "run": run_id},
        )
        record("1.sandbox", {"id": sb.object_id, "image": "sbx-runtime+agy"})
        run_gate(sb, args, secrets)
    except Exception:  # noqa: BLE001
        record("error", traceback.format_exc()[-2000:])
    finally:
        if sb is not None:
            try:
                sb.terminate()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(3)
        left = [s.object_id for s in modal.Sandbox.list(app_id=app.app_id, tags={"gate": GATE_TAG})]
        for sid in left:
            try:
                modal.Sandbox.from_id(sid).terminate()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(3)
        still = [
            s.object_id for s in modal.Sandbox.list(app_id=app.app_id, tags={"gate": GATE_TAG})
        ]
        check("cleanup.zero_leftovers", not still, f"left={still}")
        record("9.leftovers", {"terminated": left, "remaining": still})
        if CHECKS and all(c["ok"] for c in CHECKS):
            verdict = "PASS"
        RESULTS["verdict"] = verdict
        RESULTS["failed_checks"] = [c["name"] for c in CHECKS if not c["ok"]]
        RESULTS["checks"] = CHECKS
        path = write_json("agy_gate.json", RESULTS)
        log(f"verdict={verdict} failed={RESULTS['failed_checks']} artifact={path}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
