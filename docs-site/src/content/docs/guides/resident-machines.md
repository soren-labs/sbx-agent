---
title: Resident machines
description: Run Sessions on your own Linux workstation or VPS over SSH - operator registration, strict host keys, reachability, disconnects and cleanup.
---

A **resident machine** is a Linux workstation or VPS you already own. The operator of a
self-hosted SBX server registers it for one workspace; Sessions in that workspace can then
run there with executor backend `resident`. The machine keeps working when the control plane
or the network goes away, and SBX reconnects to the same work afterwards.

- SBX reaches the machine only over OpenSSH with parameters the operator fixed: host, port,
  user, key file and a known_hosts file that already holds the machine's verified host key.
  SBX never trusts a host key on first use and never reads your `~/.ssh/config`.
- Nobody can register a machine or change its SSH details through the API. Workspace members
  see its name, capacity and live reachability only.
- Each Session gets its own runtime process, worktree and home directory on the machine. Your
  developer login (`~/.claude`, `~/.codex`, shell environment) is not passed to Sessions.
- It is not a multi-tenant sandbox: everything runs as the SSH user. Register a machine only for
  a workspace you trust with that account, ideally a dedicated Unix user.

## Prepare the machine

1. Linux (kernel 5.3 or newer), Python 3.12, and an SBX checkout **at the same version** as
   the server. SBX compares the runtime source digest of both sides and refuses a mismatch.
2. An SSH key the server can use, accepted by the machine's account.
3. logind must not kill user processes on logout (`KillUserProcesses=no`, the Ubuntu
   default), so runtimes survive the SSH session that started them.
4. The official CLIs you want to use on the PATH you will register (`--remote-path`).

## Register it (operator)

```bash
sbx server init --executors local,resident   # or add resident to SBX_EXECUTORS
sbx server start
sbx server machine add ubuntu-dev --owner-email you@example.com \
  --host 192.0.2.10 --user dev \
  --identity-file ~/.ssh/id_ed25519 --known-hosts-file ~/.ssh/sbx_known_hosts \
  --remote-python /home/dev/sbx/.venv/bin/python --remote-root /home/dev/sbx \
  --remote-path /home/dev/.local/bin:/usr/local/bin:/usr/bin:/bin
```

The command probes the machine first and refuses an unknown host key, a refused key, a missing
checkout or a different SBX version (exit codes 5, 5, 3, 3; 4 when unreachable). `sbx server
machine list`, `probe` and `remove` manage registrations; removal is refused while a Session
runtime may still run there.

## Use it

```bash
sbx machines list                       # reachability, version match, capacity
sbx sessions create --machine ubuntu-dev --message "..."
sbx sessions executor SESSION_ID        # lease, runtime epoch, machine reachability
sbx sessions release SESSION_ID         # checkpoint, then stop exactly that runtime
```

With the API, pass `"executor": {"backend": "resident", "resident_machine_id": "rmach_…"}` when
creating a Session; there is no default machine.

## When the connection drops

- If the control plane stops or the link drops, work on the machine continues. When SBX can
  reach it again it reconnects to the same runtime (same Session, lease and runtime epoch) and
  reads what was produced meanwhile.
- While unreachable, a running Turn stays running and the Session shows
  `executor.unreachable`; files and terminals answer `executor_unavailable`. Nothing is
  reported as finished. After 15 minutes without contact the Turn ends as `interrupted` with
  `outcome_unknown`, and SBX keeps trying to confirm the runtime stopped before it frees the
  machine's slot.
- A machine that cannot be reached when a Turn starts keeps the Turn `preparing` while SBX
  retries; cancel it at any time.

The full contract is in the repository's `docs/specs/unified/resident-machines.md`.
