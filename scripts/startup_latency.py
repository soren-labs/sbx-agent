"""Bounded startup-latency measurement for fresh Sessions (issue #228 LEGACY198-B).

For each sample it creates one fresh Session with a short message, follows the committed
event journal, and records (seconds after the create request was sent):

* ``accepted``       — the create request returned (Turn queued);
* ``bound``          — ``executor.bound`` (sandbox allocated and runtime handshake done),
                       with the server's ``provisioning`` breakdown in milliseconds;
* ``cli_started``    — ``execution.started`` (the official CLI launched);
* ``first_content``  — the first ``message.part_*`` or ``tool.*`` event: first visible
                       model output;
* ``finished``       — the Turn's terminal event.

It prints one JSON document with every sample and p50/p95 per phase, then closes every
Session it created (which releases its compute). It never runs more than 10 samples and
never prints credentials.

Usage::

    SBX_API_URL=https://staging.example SBX_API_KEY=... \\
      uv run python scripts/startup_latency.py --workspace W --project P --samples 5 --label cold
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
from typing import Any

import httpx

MAX_SAMPLES = 10
TERMINAL = ("turn.succeeded", "turn.failed", "turn.cancelled", "turn.interrupted")


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (no interpolation): p50/p95 of a handful of samples."""
    if not values:
        return None
    ranked = sorted(values)
    return ranked[max(0, math.ceil(p / 100 * len(ranked)) - 1)]


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    phases = ("accepted", "bound", "cli_started", "first_content", "finished")
    out: dict[str, Any] = {}
    for phase in phases:
        values = [s[phase] for s in samples if s.get(phase) is not None]
        out[phase] = {
            "n": len(values),
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
            "max": max(values) if values else None,
        }
    return out


def measure_one(
    http: httpx.Client, workspace: str, project: str | None, message: str, timeout: float
) -> dict[str, Any]:
    body: dict[str, Any] = {"message": {"content": message}, "title": "startup latency sample"}
    if project:
        body["project_id"] = project
    t0 = time.monotonic()
    created = http.post(
        f"/api/workspaces/{workspace}/sessions",
        json=body,
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    created.raise_for_status()
    sample: dict[str, Any] = {"accepted": round(time.monotonic() - t0, 3)}
    data = created.json()
    sid = data["session_id"]
    sample["session_id"] = sid
    after = 0
    while time.monotonic() - t0 < timeout:
        page = http.get(f"/api/sessions/{sid}/events", params={"after": after, "limit": 500})
        page.raise_for_status()
        now = round(time.monotonic() - t0, 3)
        for event in page.json()["items"]:
            kind = event["type"]
            if kind == "executor.bound" and "bound" not in sample:
                sample["bound"] = now
                sample["provisioning_ms"] = (event.get("payload") or {}).get("provisioning")
            elif kind == "execution.started" and "cli_started" not in sample:
                sample["cli_started"] = now
            elif kind.startswith(("message.part_", "tool.")) and "first_content" not in sample:
                sample["first_content"] = now
            elif kind in TERMINAL:
                sample["finished"] = now
                sample["outcome"] = kind
        after = page.json()["next_after"]
        if "finished" in sample:
            return sample
        time.sleep(0.1)
    sample["outcome"] = "timeout"
    return sample


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="startup_latency")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--project")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--message", default="Reply with exactly: SBX_LATENCY_OK")
    parser.add_argument("--label", default="", help="condition, e.g. warm-image / cold-image")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= MAX_SAMPLES:
        parser.error(f"--samples must be between 1 and {MAX_SAMPLES}")
    url, key = os.environ.get("SBX_API_URL"), os.environ.get("SBX_API_KEY")
    if not url or not key:
        parser.error("SBX_API_URL and SBX_API_KEY must be set in the environment")
    samples: list[dict[str, Any]] = []
    with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=60) as http:
        try:
            for _ in range(args.samples):
                samples.append(
                    measure_one(http, args.workspace, args.project, args.message, args.timeout)
                )
        finally:
            for s in samples:  # cleanup: closing releases each Session's compute
                http.post(
                    f"/api/sessions/{s['session_id']}/closures",
                    headers={"Idempotency-Key": uuid.uuid4().hex},
                )
    json.dump(
        {"label": args.label, "samples": samples, "summary": summarize(samples)},
        sys.stdout,
        indent=2,
    )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
