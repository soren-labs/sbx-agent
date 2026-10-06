"""P2.1-S0 spike (Linear SOR-73): Devin CLI clean-room credential portability in Modal.

Verifies, inside real Modal Sandboxes with Secret/file injection only
(no host HOME mounts, no host GitHub/Linear/Desktop credentials):
  1. `devin auth status` succeeds from a fresh $HOME given only credentials.toml
  2. `devin -p` (swe-2-high) completes a non-interactive turn, stdin closed
  3. ACP_BACKEND=windsurf in env breaks auth -> runner must strip it (negative control)
  4. credential file hash before vs after the turn (rewrite/refresh detection)
  5. N independent Sandboxes concurrently using the SAME account credentials

Credential values are never printed: the file is base64'd into a Modal Secret
env var, materialized inside the Sandbox, and only a 16-char sha256 prefix is
recorded. Every Sandbox is terminated before exit.

Run:  uv run python spike/p2/modal_devin_spike.py --concurrency 2,4,8
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import modal

DEVIN_VERSION = "3000.10.21"
DEVIN_DIST = Path.home() / ".local/share/devin/cli/_versions" / DEVIN_VERSION
APP_NAME = "sbx-spike-devin"
WORK = "/work"
HOME_DIR = f"{WORK}/home"
CRED_REL = ".local/share/devin/credentials.toml"
CRED_ABS = f"{HOME_DIR}/{CRED_REL}"
OUT_DIR = Path(__file__).parent / "out"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ca-certificates", "curl", "procps")
    .add_local_dir(str(DEVIN_DIST), remote_path=f"/opt/devin/{DEVIN_VERSION}", copy=True)
    .run_commands(
        f"ln -sf /opt/devin/{DEVIN_VERSION}/bin/devin /usr/local/bin/devin",
        "devin --version",
    )
)

# Deliberately minimal: no ACP_BACKEND, no DEVIN_API_KEY/WINDSURF_API_KEY —
# auth must come from the injected credentials file alone.
SANDBOX_ENV = {
    "HOME": HOME_DIR,
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "TERM": "dumb",
    "SBX_WORK": WORK,
}

RESULTS: dict[str, object] = {}
LOCK = threading.Lock()


def record(key: str, value: object) -> None:
    with LOCK:
        RESULTS[key] = value
    print(f"[result] {key} = {json.dumps(value, ensure_ascii=False, default=str)}", flush=True)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sanitize(text: str) -> str:
    """Strip identifiers that must not land in the repo report."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "REDACTED@REDACTED", text)
    text = re.sub(r"\b(user|org|team|account|devin-team\$account)-[0-9a-zA-Z$]{8,}\b", r"\1-REDACTED", text)
    return text


def cred_source() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(xdg) / "devin" / "credentials.toml"


def sh(sb: modal.Sandbox, cmd: str, timeout: int | None = 120) -> tuple[int, str, str]:
    p = sb.exec("bash", "-c", cmd, timeout=timeout)
    out = p.stdout.read()
    err = p.stderr.read()
    rc = p.wait()
    return rc, out, err


def run_one(app: modal.App, cred_b64: str, model: str, smoke_timeout: int,
            batch: int, idx: int) -> dict:
    """Full clean-room flow in one Sandbox. Never logs credential material."""
    sb: modal.Sandbox | None = None
    try:
        t0 = time.perf_counter()
        sb = modal.Sandbox.create(
            "sleep", "infinity",
            app=app,
            image=image,
            secrets=[modal.Secret.from_dict({"SBX_CRED_B64": cred_b64})],
            env=SANDBOX_ENV,
            cpu=(1, 2),
            memory=(1024, 4096),
            timeout=1800,
            workdir=WORK,
            tags={"spike": "sor-73", "name": f"c{batch}-{idx}"},
        )
        t_create = time.perf_counter() - t0
        res: dict[str, object] = {
            "sandbox_id": sb.object_id, "create_s": round(t_create, 2),
        }

        # 1) materialize credentials.toml from the Secret env var, mode 600
        rc, out, err = sh(sb, (
            f"mkdir -p '{HOME_DIR}/.local/share/devin' '{WORK}/ws' && "
            f"printf %s \"$SBX_CRED_B64\" | base64 -d > '{CRED_ABS}' && "
            f"chmod 600 '{CRED_ABS}' && "
            # prove the env is otherwise clean
            f"echo ACP_BACKEND=${{ACP_BACKEND:-<unset>}}; "
            f"echo DEVIN_API_KEY=${{DEVIN_API_KEY:+<set>}}${{DEVIN_API_KEY:-<unset>}}; "
            f"echo WINDSURF_API_KEY=${{WINDSURF_API_KEY:+<set>}}${{WINDSURF_API_KEY:-<unset>}}"
        ))
        res["inject_rc"] = rc
        res["env_check"] = out.strip().splitlines()
        if rc != 0:
            res["err"] = sanitize(err[-300:])
            return res

        # 2) auth status — must succeed from the file alone
        rc, out, err = sh(sb, "devin auth status", timeout=60)
        res["auth_rc"] = rc
        res["auth_logged_in"] = "Logged in" in out
        m = re.search(r"Tier:\s+(.+)", out)
        res["auth_tier"] = m.group(1).strip() if m else None

        # 3) negative control: ACP_BACKEND=windsurf must break auth (runner hazard)
        if idx == 0:
            _, out_acp, _ = sh(sb, "ACP_BACKEND=windsurf devin auth status", timeout=60)
            res["acp_backend_breaks_auth"] = "Not logged in" in out_acp

        # 4) credential hash before the turn (16-char prefix only)
        _, h_before, _ = sh(sb, f"sha256sum '{CRED_ABS}' | cut -c1-16")
        res["cred_hash16_before"] = h_before.strip()

        # 5) non-interactive turn, stdin closed; strip the secret env var from
        #    the CLI child env exactly as the runner must (contract §凭证注入)
        t1 = time.perf_counter()
        rc, out, err = sh(sb, (
            f"cd '{WORK}/ws' && timeout {smoke_timeout} "
            f"env -u SBX_CRED_B64 devin -p 'Reply with exactly: PONG' "
            f"--model {model} --respect-workspace-trust false </dev/null"
        ), timeout=smoke_timeout + 60)
        res["smoke_rc"] = rc
        res["smoke_s"] = round(time.perf_counter() - t1, 1)
        res["smoke_pong"] = "PONG" in out
        res["smoke_tail"] = sanitize(out[-200:])
        if rc != 0:
            res["smoke_err_tail"] = sanitize(err[-200:])

        # 6) hash after — did the CLI rewrite/refresh the credential file?
        _, h_after, _ = sh(sb, f"sha256sum '{CRED_ABS}' | cut -c1-16")
        res["cred_hash16_after"] = h_after.strip()
        res["cred_hash_changed"] = h_before.strip() != h_after.strip()

        # what else did the CLI create under the fresh HOME (names only)
        _, listing, _ = sh(sb, (
            f"find '{HOME_DIR}' -type f | sed 's|^{HOME_DIR}/||' | sort | head -20"
        ))
        res["home_files_created"] = listing.strip().splitlines()
        return res
    except Exception as e:  # noqa: BLE001
        return {"err": repr(e), "tb": sanitize(traceback.format_exc()[-800:])}
    finally:
        if sb is not None:
            sb.terminate()


