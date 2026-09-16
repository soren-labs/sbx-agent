"""SOR-107 lane 4: real cross-provider workflow gate against the RC deployment.

Host-executed against the deployed ``/v1`` control plane (Bearer ``sbx_*``),
exercising the SOR-83/SOR-84 seams end to end on REAL providers/accounts:

    phase1  preflight -> worker A (producer) real task on a declared workspace
            -> durable artifact snapshot -> artifact download -> producer
            sandbox teardown -> artifact still downloadable -> worker B
            (consumer via artifact handoff, no source copied into the prompt)
            -> deliberate local orchestration-client kill mid-stream
            (``os._exit(42)`` — no cleanup handlers, no persisted handles)
    phase2  fresh process holding ONLY api key + workflow_id ->
            ``recover``/``GET /v1/workflows/{id}`` -> ``resume``/SSE
            Last-Event-ID + persisted-terminal fallback -> exact-head
            reviewer/consumer (``head_sha`` handoff + ``workspace/review``
            pin) -> cancel leg -> error leg -> ``wait_many`` mixed terminal
            truth -> ``sbx upgrade`` control-plane redeploy -> durability
            re-checks -> credential-leak scan -> scoped ``DELETE
            /v1/workflows/{id}`` cleanup -> verdict.

Prompt hygiene: consumer/reviewer prompts reference paths only — the marker
file's contents never appear in a prompt (asserted, recorded as evidence).

Secret hygiene: nothing secret is ever printed or persisted. Evidence JSON
carries ids, statuses, event type/id sequences, sha256 digests and timings —
never raw model text or payload bodies. The leak corpus (every API payload,
SSE data frame and artifact member seen) is scanned in memory against the
API key, the basic-auth password, the Modal token pair and the standard
JWT/sk- patterns; only the verdict is recorded.

Usage (isolated RC HOME — secrets never enter argv):

    env -i HOME=<rc-home> PATH=... \
        GATE_REPO=https://github.com/<org>/<fixture>.git \
        GATE_BASE_REF=main GATE_BASE_SHA=<40-hex> \
        uv run python -m tests.e2e_modal.workflow_gate phase1
    uv run python -m tests.e2e_modal.workflow_gate phase2 --workflow-id <wf>

``SBX_API_KEY`` is read from ``$SBX_API_KEY`` or
``$HOME/.local/state/sbx/bootstrap.key``; ``SBX_BASE_URL`` from the env or
``$HOME/.config/sbx/config.toml``. Exit codes: 0 = PASS, 1 = FAIL,
2 = prerequisites missing (SKIP). Phase 1 ends in a deliberate kill — its
process exit code is 42 *by design*.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from examples.sbx_client import SbxApiError, SbxClient, SseEvent  # noqa: E402

from tests.e2e_modal.helpers import artifacts_dir, leak_reason  # noqa: E402

KILL_EXIT = 42
TURN_WAIT_S = 600.0
PROVISION_WAIT_S = 300.0

RESULTS: dict[str, Any] = {}
CHECKS: list[dict[str, Any]] = []
LEAK_CORPUS: list[str] = []  # scanned in memory, never persisted
PHASE = 0  # set by phase1()/phase2() so checks carry their phase


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def record(key: str, value: object) -> None:
    RESULTS[key] = value
    log(f"[result] {key} = {json.dumps(value, ensure_ascii=False, default=str)}")


def check(name: str, ok: bool, detail: str | None = None) -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail, "phase": PHASE})
    mark = "ok" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def corpus(payload: Any) -> None:
    """Add an API/SSE/artifact payload to the leak-scan corpus (memory only)."""
    if isinstance(payload, bytes):
        LEAK_CORPUS.append(payload.decode("utf-8", "replace"))
    else:
        try:
            LEAK_CORPUS.append(json.dumps(payload, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            LEAK_CORPUS.append(str(payload))


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _watch_values() -> list[str]:
    """Secret literals that must never appear in captured output.

    Loaded from the isolated HOME only; values never printed or persisted.
    """
    values: list[str] = []
    home = Path(os.environ.get("HOME", str(Path.home())))
    key_file = home / ".local/state/sbx/bootstrap.key"
    try:
        token = key_file.read_text(encoding="utf-8").strip()
        if len(token) >= 8:
            values.append(token)
    except OSError:
        pass
    try:
        basic = json.loads((home / ".local/state/sbx/basic-auth.json").read_text())
        for field in ("user", "password"):
            value = str(basic.get(field) or "")
            if len(value) >= 8:
                values.append(value)
    except (OSError, json.JSONDecodeError):
        pass
    try:
        import tomllib

        modal_doc = tomllib.loads((home / ".modal.toml").read_text())
        for profile in modal_doc.values():
            if isinstance(profile, dict):
                for field in ("token_id", "token_secret"):
                    value = str(profile.get(field) or "")
                    if len(value) >= 8:
                        values.append(value)
    except Exception:
        pass
    return values


def leak_scan() -> bool:
    secrets = _watch_values()
    joined = "\n".join(LEAK_CORPUS)
    corpus_bytes = len(joined.encode("utf-8"))
    for i, secret in enumerate(secrets):
        if secret and secret in joined:
            return check("leak.scan", False, f"secret value #{i} present in captured output")
    why = leak_reason(joined)
    ok = why is None
    return check(
        "leak.scan",
        ok,
        f"corpus={corpus_bytes}B clean" if ok else f"pattern hit: {why}",
    )


def resolve_base_url() -> str:
    url = os.environ.get("SBX_BASE_URL")
    if url:
        return url.rstrip("/")
    home = Path(os.environ.get("HOME", str(Path.home())))
    import tomllib

    cfg = tomllib.loads((home / ".config/sbx/config.toml").read_text())
    return str(cfg["api"]["base_url"]).rstrip("/")


def resolve_api_key() -> str:
    key = os.environ.get("SBX_API_KEY")
    if key:
        return key.strip()
    home = Path(os.environ.get("HOME", str(Path.home())))
    return (home / ".local/state/sbx/bootstrap.key").read_text(encoding="utf-8").strip()


def evidence_path(workflow_id: str) -> Path:
    return artifacts_dir() / f"workflow_gate_{workflow_id}.json"


def write_evidence(workflow_id: str, verdict: str) -> Path:
    path = evidence_path(workflow_id)
    payload = {
        "gate": "SOR-107 lane4 cross-provider workflow",
        "workflow_id": workflow_id,
        "verdict": verdict,
        "results": RESULTS,
        "checks": CHECKS,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_evidence(workflow_id: str) -> dict[str, Any]:
    try:
        return json.loads(evidence_path(workflow_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def meta(workflow_id: str, task_id: str, role: str) -> dict[str, str]:
    return {"workflow_id": workflow_id, "task_id": task_id, "role": role}


def workspace_decl() -> dict[str, str]:
    return {
        "repo": os.environ["GATE_REPO"],
        "base_ref": os.environ.get("GATE_BASE_REF", "main"),
        "base_sha": os.environ["GATE_BASE_SHA"],
    }


def wait_status(
    client: SbxClient, agent_id: str, run_id: str, want: set[str], timeout: float
) -> dict:
    deadline = time.monotonic() + timeout
    run = client.get_run(agent_id, run_id)
    corpus(run)
    while run.get("status") not in want and time.monotonic() < deadline:
        time.sleep(3.0)
        run = client.get_run(agent_id, run_id)
        corpus(run)
    return run


def create_with_retry(
    client: SbxClient, *args: Any, attempts: int = 40, **kwargs: Any
) -> dict[str, Any]:
    """Create an agent, retrying while the provider account pool is busy.

    Single-account providers, the small per-key concurrency cap and parallel
    gate lanes mean a create can hit ``account_busy``/``account_unavailable``
    (409) or ``provider_exhausted``/``concurrency_limit`` (429) transiently —
    never a product failure. Budget ≈ attempts × 20 s.
    """
    last: SbxApiError | None = None
    for attempt in range(attempts):
        try:
            return client.create_agent(*args, **kwargs)
        except SbxApiError as exc:
            corpus({"code": exc.code, "status": exc.status})
            if exc.status not in (409, 429):
                raise
            last = exc
            log(f"  create attempt {attempt + 1}/{attempts}: {exc.status} {exc.code}; retrying")
            time.sleep(20.0)
    assert last is not None
    raise last


def collect_watch(
    client: SbxClient,
    agent_id: str,
    run_id: str,
    *,
    max_events: int | None = None,
    deadline_s: float = TURN_WAIT_S,
) -> dict[str, Any]:
    """Watch a run to its SSE end; returns event type/id sequence (data → corpus).

    Bounded by ``deadline_s`` — a wedged stream must not hang the gate; the
    persisted-terminal fallback (``wait``/``get_run``) decides truth anyway.
    """
    types: list[str] = []
    ids: list[str] = []
    deadline = time.monotonic() + deadline_s
    for ev in client.watch(agent_id, run_id, status_poll_s=15.0, read_timeout_s=30.0):
        if isinstance(ev, SseEvent):
            types.append(ev.type)
            if ev.id is not None:
                ids.append(ev.id)
            corpus(ev.data)
            if max_events is not None and len(ids) >= max_events:
                break
        if time.monotonic() > deadline:
            break
    return {"types": types, "ids": ids}


def push_head_to_fixture(bundle: bytes | None, head_sha: str, branch: str) -> bool:
    """Publish ``head_sha`` on a side branch of the fixture repo.

    Preferred path: fetch the artifact's ``repo.bundle`` into a temp clone —
    the exact producer commit lands on the shared remote without any source
    text passing through this client. Fallback: commit a tiny host-side file
    on top of base so the exact-head leg still has a reachable commit.
    """
    repo = os.environ["GATE_REPO"]
    base_sha = os.environ["GATE_BASE_SHA"]
    with tempfile.TemporaryDirectory(prefix="sbx-gate-push-") as td:
        # The gate runs under an isolated HOME, so the global gitconfig is
        # invisible; pass the github.com credential helper explicitly. The
        # helper reads gh's keyring entry — no token touches argv or files.
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"

        def git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "credential.https://github.com.helper=!gh auth git-credential",
                    *args,
                ],
                cwd=cwd or td,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )

        if git("clone", "--quiet", repo, "wc").returncode != 0:
            return False
        wc = str(Path(td) / "wc")
        target = head_sha
        if bundle:
            bundle_path = str(Path(td) / "a.bundle")
            Path(bundle_path).write_bytes(bundle)
            heads = git("bundle", "list-heads", bundle_path, cwd=wc)
            ref = None
            for line in heads.stdout.splitlines():
                sha, _, name = line.partition(" ")
                if sha.strip() == head_sha:
                    ref = name.strip()
            if ref is None:
                return False
            if git("fetch", "--quiet", bundle_path, ref, cwd=wc).returncode != 0:
                return False
            if git("rev-parse", "--verify", f"{head_sha}^{{commit}}", cwd=wc).returncode != 0:
                return False
        else:
            if git("checkout", "--quiet", base_sha, cwd=wc).returncode != 0:
                return False
            Path(wc, "gate_upstream.txt").write_text(f"upstream head for {branch}\n")
            if git("add", "gate_upstream.txt", cwd=wc).returncode != 0:
                return False
            commit = git(
                "-c",
                "user.name=sbx-gate",
                "-c",
                "user.email=sbx-gate@localhost",
                "commit",
                "-qm",
                f"gate: upstream head for {branch}",
                cwd=wc,
            )
            if commit.returncode != 0:
                return False
            resolved = git("rev-parse", "HEAD", cwd=wc)
            target = resolved.stdout.strip()
        if not git("merge-base", "--is-ancestor", base_sha, target, cwd=wc).returncode == 0:
            return False
        push = git("push", "origin", f"{target}:refs/heads/{branch}", cwd=wc)
        if push.returncode != 0:
            log(f"  push failed: {push.stderr.strip()[-200:]}")
            return False
        record("exact_head.pushed_sha", target)
        record("exact_head.branch", branch)
        record("exact_head.from_artifact_bundle", bool(bundle))
        return True


# ----------------------------------------------------------------- phase 1


def phase1(args: argparse.Namespace) -> int:
    global PHASE
    PHASE = 1
    base_sha = os.environ["GATE_BASE_SHA"]
    workflow_id = args.workflow_id or f"wf-gate-{uuid.uuid4().hex[:12]}"
    record("workflow_id", workflow_id)
    record("gate_repo", os.environ["GATE_REPO"])
    record("base", {"ref": os.environ.get("GATE_BASE_REF", "main"), "sha": base_sha})
    marker = f"wf_id={workflow_id}"

    client = SbxClient(base_url=resolve_base_url(), api_key=resolve_api_key())
    try:
        _phase1_body(client, args, workflow_id, marker, base_sha)
    except SystemExit:
        raise
    except Exception as exc:
        check("phase1.exception", False, f"{exc.__class__.__name__}: {exc}"[:200])
        try:
            client.http.delete(f"/v1/workflows/{workflow_id}")
        except Exception:
            pass
        write_evidence(workflow_id, "phase1-failed")
        return 1
    # _phase1_body always ends in the deliberate os._exit; reaching here is a bug.
    write_evidence(workflow_id, "phase1-returned-without-kill")
    return 1


def _phase1_body(
    client: SbxClient, args: argparse.Namespace, workflow_id: str, marker: str, base_sha: str
) -> None:
    try:
        me = client.me()
        corpus(me)
        check(
            "preflight.me",
            me.get("key_id", "").startswith("key_") and "agents" in (me.get("scopes") or []),
            f"key={me.get('key_id')} scopes={me.get('scopes')}",
        )
        models = client.models()
        corpus(models)
        available = sorted({m["provider"] for m in models if m.get("accounts_available", 0) > 0})
        check("preflight.providers>=3", len(available) >= 3, f"available={available}")

        # ---------------- worker A: producer on a declared workspace -------
        prompt_a = (
            "Work inside the repo/ directory (a git checkout). "
            f"Create a file named gate_marker.txt whose first line is exactly '{marker}' "
            "and whose second line is 'producer=grok'. "
            "Then run: git add gate_marker.txt && "
            "git -c user.name=gate -c user.email=gate@localhost commit -m 'gate: add marker'. "
            "Reply with just the new commit sha."
        )
        created = create_with_retry(
            client,
            prompt_a,
            provider="grok",
            name="gate-wf-a-producer",
            metadata=meta(workflow_id, "produce-marker", "worker"),
            workspace=workspace_decl(),
        )
        corpus(created)
        agent_a, run_a = created["agent"], created["run"]
        record("A", {"agent": agent_a["id"], "run": run_a["id"], "provider": "grok"})
        # Stream route 404s while the run is still CREATING — wait for the
        # dispatch (or a fast terminal) before attaching the watcher.
        wait_status(
            client,
            agent_a["id"],
            run_a["id"],
            {"RUNNING", "FINISHED", "ERROR", "CANCELLED", "EXPIRED", "UNKNOWN"},
            PROVISION_WAIT_S,
        )
        seq_a = collect_watch(client, agent_a["id"], run_a["id"])
        record("A.sse_events", {"count": len(seq_a["ids"]), "types": seq_a["types"][:20]})
        run_a_final = client.wait(agent_a["id"], run_a["id"], timeout_s=TURN_WAIT_S)
        corpus(run_a_final)
        check(
            "A.run_finished",
            run_a_final.get("status") == "FINISHED",
            f"status={run_a_final.get('status')} err={run_a_final.get('error')}",
        )
        agent_a_doc = client.get_agent(agent_a["id"])
        corpus(agent_a_doc)
        check(
            "A.metadata_persisted",
            (agent_a_doc.get("metadata") or {}).get("workflow_id") == workflow_id
            and (agent_a_doc.get("metadata") or {}).get("role") == "worker",
            str(agent_a_doc.get("metadata")),
        )
        ws_a = client.get_workspace(agent_a["id"])
        corpus(ws_a)
        check(
            "A.workspace",
            ws_a.get("checkout_sha") == base_sha and bool(ws_a.get("head_sha")),
            f"checkout={ws_a.get('checkout_sha')} head={ws_a.get('head_sha')}",
        )

        # ---------------- durable artifact + producer teardown -------------
        artifact = client.create_artifact(
            agent_a["id"], run_id=run_a["id"], test_command="test -f gate_marker.txt"
        )
        corpus(artifact)
        art_id = artifact["artifact_id"]
        # Snapshotting re-reads workdir HEAD and persists it on the workspace
        # record — the artifact's head_sha is the producer truth.
        head_a = artifact.get("head_sha")
        record("A.artifact", {"id": art_id, "head_sha": head_a})
        check(
            "artifact.manifest",
            artifact.get("base_sha") == base_sha and head_a and head_a != base_sha,
            f"base={artifact.get('base_sha')} head={head_a}",
        )
        ws_a2 = client.get_workspace(agent_a["id"])
        corpus(ws_a2)
        check(
            "A.workspace_head_recorded",
            ws_a2.get("head_sha") == head_a,
            f"recorded head={ws_a2.get('head_sha')}",
        )
        record("A.head_sha", head_a)
        check(
            "artifact.test_recorded",
            any(t.get("exit_code") == 0 for t in artifact.get("tests") or []),
            str(artifact.get("tests")),
        )
        members: dict[str, bytes] = {}
        for member in ("manifest.json", "patch.diff", "repo.bundle"):
            if member == "repo.bundle" and member not in (artifact.get("payloads") or {}):
                continue
            data = client.download_artifact(art_id, member=member)
            corpus(data)
            members[member] = data
            if member in (artifact.get("payloads") or {}):
                check(
                    f"artifact.{member}.sha256",
                    sha256(data) == artifact["payloads"][member],
                    sha256(data)[:16],
                )
        record("artifact.payloads", sorted((artifact.get("payloads") or {}).keys()))
        record("artifact.files", sorted(f.get("path") for f in artifact.get("files") or []))

        closed_a = client.delete_agent(agent_a["id"])
        corpus(closed_a)
        check("A.teardown", closed_a.get("status") in {"closed", "closing"}, closed_a.get("status"))
        repatch = client.download_artifact(art_id, member="patch.diff")
        corpus(repatch)
        check(
            "artifact.survives_producer_teardown",
            repatch == members["patch.diff"],
            f"{len(repatch)}B re-downloaded post-teardown",
        )
        man = client.get_artifact(art_id)
        corpus(man)
        check("artifact.get_post_teardown", man.get("artifact_id") == art_id)

        # SSE fallback on a torn-down agent: watch must not hang — the
        # persisted-terminal check is the bounded fallback for dead sandboxes.
        t0 = time.monotonic()
        fallback_events = 0
        for _ev in client.watch(
            agent_a["id"], run_a["id"], status_poll_s=8.0, read_timeout_s=30.0, max_reconnects=2
        ):
            fallback_events += 1
        check(
            "sse.fallback_after_teardown",
            client.get_run(agent_a["id"], run_a["id"]).get("status") == "FINISHED",
            f"watch returned in {round(time.monotonic() - t0, 1)}s, {fallback_events} frames",
        )

        # Publish the producer's exact head to the shared repo (bundle path
        # moves the commit bytes; no source text is copied anywhere).
        pushed = push_head_to_fixture(
            members.get("repo.bundle"), str(head_a), f"gate/{workflow_id}"
        )
        check("exact_head.published", pushed, "side branch on shared fixture repo")

        # ---------------- worker B: consumer via artifact handoff ----------
        prompt_b = (
            "Work inside the repo/ directory. A previous worker's output was applied "
            "to this checkout via an artifact handoff. Read the file gate_marker.txt, "
            "append a new line 'consumer=antigravity' to it, then run: "
            "git add gate_marker.txt && git -c user.name=gate -c user.email=gate@localhost "
            "commit -m 'gate: consume marker'. Reply with just the new commit sha."
        )
        check(
            "B.prompt_carries_no_source",
            marker not in prompt_b and "producer=grok" not in prompt_b,
            "prompt references paths only",
        )
        created_b = create_with_retry(
            client,
            prompt_b,
            provider="antigravity",
            name="gate-wf-b-consumer",
            metadata=meta(workflow_id, "consume-artifact", "consumer"),
            workspace=workspace_decl(),
            handoff={"artifact_id": art_id},
        )
        corpus(created_b)
        agent_b, run_b = created_b["agent"], created_b["run"]
        record("B", {"agent": agent_b["id"], "run": run_b["id"], "provider": "antigravity"})

        # Wait for the turn to be genuinely in flight, grab stream frames,
        # then DELIBERATELY kill this client process mid-stream: os._exit —
        # no finally/cleanup, no persisted handles. Phase 2 must recover from
        # API key + workflow_id alone.
        run_b_live = wait_status(client, agent_b["id"], run_b["id"], {"RUNNING"}, PROVISION_WAIT_S)
        record("B.status_at_kill", run_b_live.get("status"))
        seen: list[str] = []
        deadline = time.monotonic() + 30.0
        stream = client.watch(
            agent_b["id"],
            run_b["id"],
            status_poll_s=8.0,
            read_timeout_s=20.0,
            max_reconnects=2,
        )
        try:
            for ev in stream:
                if isinstance(ev, SseEvent):
                    seen.append(str(ev.id))
                    corpus(ev.data)
                if len(seen) >= 3 or time.monotonic() > deadline:
                    break
        finally:
            stream.close()
        record("B.sse_frames_before_kill", seen)
        write_evidence(workflow_id, "phase1-killed")
        log(f"DELIBERATE KILL: client exits {KILL_EXIT} mid-stream; run {run_b['id']} continues")
        sys.stdout.flush()
        os._exit(KILL_EXIT)
        return KILL_EXIT  # unreachable
    finally:
        client.close()


# ----------------------------------------------------------------- phase 2


def _handles_from_recovery(rec: Any) -> list[tuple[str, str]]:
    return rec.handles(latest_only=True)


def phase2(args: argparse.Namespace) -> int:
    global PHASE
    PHASE = 2
    workflow_id = args.workflow_id
    prior = read_evidence(workflow_id)
    art_id = ((prior.get("results") or {}).get("A.artifact") or {}).get("id")
    # Carry phase-1 results and checks forward so the final evidence file is
    # complete (the kill prevented phase 1 from writing its own checks here).
    RESULTS.update(prior.get("results") or {})
    CHECKS.extend(c for c in (prior.get("checks") or []) if c.get("phase", 1) == 1)

    client = SbxClient(base_url=resolve_base_url(), api_key=resolve_api_key())
    verdict = "FAIL"
    try:
        _phase2_body(client, workflow_id, prior)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - record, then still clean up
        check("phase2.exception", False, f"{exc.__class__.__name__}: {exc}"[:200])
    finally:
        # ------------------------- scoped cleanup --------------------------
        cleanup_note = "skipped"
        try:
            out = client.http.delete(f"/v1/workflows/{workflow_id}")
            body: dict[str, Any] = {}
            try:
                body = out.json()
            except Exception:
                pass
            corpus(body)
            closed = client.list_agents(workflow_id=workflow_id)
            corpus(closed)
            leftover = [
                a["id"]
                for a in closed.get("agents") or []
                if a.get("status") not in {"closed", "timed_out", "lost"}
            ]
            check(
                "cleanup.scoped",
                out.status_code in (200, 404) and not leftover,
                f"http={out.status_code} leftover={leftover}",
            )
            cleanup_note = f"deleted workflow scope; leftover_agents={leftover}"
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask verdict
            check("cleanup.scoped", False, f"{exc.__class__.__name__}: {exc}"[:160])
        record("cleanup", cleanup_note)
        if art_id:
            try:
                patch = client.download_artifact(art_id, member="patch.diff")
                corpus(patch)
                check(
                    "artifact.survives_workflow_cleanup",
                    len(patch) > 0,
                    f"{len(patch)}B post-cleanup",
                )
            except SbxApiError as exc:
                check("artifact.survives_workflow_cleanup", False, str(exc)[:120])
        verdict = "PASS" if all(c["ok"] for c in CHECKS) else "FAIL"
        path = write_evidence(workflow_id, verdict)
        log(f"evidence -> {path}")
        log(f"VERDICT: {verdict}")
        client.close()
    return 0 if verdict == "PASS" else 1


def _phase2_body(client: SbxClient, workflow_id: str, prior: dict[str, Any]) -> None:
    base_sha = os.environ["GATE_BASE_SHA"]
    art_id = ((prior.get("results") or {}).get("A.artifact") or {}).get("id")
    if not check("phase1.evidence", bool(prior.get("results")), "phase-1 evidence file"):
        return
    # ---------------- recovery from api key + workflow_id only ---------
    rec = client.recover(workflow_id)
    corpus({"agents": rec.agents, "runs": rec.runs})
    found = {a["id"]: a for a in rec.agents}
    check(
        "recover.agents",
        len(found) >= 2
        and all((a.get("metadata") or {}).get("workflow_id") == workflow_id for a in rec.agents),
        f"agents={sorted(found)}",
    )
    wf_view = client._check(client.http.get(f"/v1/workflows/{workflow_id}"))
    corpus(wf_view)
    view_ids = {a.get("agent_id") for a in wf_view.get("agents") or []}
    check("recover.workflow_view", view_ids == set(found), f"view={sorted(view_ids)}")
    record("recover.progress", wf_view.get("progress"))

    a_id = (prior.get("results") or {}).get("A", {}).get("agent")
    a_run = (prior.get("results") or {}).get("A", {}).get("run")
    if not check("recover.A_handle", a_id is not None and a_run is not None):
        return
    b_id, b_run = next(
        ((aid, r["id"]) for aid, r in rec.latest_runs.items() if aid != a_id),
        (None, None),
    )
    if not check("recover.B_handle", b_id is not None):
        return
    record("B.recovered", {"agent": b_id, "run": b_run})
    # Every agent bound to this workflow (incl. retried legs) — the post-
    # redeploy recovery set must equal it exactly.
    created_ids: list[str | None] = [a_id, b_id]

    # ---------------- SSE resume + persisted fallback ------------------
    # The run may still be mid-flight (we killed the phase-1 client during
    # it); wait for dispatch-or-terminal before attaching the watcher.
    wait_status(
        client,
        b_id,
        b_run,
        {"RUNNING", "FINISHED", "ERROR", "CANCELLED", "EXPIRED", "UNKNOWN"},
        PROVISION_WAIT_S,
    )
    seq_b = collect_watch(client, b_id, b_run)
    run_b_final = client.wait(b_id, b_run, timeout_s=TURN_WAIT_S)
    corpus(run_b_final)
    check(
        "B.run_terminal",
        run_b_final.get("status") in {"FINISHED", "ERROR", "CANCELLED"},
        f"status={run_b_final.get('status')}",
    )
    check(
        "B.run_finished",
        run_b_final.get("status") == "FINISHED",
        f"status={run_b_final.get('status')} err={run_b_final.get('error')}",
    )
    record("B.sse_replayed", {"count": len(seq_b["ids"])})
    if seq_b["ids"]:
        mid = seq_b["ids"][len(seq_b["ids"]) // 2]
        # Bounded resume: watch() applies Last-Event-ID and ends on the
        # persisted-terminal poll; a bare stream_run() could idle forever
        # on a finished run's keepalive stream. A transient tail-exec failure
        # on a live sandbox yields zero frames — retry once and record both.
        attempts_frames: list[list[str]] = []
        for _try in range(2):
            resumed = [
                str(ev.id)
                for ev in client.watch(
                    b_id,
                    b_run,
                    last_event_id=mid,
                    status_poll_s=5.0,
                    read_timeout_s=20.0,
                    max_reconnects=2,
                )
                if isinstance(ev, SseEvent) and ev.id is not None
            ]
            attempts_frames.append(resumed)
            if resumed:
                break
        corpus({"resumed_ids": attempts_frames})
        best = attempts_frames[-1]
        check(
            "sse.resume_last_event_id",
            bool(best) and all(int(i) > int(mid) for i in best),
            f"last_event_id={mid} -> {[len(a) for a in attempts_frames]} frame counts",
        )
    else:
        check("sse.resume_last_event_id", False, "no event ids to resume from")

    ws_b = client.get_workspace(b_id)
    corpus(ws_b)
    head_b = ws_b.get("head_sha")
    record("B.head_sha", head_b)
    artifact_b = client.create_artifact(b_id, run_id=b_run)
    corpus(artifact_b)
    prior_art_head = ((prior.get("results") or {}).get("A.artifact") or {}).get("head_sha")
    check(
        "handoff.chain",
        artifact_b.get("base_sha") == prior_art_head,
        f"B artifact base={artifact_b.get('base_sha')} == A head={prior_art_head}",
    )
    record(
        "B.artifact",
        {"id": artifact_b.get("artifact_id"), "head_sha": artifact_b.get("head_sha")},
    )
    # B's work is done — close it now so its idle sandbox frees a slot for
    # the E/D legs under the shared per-key concurrency cap. The workflow
    # binding, runs and artifacts stay durable.
    closed_b = client.delete_agent(b_id)
    corpus(closed_b)
    check("B.teardown", closed_b.get("status") == "closed", closed_b.get("status"))

    # ---------------- exact-head reviewer/consumer ---------------------
    exact_head = (prior.get("results") or {}).get("exact_head.pushed_sha")
    check("exact_head.available", bool(exact_head), str(exact_head))
    c_id = c_run = None
    if exact_head:
        prompt_c = (
            "Work inside the repo/ directory. Read gate_marker.txt and verify its "
            "first line starts with 'wf_id='. Do not modify anything. "
            "Reply with exactly: REVIEW-OK followed by the first line you saw."
        )
        check(
            "C.prompt_carries_no_source",
            "producer=" not in prompt_c and "consumer=" not in prompt_c,
            "reviewer prompt references paths only",
        )
        created_c = create_with_retry(
            client,
            prompt_c,
            provider="devin",
            name="gate-wf-c-reviewer",
            metadata=meta(workflow_id, "review-head", "reviewer"),
            workspace=workspace_decl(),
            handoff={"head_sha": exact_head},
        )
        corpus(created_c)
        c_id, c_run = created_c["agent"]["id"], created_c["run"]["id"]
        created_ids.append(c_id)
        record("C", {"agent": c_id, "run": c_run, "provider": "devin"})
        # The workspace record exists only after prepare lands (during
        # provisioning) — wait for dispatch-or-terminal before reading it.
        wait_status(
            client,
            c_id,
            c_run,
            {"RUNNING", "FINISHED", "ERROR", "CANCELLED", "EXPIRED", "UNKNOWN"},
            PROVISION_WAIT_S,
        )
        ws_c = client.get_workspace(c_id)
        corpus(ws_c)
        check(
            "C.exact_head_checkout",
            ws_c.get("checkout_sha") == exact_head and ws_c.get("head_sha") == exact_head,
            f"checkout={ws_c.get('checkout_sha')}",
        )
        collect_watch(client, c_id, c_run)
        run_c_final = client.wait(c_id, c_run, timeout_s=TURN_WAIT_S)
        corpus(run_c_final)
        check(
            "C.run_finished",
            run_c_final.get("status") == "FINISHED",
            f"status={run_c_final.get('status')} err={run_c_final.get('error')}",
        )
        reviewed = client.review_workspace(c_id, head_sha=exact_head)
        corpus(reviewed)
        check(
            "C.review_pin",
            reviewed.get("reviewed_head_sha") == exact_head,
            str(reviewed.get("reviewed_head_sha")),
        )
        try:
            client.review_workspace(c_id, head_sha=base_sha)
            check("C.review_mismatch_rejected", False, "wrong head was accepted")
        except SbxApiError as exc:
            corpus({"code": exc.code, "status": exc.status})
            check(
                "C.review_mismatch_rejected",
                "head_sha_mismatch" in str(exc),
                f"{exc.status} {exc.code}",
            )
        closed_c = client.delete_agent(c_id)
        corpus(closed_c)
        check("C.teardown", closed_c.get("status") == "closed", closed_c.get("status"))

    # ---------------- error leg (deterministic prepare failure) --------
    # Runs while the grok account slot is free (A torn down, D not yet
    # created): workspace prepare fails with base_sha_mismatch -> run-1
    # ERROR and the agent is auto-closed, freeing the slot for D.
    bad_ws = dict(workspace_decl())
    bad_ws["base_sha"] = "0" * 40
    e_id = e_run = None
    try:
        created_e = create_with_retry(
            client,
            "Reply with exactly: NOOP",
            provider="grok",
            name="gate-wf-e-error",
            metadata=meta(workflow_id, "error-leg", "worker"),
            workspace=bad_ws,
        )
        corpus(created_e)
        e_id, e_run = created_e["agent"]["id"], created_e["run"]["id"]
        created_ids.append(e_id)
        record("E", {"agent": e_id, "run": e_run, "provider": "grok"})
    except SbxApiError as exc:
        corpus({"code": exc.code, "status": exc.status})
        check("E.create_accepted", False, f"{exc.status} {exc.code}")
    if e_id:
        run_e_final = client.wait(e_id, e_run, timeout_s=PROVISION_WAIT_S)
        corpus(run_e_final)
        err_msg = json.dumps(run_e_final.get("error") or {})
        check(
            "E.run_error_base_sha_mismatch",
            run_e_final.get("status") == "ERROR" and "base_sha_mismatch" in err_msg,
            f"status={run_e_final.get('status')} err={err_msg[:160]}",
        )
        e_agent = client.get_agent(e_id)
        corpus(e_agent)
        check(
            "E.agent_auto_closed",
            e_agent.get("status") in {"closed", "lost"},
            f"agent={e_agent.get('status')}",
        )
    else:
        check("E.run_error_base_sha_mismatch", False, "no provider accepted the create")

    # ---------------- cancel leg (in-flight cancel) --------------------
    # A run that never reaches RUNNING within the reaper's create grace ends
    # `lost` -> UNKNOWN — the cancel never engaged, so the leg retried with a
    # fresh agent rather than claiming a cancel it never sent.
    d_id = d_run = None
    run_d_final: dict[str, Any] = {}
    for attempt in range(3):
        try:
            created_d = create_with_retry(
                client,
                "Run the shell command `sleep 120` in bash, wait for it to finish, "
                "then reply with exactly: DONE",
                provider="grok",
                name=f"gate-wf-d-cancel-{attempt + 1}",
                metadata=meta(workflow_id, f"cancel-leg-{attempt + 1}", "worker"),
            )
        except SbxApiError as exc:
            corpus({"code": exc.code, "status": exc.status})
            record(f"D.attempt{attempt + 1}.create_error", f"{exc.status} {exc.code}")
            break
        corpus(created_d)
        d_id, d_run = created_d["agent"]["id"], created_d["run"]["id"]
        created_ids.append(d_id)
        live_d = wait_status(
            client,
            d_id,
            d_run,
            {"RUNNING", "FINISHED", "ERROR", "CANCELLED", "EXPIRED", "UNKNOWN"},
            PROVISION_WAIT_S,
        )
        record(f"D.attempt{attempt + 1}.status_before_cancel", live_d.get("status"))
        if live_d.get("status") in {"RUNNING", "CREATING"}:
            # The cancel route persists CANCELLED both pre-dispatch (CREATING)
            # and in-flight (RUNNING); either is a valid cancel leg.
            try:
                cancel_resp = client.cancel(d_id, d_run)
                corpus(cancel_resp)
                record(f"D.attempt{attempt + 1}.cancel_response", cancel_resp.get("status"))
            except SbxApiError as exc:
                corpus({"code": exc.code, "status": exc.status})
                record(f"D.attempt{attempt + 1}.cancel_error", f"{exc.status} {exc.code}")
            run_d_final = client.wait(d_id, d_run, timeout_s=TURN_WAIT_S)
            corpus(run_d_final)
            if run_d_final.get("status") == "CANCELLED":
                break
            d_agent = client.get_agent(d_id)
            corpus(d_agent)
            record(
                f"D.attempt{attempt + 1}.final",
                {"run": run_d_final.get("status"), "agent": d_agent.get("status")},
            )
        else:
            log(
                f"  cancel leg attempt {attempt + 1}: run never dispatched "
                f"({live_d.get('status')}); retrying"
            )
        try:
            client.delete_agent(d_id)
        except SbxApiError:
            pass
    record("D", {"agent": d_id, "run": d_run, "provider": "grok"})
    check(
        "D.run_cancelled",
        run_d_final.get("status") == "CANCELLED",
        f"status={run_d_final.get('status')}",
    )

    # ---------------- wait_many mixed terminal truth -------------------
    handles = [(a_id, a_run), (b_id, b_run)]
    if d_id:
        handles.append((d_id, d_run))
    if c_id:
        handles.append((c_id, c_run))
    if e_id:
        handles.append((e_id, e_run))
    results = client.wait_many(handles, timeout_s=120.0)
    corpus({f"{k[0]}/{k[1]}": v for k, v in results.items()})
    got = {h: (results.get(h) or {}).get("status") for h in handles}
    record("wait_many.statuses", {f"{a}/{r}": s for (a, r), s in got.items()})
    expected = {
        (a_id, a_run): "FINISHED",
        (b_id, b_run): "FINISHED",
    }
    if d_id:
        expected[(d_id, d_run)] = "CANCELLED"
    if c_id:
        expected[(c_id, c_run)] = "FINISHED"
    if e_id:
        expected[(e_id, e_run)] = "ERROR"
    check(
        "wait_many.mixed_terminal_truth",
        got == expected,
        json.dumps({f"{a}/{r}": s for (a, r), s in got.items()}),
    )

    # ------------- control-plane restart/redeploy durability -----------
    env = dict(os.environ)
    # Provider image builds bake host CLI binaries (agy/grok) resolved via
    # ~/.local/bin — the isolated HOME hides them, so point the build at
    # whatever is on PATH instead.
    for var, cli in (("SBX_AGY_BIN", "agy"), ("SBX_GROK_BIN", "grok")):
        if not env.get(var):
            found = shutil.which(cli)
            if found:
                env[var] = found
    up = subprocess.run(
        [sys.executable, "-m", "sbx", "upgrade", "--json"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    corpus({"rc": up.returncode})
    # stdout carries image-build logs before the trailing --json payload;
    # parse from the last top-level '{'.
    blob = up.stdout
    start = blob.rfind("\n{")
    if start < 0 and blob.lstrip().startswith("{"):
        start = blob.index("{")
    try:
        up_doc = json.loads(blob[start:]) if start >= 0 else {}
    except json.JSONDecodeError:
        up_doc = {}
    check(
        "redeploy.upgrade_ok",
        up.returncode == 0 and up_doc.get("ok") is True,
        f"rc={up.returncode} tail={up.stdout.strip()[-160:]} {up.stderr.strip()[-160:]}",
    )
    record("redeploy.durable", up_doc.get("durable"))

    post_a = client.get_run(a_id, a_run)
    post_b = client.get_run(b_id, b_run)
    corpus(post_a)
    corpus(post_b)
    check(
        "redeploy.runs_durable",
        post_a.get("status") == "FINISHED" and post_b.get("status") == "FINISHED",
        f"A={post_a.get('status')} B={post_b.get('status')}",
    )
    if art_id:
        man = client.get_artifact(art_id)
        corpus(man)
        patch = client.download_artifact(art_id, member="patch.diff")
        corpus(patch)
        check(
            "redeploy.artifact_durable",
            man.get("artifact_id") == art_id and len(patch) > 0,
            f"{len(patch)}B after redeploy",
        )
    rec2 = client.recover(workflow_id)
    corpus({"agents": rec2.agents})
    expected_bound = {x for x in created_ids if x}
    check(
        "redeploy.recovery_durable",
        {a["id"] for a in rec2.agents} == expected_bound,
        f"agents={sorted(a['id'] for a in rec2.agents)}",
    )
    if c_id:
        ws_c2 = client.get_workspace(c_id)
        corpus(ws_c2)
        check(
            "redeploy.review_durable",
            ws_c2.get("reviewed_head_sha") == exact_head,
            str(ws_c2.get("reviewed_head_sha")),
        )

    # ------------------------- leak scan -------------------------------
    leak_scan()


def main() -> int:
    parser = argparse.ArgumentParser(prog="workflow_gate")
    parser.add_argument("phase", choices=["phase1", "phase2"])
    parser.add_argument("--workflow-id", default=None)
    args = parser.parse_args()

    missing = [k for k in ("GATE_REPO", "GATE_BASE_SHA") if not os.environ.get(k)]
    if missing:
        print(f"SKIP: missing env {missing}", file=sys.stderr)
        return 2
    if args.phase == "phase2" and not args.workflow_id:
        print("SKIP: phase2 requires --workflow-id", file=sys.stderr)
        return 2
    if args.phase == "phase1":
        return phase1(args)
    return phase2(args)


if __name__ == "__main__":
    raise SystemExit(main())
