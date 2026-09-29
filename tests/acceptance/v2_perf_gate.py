"""SOR-268 V2 control-plane perf gate — re-measures the SOR-260 B1-B5
findings against the async-ACK + indexed read-model architecture.

Two modes:

- Emulated (default, no cloud creds): the real ``create_app`` FastAPI
  control plane served by uvicorn on loopback, with every durable store
  and the sandbox backend wrapped in per-call latency shims — each Dict
  op sleeps ``--dict-ms`` (default 250) and each sandbox op sleeps
  ``--sandbox-ms`` (default 500), standing in for Modal round-trips. The
  wrappers count remote calls so the report includes op-counts per probe,
  not just wall time.
- ``--base-url`` mode: identical probes against a deployed plane (the
  SOR-271 warm gate). Only timings are measurable remotely.

Run locally (from repo root):

    uv run python -m tests.acceptance.v2_perf_gate

Against a deploy:

    uv run python -m tests.acceptance.v2_perf_gate \
        --base-url "$SBX_BASE_URL" --api-key-file <bootstrap.key>

Budgets (warm): create/message/cancel/retry ACK P95 < 1s;
detail P95 < 1.5s; list-100 flat; 20 SSE clients while unrelated reads
stay in budget with no >5s spike; control plane keeps serving during a
bulk close. Emulated mode prints a PASS/FAIL verdict against budgets
scaled to the emulated latency model (emulated P95 bounds = the durable
remote ops a path still performs × dict/sandbox latency + slack).

Exit codes: 0 = PASS, 1 = FAIL, 2 = prerequisites missing.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dataclasses import dataclass  # noqa: E402

import httpx  # noqa: E402

# ---------------------------------------------------------------------------
# latency-injecting proxy
# ---------------------------------------------------------------------------


class _SlowProxy:
    """Charges ``ms`` once per delegated call and counts calls by name —
    stands in for one remote round-trip (Modal Dict op / sandbox RPC).
    ``ns`` namespaces the counter keys so different stores stay
    distinguishable (``accounts.items`` vs ``get``)."""

    def __init__(self, inner: Any, ms: float, counter: dict[str, int], ns: str = "") -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_ms", ms)
        object.__setattr__(self, "_counter", counter)
        object.__setattr__(self, "_ns", ns)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_inner"), name)
        if not callable(attr):
            return attr
        ms = object.__getattribute__(self, "_ms")
        counter = object.__getattribute__(self, "_counter")
        ns = object.__getattribute__(self, "_ns")

        def _call(*args: Any, **kwargs: Any) -> Any:
            key = f"{ns}{name}"
            counter[key] = counter.get(key, 0) + 1
            out = attr(*args, **kwargs)
            if ms:
                time.sleep(ms / 1000.0)
            return out

        return _call

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_inner"), name, value)


class _HarnessDict:
    """``modal.Dict``-shaped fake for the emulated plane: string keys,
    arbitrary values — every method name is counted through the
    ``_SlowProxy`` so ``items``/``keys`` full scans are visible."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def put(self, key: str, value: Any) -> None:
        self.data[key] = value

    def pop(self, key: str) -> Any:
        return self.data.pop(key)

    def update(self, mapping: dict[str, Any]) -> None:
        self.data.update(mapping)

    def contains(self, key: str) -> bool:
        return key in self.data

    def items(self) -> Any:
        return iter(list(self.data.items()))

    def keys(self) -> Any:
        return iter(list(self.data))


# ---------------------------------------------------------------------------
# probe machinery
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    name: str
    samples: list[float]
    budget_ms: float | None

    @property
    def p50(self) -> float:
        return statistics.median(self.samples) if self.samples else float("nan")

    @property
    def p95(self) -> float:
        if not self.samples:
            return float("nan")
        xs = sorted(self.samples)
        idx = max(0, int(len(xs) * 0.95) - 1)
        return xs[idx]

    @property
    def worst(self) -> float:
        return max(self.samples) if self.samples else float("nan")

    def ok(self) -> bool:
        return self.budget_ms is None or self.p95 <= self.budget_ms

    def line(self) -> str:
        verdict = "PASS" if self.ok() else "FAIL"
        budget = f"{self.budget_ms * 1000:.0f}ms" if self.budget_ms is not None else "-"
        return (
            f"{self.name:<38} n={len(self.samples):<3} "
            f"p50={self.p50 * 1000:7.1f}ms  p95={self.p95 * 1000:7.1f}ms  "
            f"max={self.worst * 1000:7.1f}ms  budget={budget:>8}  {verdict}"
        )


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[max(0, int(len(s) * q) - 1)]