def run_batch(app: modal.App, cred_b64: str, model: str, smoke_timeout: int,
              n: int) -> dict:
    log(f"batch: {n} concurrent Sandboxes, same account credentials")
    t0 = time.perf_counter()
    with ThreadPoolExecutor(n) as ex:
        results = list(ex.map(lambda i: run_one(app, cred_b64, model, smoke_timeout, n, i),
                              range(n)))
    wall = time.perf_counter() - t0
    ok = all(
        r.get("auth_logged_in") and r.get("smoke_pong") and r.get("smoke_rc") == 0
        for r in results
    )
    return {
        "n": n,
        "wall_s": round(wall, 1),
        "max_sandbox_s": max((r.get("smoke_s", 0) for r in results), default=0),
        "all_pass": ok,
        "cred_hash_changed_any": any(r.get("cred_hash_changed") for r in results),
        "results": results,
        "verdict": "PASS" if ok else "FAIL",
    }


def list_leftovers(app: modal.App) -> list[str]:
    return [s.object_id for s in modal.Sandbox.list(app_id=app.app_id, tags={"spike": "sor-73"})]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", default="2,4,8",
                    help="comma-separated sandbox counts to test, e.g. 2,4,8")
    ap.add_argument("--model", default="swe-2-high")
    ap.add_argument("--smoke-timeout", type=int, default=300)
    args = ap.parse_args()

    cred_path = cred_source()
    if not cred_path.is_file():
        print(f"FATAL: {cred_path} not found", flush=True)
        return 2
    cred_bytes = cred_path.read_bytes()
    cred_b64 = base64.b64encode(cred_bytes).decode()
    record("0.params", {
        "devin": DEVIN_VERSION, "modal": modal.__version__, "model": args.model,
        "cred_file": str(cred_path).replace(str(Path.home()), "~"),
        "cred_bytes": len(cred_bytes),
        "cred_hash16_host": hashlib.sha256(cred_bytes).hexdigest()[:16],
        "concurrency_plan": args.concurrency,
    })

    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    t0 = time.perf_counter()
    log("building/checking image (pinned devin dist is ~174 MB, cached after first build)")
    image.build(app)
    record("0.image_build_or_check_s", round(time.perf_counter() - t0, 1))

    try:
        for n in [int(x) for x in args.concurrency.split(",") if x.strip()]:
            record(f"conc{n}", run_batch(app, cred_b64, args.model, args.smoke_timeout, n))
    except Exception:  # noqa: BLE001
        record("error", sanitize(traceback.format_exc()[-2000:]))
    finally:
        time.sleep(3)
        left = list_leftovers(app)
        for sid in left:
            modal.Sandbox.from_id(sid).terminate()
        record("9.leftovers_terminated", left)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_file = OUT_DIR / "modal_devin.json"
        merged: dict[str, object] = {}
        if out_file.is_file():
            try:
                merged = json.loads(out_file.read_text())
            except json.JSONDecodeError:
                merged = {}
        merged.update(RESULTS)
        out_file.write_text(json.dumps(merged, indent=2, ensure_ascii=False, default=str))
        log(f"results written to {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
