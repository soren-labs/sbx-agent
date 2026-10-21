"""``sbx server machine ...``: the operator registers resident machines for a workspace.

Only the operator (who holds the server config and its database) can register a machine
and fix its SSH target. Registration refuses a target whose host key is not already in
the given known_hosts file (no trust-on-first-use) and, unless ``--no-probe``, one that
does not answer the agent probe with the control plane's own runtime digest.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from control.application.resident import ResidentMachines
from control.domain.errors import DomainError
from control.executors.resident import ResidentExecutor, Target
from control.persistence.database import Database
from control.selfhost.config import ConfigError, ServerSettings

# Reachability status -> B1 diagnostic category (exit codes 3/4/5).
CATEGORY = {
    "unreachable": "reachability",
    "host_key_unverified": "authentication",
    "auth_failed": "authentication",
    "runtime_missing": "config",
    "runtime_mismatch": "config",
}


class MachineError(Exception):
    def __init__(self, category: str, message: str, action: str | None = None, **extra: Any):
        super().__init__(message)
        self.category = category
        self.message = message
        self.action = action
        self.extra = extra


def _database(settings: ServerSettings) -> Database:
    url = settings.values.get("SBX_DATABASE_URL")
    if not url:
        raise ConfigError("SBX_DATABASE_URL is not set", "run `sbx server init` first")
    db = Database(url)
    try:
        db.migrate()
    except Exception as exc:
        raise MachineError(
            "reachability",
            f"cannot reach the server database ({type(exc).__name__})",
            "start the server first: `sbx server start`",
        ) from None
    return db


def _workspace(db: Database, workspace: str | None, owner_email: str | None) -> str:
    def fn(uow: Any) -> str:
        if workspace:
            if uow.get("workspaces", workspace) is None:
                raise MachineError("config", f"workspace {workspace} does not exist")
            return workspace
        user = uow.find_one("users", {"email": (owner_email or "").strip().lower()})
        if user is None:
            raise MachineError("config", f"no user with email {owner_email}")
        row = uow.find_one("workspaces", {"owner_user_id": user["id"], "kind": "personal"})
        if row is None:
            raise MachineError("config", f"{owner_email} has no personal workspace")
        return row["id"]

    return db.read(fn)


def check_local_files(target: Target) -> None:
    key = Path(target.identity_file)
    if not key.is_file():
        raise MachineError("config", f"identity file {key} does not exist")
    if key.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise MachineError(
            "config", f"identity file {key} is readable by others", f"chmod 600 {key}"
        )
    known = Path(target.known_hosts_file)
    if not known.is_file():
        raise MachineError("config", f"known_hosts file {known} does not exist")
    host = target.host if target.port == 22 else f"[{target.host}]:{target.port}"
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        raise MachineError("config", "ssh-keygen is not on PATH")
    found = subprocess.run(
        [keygen, "-F", host, "-f", str(known)], capture_output=True, text=True, check=False
    )
    if found.returncode != 0 or not found.stdout.strip():
        raise MachineError(
            "authentication",
            f"{known} has no host key for {host}",
            "add the machine's verified host key line (e.g. copied from a known_hosts you "
            "already trust); SBX never accepts an unknown host key",
        )


def _target(args: Any) -> Target:
    overrides = {}
    for item in args.cli_override or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise MachineError("config", f"--cli-override {item} must be NAME=/abs/path")
        overrides[key] = value
    try:
        return Target.from_dict(
            {
                "host": args.host,
                "port": args.port,
                "user": args.user,
                "identity_file": os.path.abspath(os.path.expanduser(args.identity_file)),
                "known_hosts_file": os.path.abspath(os.path.expanduser(args.known_hosts_file)),
                "remote_python": args.remote_python,
                "remote_root": args.remote_root,
                "state_root": args.state_root,
                "remote_path": args.remote_path,
                "cli_overrides": overrides,
                "connect_timeout": args.connect_timeout,
            }
        )
    except DomainError as exc:
        raise MachineError("config", exc.message) from None


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    target = row["target"]
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "state": row["state"],
        "max_leases": row["max_leases"],
        "ssh": f"{target['user']}@{target['host']}:{target['port']}",
        "remote_root": target["remote_root"],
    }


def _require_reachable(probe: dict[str, Any]) -> None:
    if probe["status"] != "reachable":
        raise MachineError(
            CATEGORY.get(probe["status"], "reachability"),
            f"machine probe: {probe['status']}: {probe.get('reason')}",
            "fix SSH access or the remote checkout, or pass --no-probe to register anyway",
            reachability=probe,
        )


def run(args: Any, settings: ServerSettings) -> dict[str, Any]:
    action = args.machine_action
    db = _database(settings)
    machines = ResidentMachines(db)
    if action == "add":
        if not (args.workspace or args.owner_email):
            raise MachineError("config", "pass --workspace or --owner-email")
        target = _target(args)
        check_local_files(target)
        workspace_id = _workspace(db, args.workspace, args.owner_email)
        probe = None
        if not args.no_probe:
            probe = ResidentExecutor().probe(target.to_dict(), fresh=True)
            _require_reachable(probe)
        try:
            row = machines.register(
                workspace_id, args.name, target.to_dict(), max_leases=args.max_leases
            )
        except DomainError as exc:
            raise MachineError("config", exc.message) from None
        return {"ok": True, "machine": _summary(row), "reachability": probe}
    if action == "list":
        return {"ok": True, "machines": [_summary(r) for r in machines.operator_list()]}
    try:
        row = machines.find(args.machine, args.workspace)
    except DomainError as exc:
        raise MachineError("config", exc.message) from None
    if action == "probe":
        probe = ResidentExecutor().probe(row["target"], fresh=True)
        ok = probe["status"] == "reachable"
        out = {"ok": ok, "machine": _summary(row), "reachability": probe}
        if not ok:
            raise MachineError(
                CATEGORY.get(probe["status"], "reachability"),
                f"machine probe: {probe['status']}: {probe.get('reason')}",
                **out,
            )
        return out
    if action == "remove":
        try:
            removed = machines.remove(row["id"])
        except DomainError as exc:
            raise MachineError("config", exc.message, details=exc.details) from None
        return {"ok": True, "machine": _summary(removed)}
    raise MachineError("config", f"unknown action {action}")
