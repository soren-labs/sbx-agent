"""SOR-62 gate: real Modal e2e for the PRODUCTION Grok adapter.

Host-executed. Creates one real Modal Sandbox on a derived image (the
production ``sbx_runtime_image()`` plus the host's ``grok`` binary — the
named ``sbx-runtime`` image does not ship ``grok`` yet) and injects the
account credential via an ephemeral Secret carrying the exact production
``SBX_ACCOUNT_CREDENTIAL`` blob contract. It then drives
``python -m runtime.runner`` exactly like the control plane does:

    init -> turn 1 -> turn 2 (--resume) -> turn 3 (stale id)
         -> export-credentials -> terminate -> leftover scan

Coverage: real two-turn continuity (the model must recall a phrase without
re-reading files), canonical event shape (``sbx.session_meta`` once,
``thread.started`` id stability, ``turn.completed`` usage,
``sbx.turn_finished``), stale-resume behaviour (the real CLI exits rc=1
with empty stdout and a stderr ``Failed to restore session ... 404``
signature — GROK_SPIKE.md item 7 — so the runner must fail the turn with
exit 2 and keep the requested id), credential state (``.grok/auth.json``
stays 600; ``export-credentials`` prints nothing unless the CLI refreshed
the file, then a valid grok blob; contents never logged), child-env scrub
(``SBX_ACCOUNT_CREDENTIAL`` and every ``GROK_*``/``XAI_*`` alternate auth
channel stripped by ``codex.child_env``), leak checks (token / JWT / sk-
patterns plus the native ``signature`` blob over every captured stream and
artifact), and cleanup (zero tagged sandboxes left running).

Real streaming-json has **no** ``init`` line: ``end.sessionId`` is the
first session marker, so raw-event assertions key on ``end`` events, not
``init`` (unlike the agy gate).

Secret hygiene: credential material is never printed. Only a sha256-16
fingerprint, file relpaths, and booleans are recorded. ``runner
export-credentials`` stdout is captured without echoing. Prerequisites are
checked up front; without them the script exits 2 with verdict SKIP — it
never reports a real-account PASS it did not execute.

Run (from repo root, real grok + ~/.modal.toml required):

    uv run python -m tests.e2e_modal.grok_gate
    uv run python -m tests.e2e_modal.grok_gate --model grok-4.6

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
from runtime.runner.adapters.grok import GROK_AUTH_REL  # noqa: E402

from tests.e2e_modal.helpers import artifacts_dir, leak_reason, write_json  # noqa: E402

WORK = "/work"
HOME_DIR = f"{WORK}/home"
APP_NAME = "sbx-grok-gate"
GATE_TAG = "sor-62-grok"
DEFAULT_MODEL = "grok-4.6"
DEFAULT_GROK_BIN = Path.home() / ".local/bin/grok"
DEFAULT_CRED_FILE = Path.home() / GROK_AUTH_REL
RUNNER = ("python", "-m", "runtime.runner")
MARKER_FILE = f"{WORK}/grok_gate_marker.txt"
STALE_ID = "00000000-0000-0000-0000-000000000000"

# Keys whose string values inside .grok/auth.json are credential material
# (the real file is {"<issuer>::<uuid>": {key, refresh_token, ...}}).
_SECRET_VALUE_KEYS = {
    "key",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "secret",
    "signature",
}

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

    Resolution order: ``SBX_ACCOUNT_CREDENTIAL`` (must already be a grok
    blob), then the credential file (``--cred-file``).
    """
    raw = os.environ.get("SBX_ACCOUNT_CREDENTIAL")
    if raw:
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if blob.get("provider") != "grok" or not isinstance(blob.get("files"), dict):
            return None
        return raw, "env:SBX_ACCOUNT_CREDENTIAL"
    cred_file = Path(args.cred_file).expanduser()
    if not cred_file.is_file():
        return None
    blob = json.dumps({"provider": "grok", "files": {GROK_AUTH_REL: cred_file.read_text()}})
    return blob, str(cred_file).replace(str(Path.home()), "~")


