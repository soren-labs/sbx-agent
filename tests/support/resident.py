"""Execution stack on a resident machine reached over a private, real sshd.

Real: PostgreSQL, Job worker, OpenSSH client and server, the resident agent and the
sbx-runtime daemon. Fake (and only for Turn content): the repository's deterministic
OpenCode stand-in (``tests/fakes/official/opencode_cli.py``) pinned through the
operator's ``OPENCODE_BIN`` override; no model or subscription is involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from control.application.live import LiveWorkspace
from control.application.resident import ResidentMachines
from control.domain.identity import Principal
from control.executors.resident import ResidentExecutor

from tests.support.runtime import FAKE_OPENCODE
from tests.support.sshd import PrivateSshd
from tests.support.stack import Stack


class ResidentStack(Stack):
    def __init__(self, db: Any, tmp: Path, sshd: PrivateSshd, *, grace: float = 900.0) -> None:
        super().__init__(db, tmp)
        self.sshd = sshd
        self.state_root = tmp / "remote-state"
        self.resident = self.new_executor(grace)
        self.machines = ResidentMachines(db, prober=self.resident.probe)
        self.live = LiveWorkspace(db, self.connector, self.broker)

    def new_executor(self, grace: float = 900.0) -> ResidentExecutor:
        """A fresh executor, as a restarted control plane process would build it."""
        executor = ResidentExecutor(call_timeout=30, unreachable_grace=grace)
        self.execution.executors["resident"] = executor
        self.connector.resolvers["resident"] = lambda lease: executor.endpoint(
            lease, lambda: self.target_of(lease["resident_machine_id"])
        )
        self.resident = executor
        return executor

    def target_of(self, machine_id: str) -> dict[str, Any]:
        return self.db.read(lambda u: u.get("resident_machines", machine_id))["target"]

    def register(
        self, principal: Principal, name: str = "ubuntu", *, max_leases: int = 4, **target: Any
    ) -> dict[str, Any]:
        spec = self.sshd.target(
            self.state_root, cli_overrides={"OPENCODE_BIN": FAKE_OPENCODE}, **target
        )
        return self.machines.register(
            principal.default_workspace_id, name, spec, max_leases=max_leases
        )

    @staticmethod
    def body(machine_id: str, **extra: Any) -> dict[str, Any]:
        return {
            "harness": {"provider_id": "opencode", "model": "test-model"},
            "executor": {"backend": "resident", "resident_machine_id": machine_id},
            "title": "resident test",
            **extra,
        }

    def lease(self, session_id: str) -> dict[str, Any]:
        return self.db.read(
            lambda u: u.find_one(
                "executor_leases", {"session_id": session_id}, order="generation DESC"
            )
        )

    def shutdown(self) -> None:
        if self.sshd.proc is None:
            self.sshd.start()
        for lease in self.db.read(lambda u: u.find("executor_leases", {})):
            if lease["handle"] and lease["resident_machine_id"]:
                try:
                    self.resident.terminate(
                        lease["handle"],
                        "cleanup",
                        {"target": self.target_of(lease["resident_machine_id"])},
                    )
                except Exception:
                    pass
        self.resident.tunnels.close_all()
