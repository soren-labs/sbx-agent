"""P0 spike (Linear SOR-28): Codex multi-turn session loop inside a blank Modal Sandbox.

Verifies, with real Modal + the orchestrator's local ~/.codex/auth.json:
  1. slim image cold start (Sandbox.create -> `codex --version`)
  2. auth.json injection works; two Sandboxes sharing one auth.json concurrently
  3. multi-turn via `codex exec resume <thread_id>` across separate sb.exec calls
  4. idle_timeout reclaims an idle Sandbox
  5. `tail -F events.jsonl` streaming stays alive through a long silent period
  6. codex exec behaviour when stdin is an open pipe (runner must close stdin)

Only verifies; builds nothing. Every Sandbox is terminated before exit.
Run:  python spike.py --model gpt-5.6-luna --tail-minutes 10
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import modal

CODEX_VERSION = "0.153.0"
APP_NAME = "sbx-spike"
WORK = "/work"
CODEX_HOME = f"{WORK}/.codex"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("curl", "git", "ca-certificates", "ripgrep", "jq", "procps")
    .run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
        "apt-get install -y nodejs",
        f"npm i -g @openai/codex@{CODEX_VERSION}",
        "codex --version",
    )
)

RESULTS: dict[str, object] = {}
LOCK = threading.Lock()


def record(key: str, value: object) -> None:
    with LOCK:
        RESULTS[key] = value
    print(f"[result] {key} = {json.dumps(value, ensure_ascii=False, default=str)}", flush=True)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_auth() -> dict:
    p = Path.home() / ".codex" / "auth.json"
    d = json.loads(p.read_text())
    assert d.get("auth_mode") == "chatgpt", d.get("auth_mode")
    return d


def sh(sb: modal.Sandbox, cmd: str, timeout: int | None = 120) -> tuple[int, str, str]:
    p = sb.exec("bash", "-c", cmd, timeout=timeout)
    out = p.stdout.read()
    err = p.stderr.read()
    rc = p.wait()
    return rc, out, err


def new_sandbox(app: modal.App, auth: dict, *, name: str, idle_timeout: int | None = None,
                timeout: int = 1800) -> tuple[modal.Sandbox, float]:
    t0 = time.perf_counter()
    sb = modal.Sandbox.create(
        "sleep", "infinity",
        app=app,
        image=image,
        secrets=[modal.Secret.from_dict({"CODEX_AUTH_JSON": json.dumps(auth)})],
        env={"CODEX_HOME": CODEX_HOME, "SBX_WORK": WORK},
        cpu=(1, 2),
        memory=(1024, 4096),
        timeout=timeout,
        idle_timeout=idle_timeout,
        workdir=WORK,
        tags={"spike": "sor-28", "name": name},
    )
    t_create = time.perf_counter() - t0
    return sb, t_create


def bootstrap_codex(sb: modal.Sandbox, model: str) -> None:
    config = f'''model = "{model}"
approval_policy = "never"
sandbox_mode = "danger-full-access"
[shell_environment_policy]
exclude = ["CODEX_AUTH_JSON"]
'''
    cmd = (
        f"mkdir -p {CODEX_HOME} {WORK}/inbox {WORK}/turns && "
        f"printf %s \"$CODEX_AUTH_JSON\" > {CODEX_HOME}/auth.json && chmod 600 {CODEX_HOME}/auth.json && "
        f"cat > {CODEX_HOME}/config.toml <<'EOF'\n{config}EOF\n"
        f"cat > {WORK}/AGENTS.md <<'EOF'\n"
        "Work only inside /work. Never ask for confirmation. Keep answers short.\n"
        "EOF\n"
    )
    rc, out, err = sh(sb, cmd)
    assert rc == 0, (rc, out, err)


def codex_turn(sb: modal.Sandbox, prompt: str, *, thread_id: str | None, turn_n: int,
               close_stdin: bool = True, timeout: int = 900) -> dict:
    """Run one codex turn inside the Sandbox, streaming JSONL to /work/events.jsonl and back to us."""
    if thread_id is None:
        argv = f"codex exec --json --skip-git-repo-check -C {WORK} --dangerously-bypass-approvals-and-sandbox {shlex.quote(prompt)}"
    else:
        argv = f"codex exec resume --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox {thread_id} {shlex.quote(prompt)}"
    redirect = " </dev/null" if close_stdin else ""
    cmd = f"set -o pipefail; cd {WORK} && {argv}{redirect} 2>{WORK}/turns/{turn_n}.stderr | tee -a {WORK}/events.jsonl"
    t0 = time.perf_counter()
    # bufsize=1 => Modal splits the stream on newlines; default (-1) yields raw chunks that can
    # contain several JSON lines glued together (observed in run 1: 2 events lost as 1 "bad line").
    p = sb.exec("bash", "-c", cmd, timeout=timeout, bufsize=1)
    events: list[dict] = []
    bad_lines = 0
    bad_samples: list[str] = []
    first_event_at = None
    for line in p.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        if first_event_at is None:
            first_event_at = time.perf_counter() - t0
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            bad_lines += 1
            bad_samples.append(line[:200])
            continue
        events.append(ev)
        t = ev.get("type")
        if t == "item.completed":
            it = ev.get("item", {})
            log(f"  turn{turn_n} item {it.get('type')}: {str(it.get('text') or it.get('command') or '')[:120]}")
        elif t in ("thread.started", "turn.completed", "error", "turn.failed"):
            log(f"  turn{turn_n} {t}: {json.dumps({k: v for k, v in ev.items() if k != 'type'}, ensure_ascii=False)[:300]}")
    rc = p.wait()
    dur = time.perf_counter() - t0
    tid = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), thread_id)
    usage = next((e.get("usage") for e in events if e.get("type") == "turn.completed"), None)
    last_msg = next((e["item"].get("text") for e in reversed(events)
                     if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"), None)
    return {
        "rc": rc, "duration_s": round(dur, 1), "first_event_s": round(first_event_at or -1, 1),
        "thread_id": tid, "usage": usage, "n_events": len(events), "bad_lines": bad_lines, "bad_samples": bad_samples,
        "event_types": sorted({e.get("type") for e in events}),
        "item_types": sorted({e.get("item", {}).get("type") for e in events if e.get("type", "").startswith("item.")}),
        "last_message": (last_msg or "")[:300],
        "events": events,
    }


# ---------------------------------------------------------------- tests


def test_cold_start_and_multiturn(app: modal.App, auth: dict, model: str) -> modal.Sandbox:
    log("T1 cold start")
    sb, t_create = new_sandbox(app, auth, name="main")
    t0 = time.perf_counter()
    rc, out, err = sh(sb, "codex --version")
    t_ready = time.perf_counter() - t0
    record("1.cold_start", {"sandbox_id": sb.object_id, "create_s": round(t_create, 2),
                            "codex_version_exec_s": round(t_ready, 2), "total_s": round(t_create + t_ready, 2),
                            "codex_version": out.strip(), "rc": rc})
    rc, out, _ = sh(sb, "node --version; id -un; nproc; free -m | awk 'NR==2{print $2\" MB\"}'; cat /etc/os-release | head -2")
    record("1.env", out.strip().splitlines())

    bootstrap_codex(sb, model)

    log("T2 auth + turn 1 (create file)")
    r1 = codex_turn(sb, "Create a file /work/notes.md containing exactly three lines: 'alpha', 'beta', 'gamma'. "
                        "Use a shell command. Then reply with the single word DONE.", thread_id=None, turn_n=1)
    record("2.turn1", {k: v for k, v in r1.items() if k != "events"})
    rc, out, _ = sh(sb, f"cat {WORK}/notes.md; echo ---; cat {WORK}/turns/1.stderr | head -20")
    record("2.turn1_fs", out)
    if r1["rc"] != 0 or not r1["thread_id"]:
        record("2.auth_verdict", "FAIL: turn 1 did not complete")
        return sb
    record("2.auth_verdict", "PASS: subscription auth.json works inside Sandbox")

    log("T3 turn 2 via exec resume (modify file, must remember context)")
    r2 = codex_turn(sb, "Append a fourth line 'delta' to the file you just created (do not mention its name, you know it). "
                        "Then reply with the number of lines it now has.", thread_id=r1["thread_id"], turn_n=2)
    record("3.turn2_resume", {k: v for k, v in r2.items() if k != "events"})
    rc, out, _ = sh(sb, f"cat {WORK}/notes.md; echo ---; wc -l < {WORK}/notes.md; echo ---; ls {CODEX_HOME}/sessions 2>/dev/null | head; "
                        f"find {CODEX_HOME} -name '*.jsonl' | head -3")
    record("3.turn2_fs", out)
    ok = r2["rc"] == 0 and r2["thread_id"] == r1["thread_id"] and "delta" in out
    record("3.resume_verdict", "PASS: same thread_id, context + filesystem carried over" if ok else "FAIL")

    log("T3b turn 3 resume again (sanity)")
    r3 = codex_turn(sb, "What are the four lines in that file? Answer with them comma-separated only.",
                    thread_id=r1["thread_id"], turn_n=3)
    record("3.turn3_resume", {k: v for k, v in r3.items() if k != "events"})

    # Save redacted real events for fixtures
    Path("/tmp/sbx/p0/out").mkdir(parents=True, exist_ok=True)
    with open("/tmp/sbx/p0/out/real_events.jsonl", "w") as f:
        for r in (r1, r2, r3):
            for e in r["events"]:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    rc, out, _ = sh(sb, f"python3 -c \"import json;d=json.load(open('{CODEX_HOME}/auth.json'));print(d.get('last_refresh'))\"")
    record("2.auth_last_refresh_after_turns", {"injected": auth.get("last_refresh"), "in_sandbox": out.strip()})
    return sb


def test_stdin_hang(sb: modal.Sandbox) -> None:
    log("T6 stdin open-pipe behaviour")
    cmd = f"cd {WORK} && timeout 25 codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox 'Reply with the word PING only.'; echo RC=$?"
    t0 = time.perf_counter()
    p = sb.exec("bash", "-c", cmd, timeout=60)
    out = p.stdout.read()
    p.wait()
    dur = time.perf_counter() - t0
    hung = "RC=124" in out
    record("6.stdin_open_pipe", {"duration_s": round(dur, 1), "timed_out_after_25s": hung,
                                 "verdict": "codex waits for stdin EOF when stdin is a pipe -> runner MUST redirect </dev/null or write_eof"
                                 if hung else "codex did not block on open stdin",
                                 "tail": out[-300:]})


def test_tail_stream(sb: modal.Sandbox, minutes: float) -> None:
    log(f"T5 tail -F streaming for {minutes} min with a silent gap")
    total = minutes * 60
    events_seen: list[float] = []
    t0 = time.perf_counter()
    tail = sb.exec("bash", "-c", f"tail -n 0 -F {WORK}/events.jsonl", timeout=int(total) + 120, bufsize=1)

    def reader():
        try:
            for line in tail.stdout:
                events_seen.append(round(time.perf_counter() - t0, 1))
        except Exception as e:  # noqa: BLE001
            events_seen.append(-1.0)
            log(f"  tail reader ended: {e!r}")

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    # write a marker at start, stay silent for the whole window, then write again
    sh(sb, f"echo '{{\"type\":\"sbx.probe\",\"n\":1}}' >> {WORK}/events.jsonl")
    time.sleep(total)
    sh(sb, f"echo '{{\"type\":\"sbx.probe\",\"n\":2}}' >> {WORK}/events.jsonl")
    time.sleep(5)
    alive = tail.poll() is None
    record("5.tail_stream", {"window_s": total, "lines_received_at_s": events_seen, "tail_still_running": alive,
                             "verdict": "PASS: line after silent window arrived" if len(events_seen) >= 2 and events_seen[-1] > total - 5
                             else "FAIL/INCONCLUSIVE"})
    # cleanup: kill tail
    sh(sb, "pkill -f 'tail -n 0 -F' || true")


def test_concurrency(app: modal.App, auth: dict, model: str) -> None:
    log("T2b two Sandboxes sharing the same auth.json concurrently")

    def one(i: int) -> dict:
        sb = None
        try:
            sb, t_create = new_sandbox(app, auth, name=f"conc{i}")
            bootstrap_codex(sb, model)
            r = codex_turn(sb, f"Write the text 'sandbox {i}' to /work/id.txt with a shell command, then reply OK.",
                           thread_id=None, turn_n=1)
            _, out, _ = sh(sb, f"cat {WORK}/id.txt; python3 -c \"import json;d=json.load(open('{CODEX_HOME}/auth.json'));print(d.get('last_refresh'))\"")
            return {"sandbox_id": sb.object_id, "create_s": round(t_create, 2), "rc": r["rc"], "duration_s": r["duration_s"],
                    "usage": r["usage"], "fs": out.strip().splitlines(), "err": None}
        except Exception as e:  # noqa: BLE001
            return {"err": repr(e), "tb": traceback.format_exc()[-800:]}
        finally:
            if sb is not None:
                sb.terminate()

    with ThreadPoolExecutor(2) as ex:
        results = list(ex.map(one, [1, 2]))
    ok = all(r.get("rc") == 0 for r in results)
    record("2b.concurrency_2", {"results": results, "verdict": "PASS: both turns succeeded with shared auth.json" if ok else "FAIL"})


def test_idle_timeout(app: modal.App, auth: dict, idle_s: int) -> None:
    log(f"T4 idle_timeout={idle_s}s reclaim")
    sb, _ = new_sandbox(app, auth, name="idle", idle_timeout=idle_s, timeout=idle_s * 4)
    sh(sb, "echo hi")
    t0 = time.perf_counter()
    rc = None
    while time.perf_counter() - t0 < idle_s * 3:
        rc = sb.poll()
        if rc is not None:
            break
        time.sleep(5)
    dur = time.perf_counter() - t0
    record("4.idle_timeout", {"idle_timeout_s": idle_s, "reclaimed_after_s": round(dur, 1), "returncode": rc,
                              "verdict": "PASS: Sandbox ended by itself" if rc is not None else "FAIL: still running"})
    if rc is None:
        sb.terminate()


def list_leftovers(app: modal.App) -> list[str]:
    return [s.object_id for s in modal.Sandbox.list(app_id=app.app_id, tags={"spike": "sor-28"})]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-5.6-luna")
    ap.add_argument("--tail-minutes", type=float, default=10)
    ap.add_argument("--idle-seconds", type=int, default=90)
    ap.add_argument("--skip", default="", help="comma list: concurrency,idle,tail,stdin")
    args = ap.parse_args()
    skip = set(filter(None, args.skip.split(",")))

    auth = load_auth()
    record("0.params", {"model": args.model, "codex": CODEX_VERSION, "modal": modal.__version__,
                        "auth_mode": auth["auth_mode"], "auth_last_refresh": auth.get("last_refresh")})

    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    t0 = time.perf_counter()
    log("building/checking image")
    image.build(app)
    record("0.image_build_or_check_s", round(time.perf_counter() - t0, 1))

    main_sb: modal.Sandbox | None = None
    threads: list[threading.Thread] = []
    try:
        main_sb = test_cold_start_and_multiturn(app, auth, args.model)
        if "tail" not in skip:
            th = threading.Thread(target=test_tail_stream, args=(main_sb, args.tail_minutes), daemon=True)
            th.start()
            threads.append(th)
        if "stdin" not in skip:
            test_stdin_hang(main_sb)
        if "concurrency" not in skip:
            test_concurrency(app, auth, args.model)
        if "idle" not in skip:
            test_idle_timeout(app, auth, args.idle_seconds)
        for th in threads:
            th.join()
    except Exception:  # noqa: BLE001
        record("error", traceback.format_exc()[-2000:])
    finally:
        if main_sb is not None:
            try:
                rc, out, _ = sh(main_sb, f"cat {WORK}/events.jsonl | wc -l; du -sh {CODEX_HOME} | cut -f1")
                record("9.main_sandbox_summary", out.strip().splitlines())
            except Exception:  # noqa: BLE001
                pass
            main_sb.terminate()
        time.sleep(3)
        left = list_leftovers(app)
        for sid in left:
            modal.Sandbox.from_id(sid).terminate()
        record("9.leftovers_terminated", left)
        Path("/tmp/sbx/p0/out").mkdir(parents=True, exist_ok=True)
        Path("/tmp/sbx/p0/out/results.json").write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False, default=str))
        log("results written to /tmp/sbx/p0/out/results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