def _secret_strings(node: Any, depth: int = 0) -> list[str]:
    """Collect credential-looking string values from parsed JSON."""
    out: list[str] = []
    if depth > 6:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                isinstance(value, str)
                and str(key).lower() in _SECRET_VALUE_KEYS
                and len(value) >= 8
            ):
                out.append(value)
            else:
                out.extend(_secret_strings(value, depth + 1))
    elif isinstance(node, list):
        for item in node:
            out.extend(_secret_strings(item, depth + 1))
    return out


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
                doc = json.loads(content)
            except json.JSONDecodeError:
                doc = None
            if doc is not None:
                values.extend(_secret_strings(doc))
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
    marker = f"GROK-GATE-{uuid.uuid4().hex[:8].upper()}"
    account_id = "grok-gate"
    record("marker", marker)

    rc, out, err = sh(sb, "grok --version", timeout=60)
    check("grok.version", rc == 0 and bool(out.strip()), (out or err).strip()[-120:])

    # --- env hygiene (names only, never values) -----------------------------
    rc, out, _err = sh(sb, "env | cut -d= -f1 | sort")
    env_names = set(out.split())
    check("env.credential_present", "SBX_ACCOUNT_CREDENTIAL" in env_names)
    auth_envs = sorted(n for n in env_names if n.startswith(("GROK_", "XAI_")))
    check("env.no_grok_auth_vars", not auth_envs, f"unexpected={auth_envs}")

    # The production scrub applied to the provider CLI child env: the
    # injected blob and every alternate grok auth channel must be absent
    # so the restored .grok/auth.json is the only credential source.
    rc, out, err = sh(
        sb,
        'python -c "'
        "from pathlib import Path;"
        "from runtime.runner.codex import child_env;"
        "e = child_env(Path('/work'), Path('/work/.codex'));"
        "print('CRED_IN_CHILD=%s' % ('SBX_ACCOUNT_CREDENTIAL' in e));"
        "print('CHILD_AUTH_NAMES=%s' % sorted(k for k in e if k.startswith(('GROK_', 'XAI_'))))"
        '"',
    )
    check(
        "env.child_scrub",
        rc == 0 and "CRED_IN_CHILD=False" in out and "CHILD_AUTH_NAMES=[]" in out,
        (out or err).strip()[-200:],
    )

    # --- runner init ------------------------------------------------------
    rc, _out, err = runner(
        sb,
        "init",
        "--provider",
        "grok",
        "--model",
        args.model,
        "--account-id",
        account_id,
        timeout=120,
    )
    if not check("init.rc0", rc == 0, err.strip()[-200:] if rc else None):
        return

    rc, out, _err = sh(sb, f"stat -c '%a' '{HOME_DIR}/{GROK_AUTH_REL}' '{HOME_DIR}/.grok'")
    modes = out.strip().splitlines()
    check("init.auth_mode_600", rc == 0 and modes[:1] == ["600"], f"modes={modes}")
    check("init.grok_dir_700", rc == 0 and modes[1:] == ["700"], f"modes={modes}")

    session = json.loads(read_remote(sb, f"{WORK}/session.json") or "{}")
    check("init.session_provider", session.get("provider") == "grok")
    check("init.session_account", session.get("account_id") == account_id)
    check(
        "init.credential_files",
        session.get("credential_files") == [GROK_AUTH_REL],
        str(session.get("credential_files")),
    )

    _rc, h_before, _e = sh(sb, f"sha256sum '{HOME_DIR}/{GROK_AUTH_REL}' | cut -c1-16")
    cred_hash_before = h_before.strip()
    record("cred.hash16_before", cred_hash_before)

    # Fast auth probe: `grok models` exits 0 regardless of auth, so grep
    # the status line instead of trusting rc (GROK_SPIKE.md item 8).
    rc, out, err = sh(sb, f"cd {WORK} && grok models </dev/null", timeout=180)
    first = out.strip().splitlines()[0] if out.strip() else ""
    check(
        "grok.auth_probe",
        rc == 0 and "logged in" in out.lower() and "not authenticated" not in out.lower(),
        first[:160],
    )
    if rc != 0 or "logged in" not in out.lower():
        return

    # --- turn 1 -----------------------------------------------------------
    p1 = (
        f"Remember this exact phrase for the rest of our conversation: {marker}\n"
        f"Use your file tools to create the file {MARKER_FILE} "
        "whose entire contents are exactly that phrase and nothing else.\n"
        "Then reply with exactly this line and nothing else: GROK_TURN1_DONE"
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
    check("turn1.session_meta_provider", meta.get("provider") == "grok")
    check(
        "turn1.session_meta_account",
        meta.get("account_id") == account_id and meta.get("model") == args.model,
    )
    check("turn1.turn_started", {"type": "sbx.turn_started", "n": 1} in events)

    started = [e for e in events if e.get("type") == "thread.started"]
    conv_id = started[0].get("thread_id") if started else None
    check("turn1.thread_started_once", len(started) == 1 and bool(conv_id), str(conv_id))
    # Real streaming-json has no init line; end.sessionId is the marker.
    raw_ends = [e for e in raw if e.get("type") == "end"]
    check(
        "turn1.raw_end_id_match",
        bool(raw_ends) and raw_ends[-1].get("sessionId") == conv_id,
        f"end.sessionId={raw_ends[-1].get('sessionId') if raw_ends else None}",
    )
    check(
        "turn1.raw_no_init",
        not any(e.get("type") in ("init", "system.init") for e in raw),
    )
    check("turn1.session_native_id", session.get("native_session_id") == conv_id)

    completed = [e for e in events if e.get("type") == "turn.completed"]
    usage = completed[-1].get("usage") if completed else {}
    check("turn1.turn_completed", bool(completed))
    check(
        "turn1.usage_fields",
        isinstance(usage, dict)
        and all(isinstance(usage.get(k), int) for k in ("input_tokens", "output_tokens"))
        and int(usage.get("input_tokens") or 0) > 0,
        str(usage),
    )
    # The step-level `signature` blob must never survive into raw storage.
    sigs = [
        e["usage"]["signature"]
        for e in raw
        if isinstance(e.get("usage"), dict) and "signature" in e["usage"]
    ]
    check(
        "turn1.signature_redacted",
        all(s == "REDACTED" for s in sigs),
        f"signature_lines={len(sigs)}",
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
        turn1.get("status") == "success"
        and turn1.get("native_session_id") == conv_id
        and turn1.get("bad_json_lines") == 0,
    )
    check("turn1.agent_message", bool(str(turn1.get("message") or "").strip()))

    marker_body = read_remote(sb, MARKER_FILE) or ""
    check("turn1.marker_file", marker in marker_body, f"len={len(marker_body)}")
    record("turn1", {"wall_s": turn1_s, "session_id": conv_id, "usage": usage})

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
    raw_ends = [e for e in raw if e.get("type") == "end"]
    check(
        "turn2.raw_end_same_id",
        len(raw_ends) >= 2 and raw_ends[1].get("sessionId") == conv_id,
        f"ends={len(raw_ends)}",
    )
    usage2 = turn2.get("usage") or {}
    check(
        "turn2.usage_fields",
        isinstance(usage2, dict)
        and all(isinstance(usage2.get(k), int) for k in ("input_tokens", "output_tokens"))
        and int(usage2.get("input_tokens") or 0) > 0
        and turn2.get("bad_json_lines") == 0,
        str(usage2),
    )
    record("turn2", {"wall_s": turn2_s, "usage": usage2})
    raw_lines_before_stale = len((read_remote(sb, f"{WORK}/events.raw.jsonl") or "").splitlines())

    # --- turn 3: stale resume ---------------------------------------------
    # Corrupt session.json with a session id that does not exist. The real
    # CLI exits rc=1 with EMPTY stdout and a stderr "Failed to restore
    # session from remote: ... 404 Not Found" signature (GROK_SPIKE.md
    # item 7) — no error line reaches the event stream — so the runner's
    # rc path must fail the turn (exit 2, codex_error) and keep the
    # requested id rather than adopting anything new.
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
        write_remote(sb, f"{WORK}/_prompt_3.md", "Reply with exactly: GROK_STALE_DONE") == 0,
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
    turn3 = json.loads(read_remote(sb, f"{WORK}/turns/3.json") or "{}")
    session = json.loads(read_remote(sb, f"{WORK}/session.json") or "{}")
    slice3 = turn_slice(events, 3)
    stderr3 = read_remote(sb, f"{WORK}/turns/3.stderr") or ""
    raw_lines_after_stale = len((read_remote(sb, f"{WORK}/events.raw.jsonl") or "").splitlines())

    check("turn3.doc_codex_error", turn3.get("status") == "codex_error", str(turn3.get("status")))
    check("turn3.no_thread_started", not any(e.get("type") == "thread.started" for e in slice3))
    check("turn3.no_turn_completed", not any(e.get("type") == "turn.completed" for e in slice3))
    fin3 = next((e for e in slice3 if e.get("type") == "sbx.turn_finished"), {})
    check(
        "turn3.finished_codex_error",
        fin3.get("status") == "codex_error" and fin3.get("exit_code") == 2,
        str(fin3.get("status")),
    )
    check(
        "turn3.cli_stdout_empty",
        raw_lines_after_stale == raw_lines_before_stale,
        f"raw_lines {raw_lines_before_stale} -> {raw_lines_after_stale}",
    )
    check(
        "turn3.stderr_signature",
        "Failed to restore session" in stderr3 and "404" in stderr3,
        "restore-404 signature present" if stderr3 else "stderr empty",
    )
    check(
        "turn3.session_keeps_requested_id",
        session.get("native_session_id") == STALE_ID,
        str(session.get("native_session_id")),
    )
    record(
        "stale",
        {
            "rc": rc,
            "status": turn3.get("status"),
            "health": turn3.get("health"),
            "stdout_empty": raw_lines_after_stale == raw_lines_before_stale,
        },
    )

    # --- credential state after real turns ---------------------------------
    rc, out, _err = sh(sb, f"stat -c '%a' '{HOME_DIR}/{GROK_AUTH_REL}'")
    check("cred.still_600", rc == 0 and out.strip() == "600", f"mode={out.strip()}")
    _rc, h_after, _e = sh(sb, f"sha256sum '{HOME_DIR}/{GROK_AUTH_REL}' | cut -c1-16")
    cred_changed = bool(h_after.strip()) and h_after.strip() != cred_hash_before
    record("cred.hash16_after", h_after.strip())
    record("cred.hash_changed", cred_changed)
    _rc, listing, _e = sh(
        sb,
        f"find '{HOME_DIR}/.grok' -type f | sed 's|^{HOME_DIR}/||' | sort | head -40",
    )
    record("cred.home_files", listing.strip().splitlines())

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
            and blob.get("provider") == "grok"
            and isinstance(blob.get("files"), dict)
            and GROK_AUTH_REL in blob["files"],
            f"keys={sorted(blob['files']) if isinstance(blob, dict) else 'unparseable'}",
        )
        exported["files"] = sorted(blob["files"]) if isinstance(blob, dict) else []
        exported["hash16"] = hashlib.sha256(out_exp.encode()).hexdigest()[:16]
        secrets.append(out_exp)
    check(
        "export.consistent_with_hash",
        bool(out_exp) == cred_changed,
        f"hash_changed={cred_changed} exported={bool(out_exp)}",
    )
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
        "inbox/3.md",
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
        "--grok-bin",
        default=str(DEFAULT_GROK_BIN),
        help="host path to the real grok binary (baked into a derived image)",
    )
    ap.add_argument(
        "--cred-file",
        default=str(DEFAULT_CRED_FILE),
        help=f"host path to the grok credential file ({GROK_AUTH_REL})",
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
    grok_bin = Path(args.grok_bin).expanduser()
    cred = load_credential(args)
    prereq_missing = []
    if not grok_bin.is_file():
        prereq_missing.append(f"grok binary not found: {grok_bin}")
    if cred is None:
        prereq_missing.append(
            "no grok credential (set SBX_ACCOUNT_CREDENTIAL or "
            f"point --cred-file at {GROK_AUTH_REL})"
        )
    if not (Path.home() / ".modal.toml").is_file() and not os.environ.get("MODAL_TOKEN_ID"):
        prereq_missing.append("no Modal credentials (~/.modal.toml or MODAL_TOKEN_ID)")
    if prereq_missing:
        for msg in prereq_missing:
            print(f"[skip] {msg}", flush=True)
        RESULTS["verdict"] = "SKIP"
        write_json("grok_gate.json", {"verdict": "SKIP", "missing": prereq_missing})
        return 2
    assert cred is not None
    blob, source = cred
    secrets = credential_watch_values(blob)
    run_id = f"grok-{int(time.time())}"
    record(
        "0.params",
        {
            "modal": modal.__version__,
            "model": args.model,
            "grok_bin": str(grok_bin).replace(str(Path.home()), "~"),
            "grok_bin_bytes": grok_bin.stat().st_size,
            "credential_source": source,
            "credential_hash16": hashlib.sha256(blob.encode()).hexdigest()[:16],
            "run_id": run_id,
        },
    )

    app = modal.App.lookup(args.app, create_if_missing=True)
    t0 = time.perf_counter()
    log("building/checking derived image (sbx-runtime + host grok binary)")
    image = (
        sbx_runtime_image()
        .add_local_file(str(grok_bin), "/usr/local/bin/grok", copy=True)
        .run_commands("chmod 755 /usr/local/bin/grok")
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
        "SBX_ACCOUNT_ID": "grok-gate",
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
        record("1.sandbox", {"id": sb.object_id, "image": "sbx-runtime+grok"})
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
        path = write_json("grok_gate.json", RESULTS)
        log(f"verdict={verdict} failed={RESULTS['failed_checks']} artifact={path}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