def _time(http: httpx.Client, fn) -> tuple[float, Any]:
    t0 = time.perf_counter()
    out = fn(http)
    return time.perf_counter() - t0, out


def _timed_read(http: httpx.Client, fn) -> float:
    """A read probe where starvation (client timeout) is itself the
    measurement — a wedged pre-fix plane must not crash the gate."""
    t0 = time.perf_counter()
    try:
        fn(http)
    except httpx.HTTPError:
        pass
    return time.perf_counter() - t0


def _wait_bound(
    http: httpx.Client, headers: dict[str, str], session_id: str, timeout: float = 120.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = http.get(f"/v2/sessions/{session_id}", headers=headers).json()
        session = body["session"]
        # ``queued``/``queued_work`` = dispatch not yet committed; every other
        # status means the durable bind already happened (or is terminal).
        if session["status"] not in ("queued", "queued_work"):
            return body
        time.sleep(0.5)
    raise TimeoutError(f"session {session_id} never left the queue")


# ---------------------------------------------------------------------------
# probe suite (shared between emulated and --base-url modes)
# ---------------------------------------------------------------------------


def run_probes(
    base_url: str,
    headers: dict[str, str],
    *,
    n_sessions: int,
    sse_clients: int,
    bulk_n: int,
    budgets: dict[str, float],
    op_counter: dict[str, int] | None = None,
) -> tuple[list[ProbeResult], list[tuple[str, dict[str, int]]]]:
    results: list[ProbeResult] = []

    probe_ops: list[tuple[str, dict[str, int]]] = []

    def mark(name: str) -> None:
        if op_counter is not None:
            for key in op_counter:
                op_counter[key] = 0

    def ops() -> str:
        if op_counter is None:
            return ""
        snap = dict(sorted(op_counter.items()))
        probe_ops.append((results[-1].name if results else "", snap))
        return "  remote-ops: " + json.dumps(snap)

    create_seq = 0

    def create_one(http: httpx.Client) -> httpx.Response:
        nonlocal create_seq
        create_seq += 1
        # Unique Idempotency-Key: the replay-check path (idem get +
        # owner prefetch + put) is the worst-case create — the path the
        # SOR-271 gate timed.
        h = {**headers, "Idempotency-Key": f"gate-{time.time_ns()}-{create_seq}"}
        return http.post(
            "/v2/sessions",
            json={
                "prompt": "Create hello.txt in the workspace.",
                "execution": {"provider": "codex"},
            },
            headers=h,
        )

    def detail_one(sid: str):
        return lambda http: http.get(f"/v2/sessions/{sid}", headers=headers)

    def list_one(limit: int):
        return lambda http: http.get("/v2/sessions", params={"limit": limit}, headers=headers)

    def message_one(sid: str):
        return lambda http: http.post(
            f"/v2/sessions/{sid}/messages",
            json={"prompt": "follow up"},
            headers=headers,
        )

    http = httpx.Client(base_url=base_url, timeout=60.0)

    # ---- B1: create ACK ----------------------------------------------------
    mark("create")
    samples: list[float] = []
    session_ids: list[str] = []
    for _ in range(n_sessions):
        dt, resp = _time(http, create_one)
        samples.append(dt)
        assert resp.status_code in (200, 201, 202), resp.text
        session_ids.append(resp.json()["session"]["id"])
    results.append(ProbeResult("POST /v2/sessions (create ACK)", samples, budgets.get("create")))
    if op_counter is not None:
        # SOR-271: the accounts full scan must be gone from the create
        # path. One ``items`` is tolerated — the first-ever listing on a
        # pre-index Dict pays one migration scan, then self-heals.
        results.append(
            ProbeResult(
                "accounts full scans during creates",
                [float(op_counter.get("accounts.items", 0))],
                budgets.get("account_items"),
            )
        )
    print(f"B1 create remote-ops{ops()}", file=sys.stderr)

    # Wait for binds so detail/list exercise the full settle path.
    bound_ids = [_wait_bound(http, headers, sid)["session"]["id"] for sid in session_ids]

    # ---- B2: detail + list -------------------------------------------------
    samples = []
    mark("detail")
    for sid in bound_ids * 2:
        dt, resp = _time(http, detail_one(sid))
        assert resp.status_code == 200, resp.text
        samples.append(dt)
    results.append(ProbeResult("GET /v2/sessions/{id} (detail)", samples, budgets.get("detail")))
    print(f"B2 detail remote-ops{ops()}", file=sys.stderr)

    for limit in (25, 100):
        mark(f"list-{limit}")
        dt, resp = _time(http, list_one(limit))
        assert resp.status_code == 200, resp.text
        results.append(ProbeResult(f"GET /v2/sessions?limit={limit}", [dt], budgets.get("list")))
        print(f"B2 list-{limit} remote-ops{ops()}", file=sys.stderr)

    # ---- B5: message ACK ----------------------------------------------------
    samples = []
    mark("message")
    for sid in bound_ids[:3]:
        dt, resp = _time(http, message_one(sid))
        assert resp.status_code in (200, 201, 202, 409), resp.text
        samples.append(dt)
    results.append(ProbeResult("POST .../messages (ACK)", samples, budgets.get("message")))
    print(f"B5 message remote-ops{ops()}", file=sys.stderr)

    # ---- cancel / retry ACK -------------------------------------------------
    samples = []
    mark("cancel")
    cancel_targets: list[str] = []
    for sid in bound_ids[:3]:
        dt, resp = _time(
            http,
            lambda h, s=sid: h.post(f"/v2/sessions/{s}/cancel", headers=headers, timeout=60.0),
        )
        if resp.status_code in (200, 202, 409):
            samples.append(dt)
            cancel_targets.append(sid)
    results.append(ProbeResult("POST .../cancel (ACK)", samples, budgets.get("cancel")))
    print(f"cancel remote-ops{ops()}", file=sys.stderr)

    samples = []
    mark("retry")
    for sid in bound_ids[:2]:
        dt, resp = _time(
            http,
            lambda h, s=sid: h.post(f"/v2/sessions/{s}/retry", headers=headers, timeout=60.0),
        )
        if resp.status_code in (200, 202, 409):
            samples.append(dt)
    if samples:
        results.append(ProbeResult("POST .../retry (ACK)", samples, budgets.get("retry")))
    print(f"retry remote-ops{ops()}", file=sys.stderr)

    # ---- B3: SSE fanout must not starve reads -------------------------------
    if bound_ids:
        # Spread clients across up to 4 sessions so the probe exercises
        # several hubs (tail execs + status pollers), not only the
        # one-hub/many-queues dedup case.
        stream_targets = bound_ids[-4:]
        streams: list[Any] = []
        responses: list[Any] = []
        opened = 0
        try:
            for i in range(sse_clients):
                sid = stream_targets[i % len(stream_targets)]
                # Streams are long-lived: only the connect/first-byte wait is
                # bounded. On the pre-fix plane this is where clients stall.
                s = http.stream(
                    "GET",
                    f"/v2/sessions/{sid}/events",
                    headers=headers,
                    timeout=httpx.Timeout(None, connect=30.0),
                )
                try:
                    resp = s.__enter__()
                except httpx.HTTPError as exc:
                    print(f"SSE open failed: {exc!r}", file=sys.stderr)
                    s.__exit__(None, None, None)
                    break
                assert resp.status_code == 200
                streams.append(s)
                responses.append(resp)
                opened += 1
            time.sleep(1.0)  # let every client attach to the hub
            samples = []
            worst_spike = 0.0
            # A starved read on the pre-fix plane times out at the client
            # ceiling — that IS the B3 symptom, so record it as a sample.
            for _ in range(10):
                dt = _timed_read(http, detail_one(sid))
                samples.append(dt)
                worst_spike = max(worst_spike, dt)
            for _ in range(5):
                dt = _timed_read(http, list_one(25))
                samples.append(dt)
                worst_spike = max(worst_spike, dt)
            results.append(
                ProbeResult(
                    f"reads during {sse_clients} SSE clients",
                    samples,
                    budgets.get("sse_reads"),
                )
            )
            results.append(
                ProbeResult(
                    "max single-read spike under SSE load",
                    [worst_spike],
                    budgets.get("sse_spike"),
                )
            )
            # each stream got the opening status frame
            seen_frames = 0
            for resp in responses:
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        seen_frames += 1
                        break
            print(
                f"SSE: {seen_frames}/{opened} of {sse_clients} clients framed",
                file=sys.stderr,
            )
        finally:
            for s in streams:
                try:
                    s.__exit__(None, None, None)
                except Exception:
                    pass

    # ---- B4: bulk close must not wedge the plane ----------------------------
    if bulk_n:
        bulk_ids: list[str] = []
        for _ in range(bulk_n):
            resp = http.post(
                "/v2/sessions",
                json={
                    "prompt": "Bulk close probe.",
                    "execution": {"provider": "codex"},
                },
                headers=headers,
            )
            if resp.status_code in (200, 201, 202):
                bulk_ids.append(resp.json()["session"]["id"])
        t0 = time.perf_counter()
        threads = []
        for sid in bulk_ids:
            threads.append(
                threading.Thread(
                    target=lambda s=sid: http.post(
                        f"/v2/sessions/{s}/cancel",
                        headers=headers,
                        timeout=120.0,
                    )
                )
            )
        for t in threads:
            t.start()
        # unrelated reads while the bulk close is in flight
        wedge_samples: list[float] = []
        for _ in range(6):
            dt = _timed_read(http, detail_one(bound_ids[-1] if bound_ids else bulk_ids[0]))
            wedge_samples.append(dt)
        for t in threads:
            t.join(timeout=120)
        bulk_s = time.perf_counter() - t0
        results.append(
            ProbeResult(
                f"bulk cancel x{len(bulk_ids)} wall",
                [bulk_s],
                budgets.get("bulk_wall"),
            )
        )
        results.append(
            ProbeResult(
                "detail during bulk cancel",
                wedge_samples,
                budgets.get("bulk_read"),
            )
        )

    return results, probe_ops


# ---------------------------------------------------------------------------
# emulated-mode app wiring
# ---------------------------------------------------------------------------


def _emulated_app(dict_ms: float, sandbox_ms: float, counter: dict[str, int]):
    """The real control plane on loopback with latency-shimmed remote ops."""
    import os
    from datetime import UTC, datetime

    # Platform-only default disables every provider; the gate needs codex.
    os.environ.setdefault("SBX_PROVIDERS", "codex")

    from control.accounts import (
        ModalDictAccountStore,
        PersistentAccountRegistry,
    )
    from control.api_v1.state import (
        InMemoryApiKeyStore,
        InMemoryScheduler,
    )
    from control.app import create_app
    from control.backend import LocalProcessBackend
    from control.ports import Account
    from control.run_store import InMemoryRunStore
    from control.store import InMemoryStore
    from control.tasks import InMemoryTaskStore

    stub_runner = (Path(__file__).resolve().parents[1] / "fakes" / "stub_runner.py").resolve()
    backend = _SlowProxy(LocalProcessBackend(), sandbox_ms, counter)
    app = create_app(
        backend=backend,
        store=_SlowProxy(InMemoryStore(), dict_ms, counter),
        run_store=_SlowProxy(InMemoryRunStore(), dict_ms, counter),
        task_store=_SlowProxy(InMemoryTaskStore(), dict_ms, counter),
        runner_cmd=[sys.executable, str(stub_runner)],
        keepalive_s=0.5,
        max_concurrent=256,
    )
    # The accounts path goes through the real Dict-backed store — a
    # ``modal_dict.items`` full scan inside ``resolve_execution`` (the
    # SOR-271 create-ACK finding) shows up as ``accounts.items`` in the
    # counter and as ~items latency on the dispatch path.
    accounts_dict = _SlowProxy(_HarnessDict(), dict_ms, counter, ns="accounts.")
    accounts_store = ModalDictAccountStore("sbx-accounts")
    accounts_store._dict = accounts_dict
    registry = PersistentAccountRegistry(accounts_store)
    scheduler = InMemoryScheduler(registry)
    keys = InMemoryApiKeyStore()
    app.state.account_registry = registry
    app.state.scheduler = scheduler
    app.state.api_key_store = keys
    registry.put(
        Account(
            id="acct-codex-1",
            provider="codex",
            label="codex account",
            models=(),
            max_concurrent=64,
            secret_name="sbx-test-codex",
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    _key, token = keys.create(label="perf", scopes=("agents",))
    return app, token


def _serve(app: Any) -> tuple[str, Any]:
    import socket

    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base}/v1/me", timeout=0.5)
            break
        except httpx.TransportError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError("control plane did not start")
    return base, (server, thread)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=None, help="deployed plane URL")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-key-file", default=None)
    parser.add_argument("--dict-ms", type=float, default=250.0)
    parser.add_argument("--sandbox-ms", type=float, default=500.0)
    # SOR-271 gate shapes: >=20 warm create samples, 20 SSE clients
    # spread across hubs, 16x bulk cancel.
    parser.add_argument("--sessions", type=int, default=21)
    parser.add_argument("--sse-clients", type=int, default=20)
    parser.add_argument("--bulk", type=int, default=16)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    # Emulated-mode budgets assume ~250ms Dict + ~500ms sandbox ops; a warm
    # ACK performs <= 2 durable Dict ops, so 1.5s tracks the production
    # <1s SLO with headroom. In --base-url mode use the real SLOs.
    live = args.base_url is not None
    budgets = {
        "create": 1.0 if live else 1.5,
        "message": 1.0 if live else 1.5,
        "cancel": 1.0 if live else 1.5,
        "retry": 1.0 if live else 1.5,
        "detail": 1.5 if live else 2.0,
        "list": None,
        "sse_reads": 1.5 if live else 2.0,
        "sse_spike": 5.0,
        "bulk_wall": None,
        "bulk_read": 1.5 if live else 2.0,
        "account_items": 1.0,
    }

    token: str | None = args.api_key
    if args.api_key_file:
        token = Path(args.api_key_file).read_text(encoding="utf-8").strip()

    counter: dict[str, int] = {}
    server_ctx: Any = None
    if live:
        if not token:
            print("FAIL: --base-url requires --api-key/--api-key-file", file=sys.stderr)
            return 2
        base = args.base_url
    else:
        app, token = _emulated_app(args.dict_ms, args.sandbox_ms, counter)
        base, server_ctx = _serve(app)

    headers = {"Authorization": f"Bearer {token}"}
    try:
        results, probe_ops = run_probes(
            base,
            headers,
            n_sessions=args.sessions,
            sse_clients=args.sse_clients,
            bulk_n=args.bulk,
            budgets=budgets,
            op_counter=None if live else counter,
        )
    finally:
        if server_ctx is not None:
            server_ctx[0].should_exit = True
            server_ctx[1].join(timeout=5)

    print("\n=== SOR-268 perf probe results ===")
    ok = True
    report: dict[str, Any] = {"base_url": base, "probes": []}
    for r in results:
        print(r.line())
        ok = ok and r.ok()
        report["probes"].append(
            {
                "name": r.name,
                "n": len(r.samples),
                "p50_ms": round(r.p50 * 1000, 1),
                "p95_ms": round(r.p95 * 1000, 1),
                "max_ms": round(r.worst * 1000, 1),
                "budget_ms": r.budget_ms,
                "ok": r.ok(),
            }
        )
    if not live:
        report["remote_op_counts"] = dict(counter)
        report["remote_ops_per_probe"] = {name: ops for name, ops in probe_ops}
        print(f"\ntotal remote ops: {json.dumps(dict(sorted(counter.items())))}")
    print(f"\nVERDICT: {'PASS' if ok else 'FAIL'}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
