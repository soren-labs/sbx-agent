"""Release 0.1 core-provider gate: real Modal runs through a deployed /v1 plane.

Host-executed against the dedicated RC control plane (default
``sbx-control-release01-rc``). For each target account the gate drives the
public API exactly as a customer would:

    GET /v1/accounts/{id}          fleet health
    POST /v1/agents                run-1 (provision + runner init + turn)
    GET  .../runs/run-1            poll to a truthful terminal status
    GET  .../runs/run-1/stream     SSE canonical-event capture
    (Modal exec on the tagged sandbox)  on-disk truth: session.json,
        turns/<n>.json, credential-file modes, marker file
    POST /v1/agents/{id}/runs      run-2 follow-up (native-id resume)
    GET  /v1/agents/{id}/usage     usage honesty (measured, never zeros)
    POST .../runs/run-3 + cancel   cancel path -> CANCELLED terminal
    DELETE /v1/agents/{id}         close; stale follow-up refused;
        sandbox leftovers = 0

Lanes for accounts whose live status is not ``active`` degrade to the
honest negative path (``verify`` re-probe, named pick refused with
``account_unavailable``, ``auto`` refused with ``provider_exhausted``) and
are recorded CREDENTIAL_DEFERRED — never a fabricated PASS.

Credential hygiene: secret values are never printed. Leak scans cover the
SSE bodies, run/agent payloads, in-sandbox evidence files and the control
app log tail; watch values are the bearer key itself, the host's Modal
token pair, and the internal basic-auth password when present.

Run (from repo root, real Modal creds in ``$HOME/.modal.toml``):

    SBX_V1_API_KEY="$(cat <bootstrap.key>)" \
        uv run python -m tests.e2e_modal.v1_core_gate --account devin-1

Env: ``SBX_V1_API_KEY``/``SBX_API_KEY`` or ``SBX_KEY_FILE`` (bearer key),
``SBX_BASE_URL`` (default: the RC endpoint), ``SBX_GATE_APP`` (default
``sbx-control-release01-rc``), ``MODAL_PROFILE``.

Exit codes: 0 = PASS, 1 = FAIL, 2 = prerequisites missing (SKIP),
3 = CREDENTIAL_DEFERRED (account not usable; negative path verified).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from tests.e2e_modal.helpers import leak_reason, write_json  # noqa: E402

DEFAULT_BASE_URL = "https://sorenlab2026--sbx-control-release01-rc-fastapi-app.modal.run"
DEFAULT_APP = "sbx-control-release01-rc"
TERMINAL_RUN_STATUSES = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED", "UNKNOWN"}
CLOSED_AGENT_STATUSES = {"closed", "timed_out", "lost"}
CREATE_TIMEOUT_S = 180.0
TURN_TIMEOUT_S = 900.0
CANCEL_TIMEOUT_S = 300.0
POLL_S = 3.0

JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
SK_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")
SBX_KEY_RE = re.compile(r"sbx_[A-Za-z0-9]{16,}")


@dataclass(frozen=True)
class Lane:
    provider: str
    model: str
    # Credential file location inside the sandbox, relative to /work.
    # The blob restores under $HOME (=/work/home); codex routes .codex/*
    # to CODEX_HOME (=/work/.codex).
    cred_path: str


TARGETS: dict[str, Lane] = {
    "codex-1": Lane("codex", "gpt-5.6-luna", ".codex/auth.json"),
    "devin-1": Lane("devin", "swe-2-high", "home/.local/share/devin/credentials.toml"),
    "opencode-1": Lane("opencode", "openai/gpt-5.6-luna", "home/.local/share/opencode/auth.json"),
}

CHECKS: list[dict[str, Any]] = []
RESULTS: dict[str, Any] = {}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(name: str, ok: bool, detail: str | None = None) -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    mark = "ok" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def note(name: str, detail: str) -> None:
    """Record an observation that is not pass/fail (degraded coverage)."""
    CHECKS.append({"name": name, "ok": None, "detail": detail})
    print(f"[note] {name} — {detail}", flush=True)


def record(key: str, value: object) -> None:
    RESULTS[key] = value
    print(f"[result] {key} = {json.dumps(value, ensure_ascii=False, default=str)}", flush=True)


# ------------------------------------------------------------------ secrets


def watch_values() -> list[str]:
    """Secret substrings that must never appear in captured output."""
    out: list[str] = []
    for name in ("SBX_V1_API_KEY", "SBX_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"):
        value = os.environ.get(name)
        if value and len(value) >= 8:
            out.append(value)
    # The host's Modal token file feeds the SDK; include its values so a
    # stray echo into any captured surface is caught.
    modal_toml = Path.home() / ".modal.toml"
    if modal_toml.is_file():
        for m in re.finditer(r'=\s*"([^"]{8,})"', modal_toml.read_text()):
            out.append(m.group(1))
    for name in ("SBX_BASIC_USER", "SBX_BASIC_PASS"):
        value = os.environ.get(name)
        if value and len(value) >= 4:
            out.append(value)
    return out


def scan_leaks(label: str, text: str | None, secrets: list[str]) -> bool:
    if not text:
        return check(f"leak:{label}", True, "empty")
    for i, secret in enumerate(secrets):
        if secret and secret in text:
            return check(f"leak:{label}", False, f"watch value #{i} present")
    if SBX_KEY_RE.search(text):
        return check(f"leak:{label}", False, "sbx_ api-key-shaped token present")
    why = leak_reason(text)
    if why:
        return check(f"leak:{label}", False, why)
    return check(f"leak:{label}", True)


# -------------------------------------------------------------------- /v1


class V1:
    def __init__(self, base_url: str, token: str) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )

    def close(self) -> None:
        self._client.close()

    def req(self, method: str, path: str, **kw: Any) -> tuple[int, Any]:
        resp = self._client.request(method, path, **kw)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"_raw": resp.text[:500]}

    def get(self, path: str) -> tuple[int, Any]:
        return self.req("GET", path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        return self.req("POST", path, json=body)

    def delete(self, path: str) -> tuple[int, Any]:
        return self.req("DELETE", path)

    def stream_run(self, agent_id: str, run_id: str, until: float) -> str:
        """Collect SSE frames until ``sbx.turn_finished`` or ``until``.

        The endpoint is intentionally open-ended (tail -F + keepalives);
        the gate bounds the read itself.
        """
        captured: list[str] = []
        deadline = time.monotonic() + until
        try:
            with self._client.stream(
                "GET",
                f"/v1/agents/{agent_id}/runs/{run_id}/stream",
                timeout=httpx.Timeout(30.0, read=45.0),
            ) as resp:
                for line in resp.iter_lines():
                    captured.append(line)
                    if '"sbx.turn_finished"' in line or '"sbx.error"' in line:
                        break
                    if time.monotonic() > deadline:
                        break
        except httpx.HTTPError as exc:
            captured.append(f"__stream_error__ {exc}")
        return "\n".join(captured)


def err_code(body: Any) -> str | None:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err.get("code")
    return None


def wait_run(client: V1, agent_id: str, run_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _status, body = client.get(f"/v1/agents/{agent_id}/runs/{run_id}")
        if isinstance(body, dict) and body.get("id"):
            last = body
            if body.get("status") in TERMINAL_RUN_STATUSES:
                return body
        time.sleep(POLL_S)
    return last


def wait_agent_status(client: V1, agent_id: str, want: set[str], timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _status, body = client.get(f"/v1/agents/{agent_id}")
        if isinstance(body, dict) and body.get("id"):
            last = body
            if body.get("status") in want:
                return body
        time.sleep(POLL_S)
    return last


def parse_sse(body: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for match in re.finditer(r"^data:\s*(.+)$", body or "", flags=re.M):
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# -------------------------------------------------------------- modal truth


def modal_backend(app_name: str) -> Any | None:
    try:
        from control.backends.modal import ModalBackend

        return ModalBackend(app_name=app_name)
    except Exception:
        return None


def sandbox_handles(backend: Any, session_id: str) -> list[Any]:
    try:
        return backend.list(tags={"session_id": session_id})
    except Exception:
        return []


def sandbox_exec(backend: Any, handle: Any, argv: list[str]) -> tuple[int, str]:
    from control.sandbox_io import sandbox_env

    proc = backend.exec(handle, argv, env=sandbox_env(handle))
    chunks = list(proc.stdout)
    return proc.wait(), "\n".join(chunks)


def sandbox_read(backend: Any, handle: Any, relative: str) -> str | None:
    from control.sandbox_io import read_text

    try:
        return read_text(backend, handle, relative)
    except Exception:
        return None


def mask(text: str | None, secrets: list[str]) -> str | None:
    """Replace every watch value and credential-shaped token in ``text``."""
    if text is None:
        return None
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<WATCH>")
    text = JWT_RE.sub("<JWT>", text)
    text = SK_RE.sub("<SK>", text)
    text = SBX_KEY_RE.sub("<SBX_KEY>", text)
    return text


def failure_evidence(backend: Any, agent_id: str, n: int, secrets: list[str]) -> dict[str, Any]:
    """Masked in-sandbox diagnostics for a failed run (sandbox still warm)."""
    out: dict[str, Any] = {}
    handles = sandbox_handles(backend, agent_id)
    if not handles:
        out["sandbox"] = "gone"
        return out
    handle = handles[0]
    stderr = sandbox_read(backend, handle, f"turns/{n}.stderr")
    raw = sandbox_read(backend, handle, "events.raw.jsonl")
    turn_doc = sandbox_read(backend, handle, f"turns/{n}.json")
    out["stderr_tail"] = mask((stderr or "")[-2000:], secrets)
    raw_lines = (raw or "").splitlines()
    out["raw_tail"] = mask("\n".join(raw_lines[-25:]), secrets)
    out["turn_doc"] = mask(turn_doc, secrets)
    return out


def app_logs(app_name: str, tail: int = 600) -> str:
    env = os.environ.copy()
    env.setdefault("MODAL_PROFILE", "sorenlab2026")
    try:
        proc = subprocess.run(
            ["uv", "run", "modal", "app", "logs", app_name, "--tail", str(tail)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


# ---------------------------------------------------------------- lane body


def gate_invalid_lane(
    client: V1,
    lane: Lane,
    account_id: str,
    acct: dict[str, Any],
    secrets: list[str],
    args: argparse.Namespace,
    backend: Any | None,
) -> str:
    """Honest negative path for a non-active account. Never fakes a run.

    ``verify`` re-probes the stored credential in a throwaway sandbox
    (real ``runner init``); a credential that recovered since seeding
    promotes the lane to the full active matrix instead of deferring.
    """
    http, body = client.post(f"/v1/accounts/{account_id}/verify")
    probed = body if isinstance(body, dict) else {}
    if not check(
        "verify.reprobe",
        http == 200 and probed.get("status") in ("invalid", "cooling", "active"),
        f"http={http} status={probed.get('status')}",
    ):
        return "FAIL"
    record("verify", {"status": probed.get("status"), "last_error": probed.get("last_error")})
    scan_leaks("verify", json.dumps(body), secrets)
    if probed.get("status") == "active":
        note(
            "verify.recovered",
            f"{account_id} re-probed active — promoting to the full lane",
        )
        return gate_active_lane(client, backend, lane, account_id, secrets, args)

    # A named pick on a non-active account must be refused, never spend the
    # credential.
    http, body = client.post(
        "/v1/agents",
        body={
            "prompt": {"text": "Reply with exactly: sbx-ok"},
            "agent": {"provider": lane.provider, "account_id": account_id, "model": lane.model},
            "name": f"gate-{account_id}-named",
        },
    )
    check(
        "create.named_refused",
        http == 409 and err_code(body) in ("account_unavailable", "account_busy"),
        f"http={http} code={err_code(body)}",
    )

    http, body = client.post(
        "/v1/agents",
        body={
            "prompt": {"text": "Reply with exactly: sbx-ok"},
            "agent": {"provider": lane.provider, "account_id": "auto", "model": lane.model},
            "name": f"gate-{account_id}-auto",
        },
    )
    check(
        "create.auto_exhausted",
        http == 429 and err_code(body) == "provider_exhausted",
        f"http={http} code={err_code(body)}",
    )
    return "CREDENTIAL_DEFERRED"


def gate_active_lane(
    client: V1,
    backend: Any | None,
    lane: Lane,
    account_id: str,
    secrets: list[str],
    args: argparse.Namespace,
) -> str:
    provider = lane.provider
    marker = f"GATE-{provider.upper()}-{uuid.uuid4().hex[:8].upper()}"
    marker_file = f"gate_marker_{provider}.txt"
    record("marker", marker)
    created_agent: str | None = None

    try:
        # --- create + run 1 ----------------------------------------------
        t1 = (
            f"Remember this exact phrase for the rest of our conversation: {marker}\n"
            f"Use your file tools to create the file /work/{marker_file} whose entire "
            "contents are exactly that phrase and nothing else.\n"
            f"Then reply with exactly this line and nothing else: {provider.upper()}_TURN1_DONE"
        )
        t0 = time.monotonic()
        http, body = client.post(
            "/v1/agents",
            body={
                "prompt": {"text": t1},
                "agent": {"provider": provider, "account_id": account_id, "model": lane.model},
                "name": f"gate-{account_id}",
            },
        )
        if not check("create.201", http == 201, f"http={http} code={err_code(body)}"):
            return "FAIL"
        agent = body.get("agent") or {}
        run = body.get("run") or {}
        agent_id = str(agent.get("id") or "")
        run_id = str(run.get("id") or "run-1")
        created_agent = agent_id
        check(
            "create.account_pinned",
            agent.get("account_id") == account_id,
            f"account_id={agent.get('account_id')}",
        )
        check("create.model", agent.get("model") == lane.model, f"model={agent.get('model')}")
        check(
            "create.born_queued",
            run.get("status") in ("CREATING", "RUNNING"),
            f"run.status={run.get('status')}",
        )

        log(f"run-1 dispatched on {agent_id}; waiting for terminal state")
        final1 = wait_run(client, agent_id, run_id, args.turn_timeout)
        turn1_s = round(time.monotonic() - t0, 1)
        status1 = final1.get("status")
        if status1 != "FINISHED":
            # An honest structured provider-auth failure is a credential
            # limitation (the plane classified it truthfully), not a product
            # defect — defer the lane, keep the evidence.
            error1 = final1.get("error") if isinstance(final1.get("error"), dict) else None
            if backend is not None:
                evidence = failure_evidence(backend, agent_id, 1, secrets)
                record("run1_evidence", evidence)
            if status1 == "ERROR" and (error1 or {}).get("code") == "auth_invalid":
                note(
                    "run1.auth_invalid",
                    f"error={error1} wall={turn1_s}s — credential unusable upstream",
                )
                scan_leaks("run1_error", json.dumps(final1), secrets)
                record("run1", {"id": run_id, "wall_s": turn1_s, "error": error1})
                return "CREDENTIAL_DEFERRED"
            check(
                "run1.finished",
                False,
                f"status={status1} error={final1.get('error')} wall={turn1_s}s",
            )
            return "FAIL"
        check("run1.result_text", bool(str((final1.get("result") or {}).get("text") or "").strip()))
        usage1 = final1.get("usage")
        check(
            "run1.usage_measured",
            isinstance(usage1, dict)
            and all(isinstance(usage1.get(k), int) for k in ("input_tokens", "output_tokens"))
            and int(usage1.get("input_tokens") or 0) > 0,
            f"usage={usage1}",
        )
        record("run1", {"id": run_id, "wall_s": turn1_s, "usage": usage1})

        # --- SSE canonical events ----------------------------------------
        sse1 = client.stream_run(agent_id, run_id, until=30.0)
        events1 = parse_sse(sse1)
        check("sse1.frames", bool(events1), f"frames={len(events1)}")
        meta = events1[0] if events1 else {}
        check(
            "sse1.session_meta_first",
            meta.get("type") == "sbx.session_meta",
            f"first={meta.get('type')}",
        )
        check("sse1.session_meta_provider", meta.get("provider") == provider)
        check("sse1.session_meta_account", meta.get("account_id") == account_id)
        started = [e for e in events1 if e.get("type") == "thread.started"]
        native_id = started[0].get("thread_id") if started else None
        check(
            "sse1.thread_started", len(started) == 1 and bool(native_id), f"thread_id={native_id}"
        )
        completed = [e for e in events1 if e.get("type") == "turn.completed"]
        check("sse1.turn_completed", bool(completed))
        finished = [e for e in events1 if e.get("type") == "sbx.turn_finished"]
        check(
            "sse1.finished_success",
            bool(finished)
            and finished[-1].get("status") == "success"
            and finished[-1].get("exit_code") == 0,
            f"last={finished[-1] if finished else None}",
        )
        scan_leaks("sse1", sse1, secrets)

        # --- on-disk truth inside the live sandbox ------------------------
        session_native: str | None = None
        if backend is None:
            note("sandbox.modal_unavailable", "no Modal SDK access; on-disk checks skipped")
        else:
            handles = sandbox_handles(backend, agent_id)
            check("sandbox.tagged_one", len(handles) == 1, f"handles={len(handles)}")
            if handles:
                handle = handles[0]
                sess = sandbox_read(backend, handle, "session.json")
                try:
                    session_doc = json.loads(sess or "{}")
                except json.JSONDecodeError:
                    session_doc = {}
                check("disk.session_provider", session_doc.get("provider") == provider)
                check("disk.session_account", session_doc.get("account_id") == account_id)
                session_native = session_doc.get("native_session_id")
                check(
                    "disk.native_id_match",
                    session_native is not None and session_native == native_id,
                    f"native={session_native} stream={native_id}",
                )
                turn1_doc_raw = sandbox_read(backend, handle, "turns/1.json")
                try:
                    turn1_doc = json.loads(turn1_doc_raw or "{}")
                except json.JSONDecodeError:
                    turn1_doc = {}
                check(
                    "disk.turn1_doc",
                    turn1_doc.get("status") == "success"
                    and turn1_doc.get("native_session_id") == session_native,
                    f"status={turn1_doc.get('status')}",
                )
                rc, modes = sandbox_exec(
                    backend, handle, ["stat", "-c", "%a", f"/work/{lane.cred_path}"]
                )
                check(
                    "disk.cred_mode_600",
                    rc == 0 and modes.strip() == "600",
                    f"rc={rc} mode={modes.strip()}",
                )
                marker_body = sandbox_read(backend, handle, marker_file) or ""
                check(
                    "disk.marker_file",
                    marker in marker_body,
                    f"len={len(marker_body)}",
                )
                for rel in ("events.jsonl", "events.raw.jsonl", "session.json", "turns/1.json"):
                    scan_leaks(f"disk:{rel}", sandbox_read(backend, handle, rel), secrets)

        # --- run 2: follow-up resume --------------------------------------
        t2 = (
            "Without reading any files or running any commands, what exact phrase did I "
            "ask you to remember earlier in this conversation? Reply with exactly that "
            "phrase and nothing else."
        )
        t0 = time.monotonic()
        http, body = client.post(f"/v1/agents/{agent_id}/runs", body={"prompt": {"text": t2}})
        run2 = body if isinstance(body, dict) and body.get("id") else (body.get("run") or {})
        run2_id = str(run2.get("id") or "run-2")
        if not check(
            "run2.accepted", http in (200, 201, 202), f"http={http} code={err_code(body)}"
        ):
            return "FAIL"
        log(f"run-2 dispatched ({run2_id}); waiting for terminal state")
        final2 = wait_run(client, agent_id, run2_id, args.turn_timeout)
        turn2_s = round(time.monotonic() - t0, 1)
        if not check(
            "run2.finished",
            final2.get("status") == "FINISHED",
            f"status={final2.get('status')} error={final2.get('error')} wall={turn2_s}s",
        ):
            return "FAIL"
        text2 = str((final2.get("result") or {}).get("text") or "")
        check(
            "run2.recalls_marker",
            marker in text2,
            "model recalled the phrase" if marker in text2 else f"reply={text2[:120]!r}",
        )
        sse2 = client.stream_run(agent_id, run2_id, until=30.0)
        events2 = parse_sse(sse2)
        started2 = [e for e in events2 if e.get("type") == "thread.started"]
        check(
            "run2.resume_same_native",
            bool(started2) and started2[0].get("thread_id") == native_id,
            f"thread_id={started2[0].get('thread_id') if started2 else None}",
        )
        finished2 = [e for e in events2 if e.get("type") == "sbx.turn_finished"]
        check(
            "run2.finished_event",
            bool(finished2) and finished2[-1].get("status") == "success",
        )
        usage2 = final2.get("usage")
        check(
            "run2.usage_measured",
            isinstance(usage2, dict) and int(usage2.get("input_tokens") or 0) > 0,
            f"usage={usage2}",
        )
        record("run2", {"id": run2_id, "wall_s": turn2_s, "usage": usage2})
        scan_leaks("sse2", sse2, secrets)

        # --- usage honesty -------------------------------------------------
        http, usage_body = client.get(f"/v1/agents/{agent_id}/usage")
        agent_usage = (usage_body or {}).get("usage") if isinstance(usage_body, dict) else None
        cost = usage_body.get("cost_estimate_usd") if isinstance(usage_body, dict) else None
        ok_usage = isinstance(agent_usage, dict) and int(agent_usage.get("input_tokens") or 0) > 0
        if isinstance(usage1, dict) and isinstance(agent_usage, dict):
            ok_usage = ok_usage and int(agent_usage["input_tokens"]) >= int(
                usage1.get("input_tokens") or 0
            )
        check(
            "usage.honest_totals",
            http == 200 and ok_usage,
            f"http={http} usage={agent_usage} cost={cost}",
        )
        record("usage", usage_body if isinstance(usage_body, dict) else None)

        # --- cancel path ----------------------------------------------------
        t3 = (
            "Run the shell command `sleep 240` and wait for it to finish, then "
            f"reply with exactly this line: {provider.upper()}_TURN3_DONE"
        )
        http, body = client.post(f"/v1/agents/{agent_id}/runs", body={"prompt": {"text": t3}})
        run3 = body if isinstance(body, dict) and body.get("id") else (body.get("run") or {})
        run3_id = str(run3.get("id") or "run-3")
        if http not in (200, 201, 202):
            check("cancel.dispatched", False, f"http={http} code={err_code(body)}")
        else:
            time.sleep(2.0)
            http_c, cancel_body = client.post(f"/v1/agents/{agent_id}/runs/{run3_id}/cancel")
            check("cancel.accepted", http_c == 200, f"http={http_c}")
            final3 = wait_run(client, agent_id, run3_id, args.cancel_timeout)
            status3 = final3.get("status")
            if status3 == "CANCELLED":
                check("cancel.terminal", True, "run-3 CANCELLED")
            elif status3 in TERMINAL_RUN_STATUSES:
                note(
                    "cancel.terminal",
                    f"run-3 reached {status3} before cancel landed (already-terminal)",
                )
            else:
                check("cancel.terminal", False, f"status={status3}")
            record("run3", {"id": run3_id, "status": status3})
            agent_rec = wait_agent_status(client, agent_id, {"idle", "running"}, 60.0)
            check(
                "cancel.agent_recovered",
                agent_rec.get("status") == "idle",
                f"agent.status={agent_rec.get('status')}",
            )

        return "PASS"
    finally:
        # --- per-agent cleanup + stale path -------------------------------
        if created_agent:
            # Stale follow-up on a closed agent must be refused.
            http, _body = client.get(f"/v1/agents/{created_agent}")
            pre_status = (_body or {}).get("status") if isinstance(_body, dict) else None
            http, del_body = client.delete(f"/v1/agents/{created_agent}")
            closed = (del_body or {}).get("status") if isinstance(del_body, dict) else None
            check(
                "cleanup.closed",
                http == 200 and closed in CLOSED_AGENT_STATUSES,
                f"http={http} status={closed} (pre={pre_status})",
            )
            http, body = client.post(
                f"/v1/agents/{created_agent}/runs",
                body={"prompt": {"text": "ping"}},
            )
            check(
                "stale.followup_refused",
                http in (404, 409),
                f"http={http} code={err_code(body)}",
            )
            # The durable ledger keeps the terminal truth readable.
            http, run_body = client.get(f"/v1/agents/{created_agent}/runs/run-1")
            run1_status = run_body.get("status") if isinstance(run_body, dict) else None
            check(
                "stale.run_history",
                http == 200 and run1_status in TERMINAL_RUN_STATUSES,
                f"http={http} status={run1_status}",
            )
            if backend is not None:
                deadline = time.monotonic() + 90.0
                leftover = sandbox_handles(backend, created_agent)
                while leftover and time.monotonic() < deadline:
                    time.sleep(5.0)
                    leftover = sandbox_handles(backend, created_agent)
                check("cleanup.no_sandbox_left", not leftover, f"leftover={len(leftover)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, choices=sorted(TARGETS))
    parser.add_argument("--base-url", default=os.environ.get("SBX_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--app", default=os.environ.get("SBX_GATE_APP", DEFAULT_APP))
    parser.add_argument("--model", default=None, help="override the lane's default model")
    parser.add_argument("--turn-timeout", type=float, default=TURN_TIMEOUT_S)
    parser.add_argument("--cancel-timeout", type=float, default=CANCEL_TIMEOUT_S)
    args = parser.parse_args()

    account_id = args.account
    lane = TARGETS[account_id]
    if args.model:
        lane = Lane(lane.provider, args.model, lane.cred_path)

    token = os.environ.get("SBX_V1_API_KEY") or os.environ.get("SBX_API_KEY")
    key_file = os.environ.get("SBX_KEY_FILE")
    if not token and key_file and Path(key_file).is_file():
        token = Path(key_file).read_text(encoding="utf-8").strip()
    if not token:
        log("SKIP: no bearer key (SBX_V1_API_KEY / SBX_API_KEY / SBX_KEY_FILE)")
        return 2

    secrets = watch_values()
    client = V1(args.base_url, token)
    artifact_name = f"v1_gate_{account_id}.json"
    verdict = "FAIL"
    try:
        http, me = client.get("/v1/me")
        if not check("auth.me", http == 200 and isinstance(me, dict), f"http={http}"):
            verdict = "SKIP"
            return 2
        record("me", {"key_id": me.get("key_id"), "scopes": me.get("scopes")})
        check(
            "auth.scopes",
            "agents" in set(me.get("scopes") or []),
            f"scopes={me.get('scopes')}",
        )
        scan_leaks("me", json.dumps(me), secrets)

        http, acct_body = client.get(f"/v1/accounts/{account_id}")
        account = acct_body if isinstance(acct_body, dict) else {}
        if not check("account.readable", http == 200, f"http={http} code={err_code(acct_body)}"):
            verdict = "SKIP"
            return 2
        record(
            "account",
            {
                "id": account_id,
                "provider": account.get("provider"),
                "status": account.get("status"),
                "models": account.get("models"),
            },
        )
        scan_leaks("account", json.dumps(acct_body), secrets)
        if account.get("provider") != lane.provider:
            check("account.provider_match", False, f"provider={account.get('provider')}")
            return 1

        backend = modal_backend(args.app)
        if account.get("status") != "active":
            verdict = gate_invalid_lane(client, lane, account_id, account, secrets, args, backend)
        else:
            verdict = gate_active_lane(client, backend, lane, account_id, secrets, args)

        # Control-plane log tail: the app must never log secrets.
        logs = app_logs(args.app)
        scan_leaks("app_logs", logs, secrets)
        record("app_log_bytes", len(logs))
    finally:
        client.close()
        failed = [c for c in CHECKS if c["ok"] is False]
        if failed and verdict != "SKIP":
            verdict = "FAIL"
        payload = {
            "account": account_id,
            "provider": lane.provider,
            "base_url": args.base_url,
            "app": args.app,
            "verdict": verdict,
            "checks": CHECKS,
            "results": RESULTS,
            "failed": [c["name"] for c in failed],
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        path = write_json(artifact_name, payload)
        log(f"evidence -> {path}")
    log(f"verdict={verdict} checks={len(CHECKS)} failed={len(failed)}")
    if verdict == "PASS":
        return 0
    if verdict == "SKIP":
        return 2
    if verdict == "CREDENTIAL_DEFERRED":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
