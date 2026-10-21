"""B2 acceptance driver: a control plane exercises a real resident Linux machine.

Started by ``b2_resident_mac.sh`` after it initialised an isolated control plane, signed a
user in and registered the machines. Every product action goes through the SBX API (SDK);
``ssh <alias>`` is used only by this harness to collect evidence on the machine (process
lists, file growth) and never by the product. Prints one PASS/FAIL line per check and
writes a JSON evidence file without secrets. Exit 0 only when every check passed.

Optional ``--fake-turn``: Turns on a separately registered machine whose OpenCode CLI is the
repository's deterministic FAKE stand-in. Those checks are labelled FAKE; they prove Turn
plumbing on the resident runtime, not any subscription (that is B3).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from typing import Any

from sbx.config import ClientConfig
from sbx.sdk import SBXClient, SBXError


class Checks:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []
        self.evidence: dict[str, Any] = {}

    def check(self, name: str, ok: bool, detail: Any = None) -> bool:
        self.results.append({"check": name, "ok": bool(ok), "detail": detail})
        print(
            f"{'PASS' if ok else 'FAIL'} {name}" + (f" :: {detail}" if detail else ""), flush=True
        )
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(r["ok"] for r in self.results)


class Harness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        cfg = ClientConfig.load()
        self.client = SBXClient(cfg.base_url, cfg.api_key, timeout=120)
        self.c = Checks()
        self.created: list[str] = []

    # ------------------------------------------------------------------ helpers
    def remote(self, command: str, timeout: float = 30) -> str:
        """Evidence on the machine through the operator's own SSH alias (harness only)."""
        config = os.environ.get("SBX_B2_SSH_CONFIG")
        out = subprocess.run(
            ["ssh", *(["-F", config] if config else []), "-o", "BatchMode=yes"]
            + [self.args.alias, command],
            capture_output=True,
            text=True,
            timeout=timeout,
            # The harness's own SSH config lives in the operator's real HOME.
            env={**os.environ, "HOME": os.environ.get("SBX_B2_ORIG_HOME", os.environ["HOME"])},
        )
        return out.stdout.strip()

    def remote_pids(self, pattern: str) -> list[str]:
        # The bracket keeps pgrep from matching this ssh command's own shell.
        safe = "[" + pattern[0] + "]" + pattern[1:]
        return [p for p in self.remote(f"pgrep -f {shlex.quote(safe)} || true").split() if p]

    def shell(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=300)

    def machine(self, name: str) -> dict[str, Any]:
        for item in self.client.resident_machines.list(probe=False):
            if item["name"] == name:
                return item
        raise SystemExit(f"machine {name} is not registered")

    def session(self, machine_id: str, key: str | None = None, **extra: Any) -> dict[str, Any]:
        created = self._create(machine_id, key, **extra)
        self.created.append(created["session_id"])
        return created

    def _create(self, machine_id: str, key: str | None, **extra: Any) -> dict[str, Any]:
        return self.client.sessions.create(
            idempotency_key=key,
            harness={"provider_id": "opencode"},
            executor={"backend": "resident", "resident_machine_id": machine_id},
            connections={"inference": self.args.inference_connection},
            title="b2 acceptance",
            **extra,
        )

    def wait(self, what: str, fn: Any, timeout: float = 120, poll: float = 1.0) -> Any:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = fn()
            except SBXError as exc:
                last = exc
            if last and not isinstance(last, SBXError):
                return last
            time.sleep(poll)
        raise TimeoutError(f"{what}: {last}")

    def ready(self, session_id: str, timeout: float = 180) -> dict[str, Any]:
        def check() -> dict[str, Any] | None:
            view = self.client.sessions.executor(session_id)
            lease = view["leases"][0] if view["leases"] else None
            if lease and lease["state"] == "ready" and view["worktree"]["availability"] == "live":
                return view
            return None

        return self.wait(f"executor ready for {session_id}", check, timeout)

    def type_and_read(
        self, session_id: str, terminal_id: str, line: str, needle: str, timeout: float = 30
    ) -> str:
        self.client.terminals.input(session_id, terminal_id, line + "\n")

        def check() -> str | None:
            text = self.client.terminals.output(session_id, terminal_id, 0)["data"]
            return text if needle in text else None

        return self.wait(f"terminal output {needle}", check, timeout, 0.5)

    def lease_pattern(self, lease_id: str) -> str:
        """The lease's sbx-runtime daemon (not its helper processes)."""
        return f"runtime.daemon.main --state-dir {self.args.remote_state}/leases/{lease_id}/state"

    # -------------------------------------------------------------------- steps
    def discovery(self) -> None:
        raw = json.dumps(self.client.resident_machines.list(probe=True))
        items = json.loads(raw)
        by_name = {m["name"]: m for m in items}
        for name in ("ubuntu-dev", "ubuntu-link"):
            m = by_name.get(name) or {}
            r = m.get("reachability") or {}
            self.c.check(
                f"discovery: {name} reachable with the same runtime version",
                r.get("status") == "reachable" and (r.get("runtime") or {}).get("digest_match"),
                {"status": r.get("status"), "runtime": r.get("runtime")},
            )
        off = by_name.get("ubuntu-offline", {}).get("reachability", {})
        self.c.check(
            "discovery: offline machine reported unreachable", off.get("status") == "unreachable"
        )
        leaked = [s for s in self.args.secret_shaped if s and s in raw]
        self.c.check("discovery: no SSH target detail in API output", not leaked, leaked or None)
        self.c.evidence["machines"] = items

    def task(self) -> dict[str, Any]:
        dev = self.machine("ubuntu-dev")
        key = "b2-create-" + secrets.token_hex(4)
        first = self.session(dev["id"], key)
        again = self.session(dev["id"], key)
        self.c.check(
            "idempotency: repeated create with one key is one Session",
            first["session_id"] == again["session_id"],
            first["session_id"],
        )
        sid = first["session_id"]
        a1 = self.client.sessions.activate(sid)
        a2 = self.client.sessions.activate(sid)
        self.c.check(
            "idempotency: repeated activation is one lease", a1["lease_id"] == a2["lease_id"]
        )
        view = self.ready(sid)
        lease = view["leases"][0]
        self.client.files.write(sid, "task.txt", "b2 payload from the control plane\n")
        term = self.client.terminals.create(sid)["terminal_id"]
        out = self.type_and_read(
            sid,
            term,
            "cat task.txt; echo HOST=$(uname -n); echo PWD=$(pwd); "
            "echo B2-DONE-$((6*7)) | tee result.txt",
            "B2-DONE-42",
        )
        host = self.remote("uname -n")
        pwd = next((x[4:] for x in out.splitlines() if x.startswith("PWD=")), "")
        result = self.client.files.read(sid, "result.txt")["content"].strip()
        self.c.check(
            "task: file + command ran on the resident machine",
            "b2 payload" in out and f"HOST={host}" in out and result == "B2-DONE-42",
            {"host": host, "pwd": pwd},
        )
        self.c.check(
            "task: worktree is inside this lease's own directory",
            pwd.startswith(f"{self.args.remote_state}/leases/{lease['id']}/"),
            pwd,
        )
        pids = self.remote_pids(self.lease_pattern(lease["id"]))
        self.c.check("idempotency: exactly one runtime process for the lease", len(pids) == 1, pids)
        self.c.evidence["task"] = {
            "session_id": sid,
            "lease_id": lease["id"],
            "runtime_epoch": lease["runtime_epoch"],
            "image_digest": lease["image_digest"],
            "remote_pid": pids,
            "pwd": pwd,
        }
        return {"sid": sid, "lease": lease, "terminal": term, "pwd": pwd, "pids": pids}

    def disconnect(self, t: dict[str, Any]) -> None:
        sid, term, pwd = t["sid"], t["terminal"], t["pwd"]
        self.client.terminals.input(
            sid, term, "for i in $(seq 1 60); do echo tick-$i >> ticks.txt; sleep 1; done &\n"
        )
        time.sleep(3)
        stop = self.shell(*self.args.sbx, "server", "stop")
        self.c.check("disconnect: control plane stopped", stop.returncode == 0, stop.stdout[-200:])
        before = self.remote(f"wc -l < {shlex.quote(pwd)}/ticks.txt")
        time.sleep(12)
        during = self.remote(f"wc -l < {shlex.quote(pwd)}/ticks.txt")
        alive = self.remote_pids(self.lease_pattern(t["lease"]["id"]))
        self.c.check(
            "disconnect: runtime kept working while the control plane was down",
            alive == t["pids"] and int(during or 0) > int(before or 0),
            {"ticks_at_stop": before, "ticks_12s_later": during, "pid": alive},
        )
        start = self.shell(*self.args.sbx, "server", "start")
        self.c.check("reconnect: control plane started", start.returncode == 0, start.stdout[-200:])
        view = self.client.sessions.executor(sid)
        lease = view["leases"][0]
        same = (
            lease["id"] == t["lease"]["id"]
            and lease["runtime_epoch"] == t["lease"]["runtime_epoch"]
            and lease["state"] == "ready"
        )
        self.c.check("reconnect: same Session, lease and runtime epoch", same, lease)
        ticks = self.client.files.read(sid, "ticks.txt")["content"].count("tick-")
        output = self.client.terminals.output(sid, term, 0)["data"]
        self.c.check(
            "reconnect: output written during the outage is readable through SBX",
            ticks >= int(during or 0) and "B2-DONE-42" in output,
            {"ticks_now": ticks},
        )

    def cancel(self, t: dict[str, Any]) -> None:
        sid = t["sid"]
        marker = "b2-cancel-" + secrets.token_hex(4)
        term = self.client.terminals.create(sid)["terminal_id"]
        # exec -a names the process, so the machine-side check finds exactly this command.
        self.client.terminals.input(sid, term, f"( exec -a {marker} sleep 600 )\n")
        running = self.wait("long command", lambda: self.remote_pids(marker), 20)
        self.client.terminals.close(sid, term)
        gone = self.wait("command stopped", lambda: not self.remote_pids(marker) or None, 20)
        self.c.check("cancel: closing the terminal stopped the long command", bool(gone), running)

    def two_jobs(self, t: dict[str, Any]) -> dict[str, Any]:
        dev = self.machine("ubuntu-dev")
        sid = self.session(dev["id"])["session_id"]
        self.client.sessions.activate(sid)
        lease = self.ready(sid)["leases"][0]
        term = self.client.terminals.create(sid)["terminal_id"]
        out = self.type_and_read(sid, term, "echo PWD2=$(pwd)", "PWD2=")
        pwd = next(x[5:] for x in out.splitlines() if x.startswith("PWD2="))
        files = [f["path"] for f in self.client.files.list(sid).get("items", [])]
        self.c.check(
            "two jobs: separate leases, runtimes and worktrees",
            lease["id"] != t["lease"]["id"] and pwd != t["pwd"] and "task.txt" not in files,
            {"first": t["pwd"], "second": pwd},
        )
        return {"sid": sid, "lease": lease}

    def offline(self) -> dict[str, Any]:
        link = self.machine("ubuntu-link")
        sid = self.session(link["id"])["session_id"]
        self.client.sessions.activate(sid)
        lease = self.ready(sid)["leases"][0]
        term = self.client.terminals.create(sid)["terminal_id"]
        self.client.terminals.input(
            sid, term, "for i in $(seq 1 90); do echo t-$i >> link.txt; sleep 1; done &\n"
        )
        time.sleep(2)
        self.shell(self.args.link, "down")
        time.sleep(6)  # past the 5 s probe cache
        try:
            self.client.files.read(sid, "link.txt")
            code = "read_succeeded"
        except SBXError as exc:
            code = exc.code
        self.c.check(
            "offline: live read reports the machine unavailable (not a result)",
            code in ("executor_unavailable", "outcome_unknown"),
            code,
        )
        view = self.client.sessions.executor(sid)
        reach = view["resident_machine"]["reachability"]["status"]
        self.c.check("offline: executor view shows the machine unreachable", reach == "unreachable")
        probe = self.client.resident_machines.get(link["id"])["reachability"]["status"]
        self.c.check("offline: machine probe says unreachable", probe == "unreachable")
        alive = self.remote_pids(self.lease_pattern(lease["id"]))
        self.c.check("offline: the machine's runtime keeps running", len(alive) == 1, alive)
        time.sleep(5)
        self.shell(self.args.link, "up")
        content = self.wait(
            "read after the link returned",
            lambda: self.client.files.read(sid, "link.txt")["content"],
            60,
        )
        after = self.client.sessions.executor(sid)["leases"][0]
        self.c.check(
            "offline: link restored -> same lease and epoch, work continued",
            after["id"] == lease["id"]
            and after["runtime_epoch"] == lease["runtime_epoch"]
            and content.count("t-") >= 10,
            {"lines": content.count("t-")},
        )
        return {"sid": sid, "lease": lease}

    def offline_from_start(self) -> None:
        off = self.machine("ubuntu-offline")
        created = self.session(off["id"], message={"content": "never runs"})
        sid, turn_id = created["session_id"], created["turn_id"]
        time.sleep(15)
        turn = self.client.turns.get(turn_id)
        self.c.check(
            "offline machine: Turn is not reported as succeeded",
            turn["state"] in ("queued", "preparing"),
            {"state": turn["state"], "reason": turn.get("reason")},
        )
        self.client.turns.cancel(turn_id)
        final = self.wait(
            "cancelled",
            lambda: (t := self.client.turns.get(turn_id))["state"] in ("cancelled", "failed") and t,
            60,
        )
        events = [e["type"] for e in self.client.sessions.events(sid)]
        self.c.check(
            "offline machine: cancel ends it without any success event",
            final["state"] == "cancelled" and "turn.succeeded" not in events,
            final["state"],
        )

    def fake_turns(self) -> None:
        fake = self.machine("ubuntu-fake")
        created = self.session(fake["id"], message={"content": "hello [write:f.txt=fake]"})
        sid, t1 = created["session_id"], created["turn_id"]
        done = self.client.turns.wait(t1, deadline=180)
        self.c.check("FAKE turn: succeeded on the resident runtime", done["state"] == "succeeded")
        t2 = self.client.sessions.send(sid, "slow [slow:25] [write:g.txt=after]")["turn_id"]
        self.wait("running", lambda: self.client.turns.get(t2)["state"] == "running", 60)
        self.shell(*self.args.sbx, "server", "stop")
        time.sleep(8)
        self.shell(*self.args.sbx, "server", "start")
        done = self.client.turns.wait(t2, deadline=180)
        self.c.check(
            "FAKE turn: survives a control-plane restart and completes",
            done["state"] == "succeeded",
        )
        t3 = self.client.sessions.send(sid, "block [hang]")["turn_id"]
        self.wait("running", lambda: self.client.turns.get(t3)["state"] == "running", 60)
        self.client.turns.cancel(t3)
        done = self.client.turns.wait(t3, deadline=120)
        self.c.check("FAKE turn: cancellation confirmed", done["state"] == "cancelled")
        self.client.sessions.release(sid)

    def release(self, sessions: list[dict[str, Any]]) -> None:
        for s in sessions:
            self.client.sessions.release(s["sid"])
        for s in sessions:

            def released(s: dict[str, Any] = s) -> bool:
                leases = self.client.sessions.executor(s["sid"])["leases"]
                return bool(leases) and leases[0]["state"] == "released"

            ok = bool(self.wait("released", released, 180))
            pids = self.remote_pids(self.lease_pattern(s["lease"]["id"]))
            self.c.check(
                f"release: {s['lease']['id']} released, no process left", ok and not pids, pids
            )

    def run(self) -> int:
        try:
            return self._run()
        finally:
            self.release_all()

    def release_all(self) -> None:
        """Whatever happened, leave no runtime behind: release every Session we created."""
        for sid in dict.fromkeys(self.created):
            try:
                leases = self.client.sessions.executor(sid)["leases"]
                if leases and leases[0]["state"] in ("allocating", "ready", "quiescing"):
                    self.client.sessions.release(sid)
            except SBXError:
                continue
        deadline = time.monotonic() + 180
        for sid in dict.fromkeys(self.created):
            while time.monotonic() < deadline:
                try:
                    leases = self.client.sessions.executor(sid)["leases"]
                except SBXError:
                    break
                if not leases or leases[0]["state"] in ("released", "lost"):
                    break
                time.sleep(1)

    def fixture(self) -> bool:
        """The FAKE validation-only inference Connection must be ready, by SBX's own check."""
        view = self.client.connections.wait_health(self.args.inference_connection)
        validation = view.get("validation") or {}
        self.c.evidence["fixture_connection"] = {
            "id": view["id"],
            "health": view["health"],
            "health_reason": view.get("health_reason"),
            "validation": validation.get("status"),
            "base_url_kind": "FAKE validation-only endpoint on 127.0.0.1",
        }
        return self.c.check(
            "fixture: FAKE inference Connection validated ready by SBX (no model is called)",
            view["health"] == "ready",
            self.c.evidence["fixture_connection"],
        )

    def _run(self) -> int:
        if not self.fixture():
            print("SUMMARY fixture not ready; scenario not run", flush=True)
            return 1
        self.discovery()
        first = self.task()
        self.disconnect(first)
        self.cancel(first)
        second = self.two_jobs(first)
        link = self.offline()
        self.offline_from_start()
        if self.args.fake_turn:
            self.fake_turns()
        self.release([first, second, link])
        self.c.evidence["results"] = self.c.results
        with open(self.args.evidence, "w") as fh:
            json.dump(self.c.evidence, fh, indent=2, default=str)
        passed = sum(r["ok"] for r in self.c.results)
        print(
            f"SUMMARY {passed}/{len(self.c.results)} checks passed; evidence {self.args.evidence}"
        )
        return 0 if self.c.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alias", required=True, help="harness-only SSH alias for evidence")
    parser.add_argument("--remote-state", required=True, help="absolute remote state root")
    parser.add_argument("--link", required=True, help="script taking up|down")
    parser.add_argument("--sbx", nargs="+", required=True, help="argv prefix of the sbx CLI")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--inference-connection", required=True)
    parser.add_argument("--secret-shaped", nargs="*", default=[])
    parser.add_argument("--fake-turn", action="store_true")
    return Harness(parser.parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())
