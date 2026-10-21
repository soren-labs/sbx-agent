# B2 acceptance record: resident Linux machine execution

Date: 2026-10-21. Package: B2 (+ B5 consistency for the touched surfaces). Implementer: Ubuntu
Claude Code (Opus 5.5). Branch `feat/oss-b2-resident-20261021`, based on `1057bf2`
(`integration/oss-b1-20261021`). Status: **implemented, Ubuntu-verified; independent Mac→Ubuntu
acceptance by Mac Codex pending** (recipe below). No production resource, Modal account,
model provider or subscription was used.

## Completed behaviour

| Need | Delivered |
| --- | --- |
| A resident machine is a real execution resource | Executor backend `resident`: the official sbx-runtime daemon on the machine, same runtime protocol v1, lease/fence/operation identities and PostgreSQL authority as Local/Modal. |
| Registration owned by a workspace, no caller-chosen SSH | Operator-only `sbx server machine add/list/probe/remove`; `resident_machines` row per workspace; SSH target (host, port, user, key/known_hosts paths, remote checkout) never returned by or settable through the API. No ssh_config alias is read (`-F none`), no TOFU (`StrictHostKeyChecking=yes`, registration refuses a known_hosts without the host's key). |
| Discover, select, see reachability/availability | `GET /api/workspaces/{id}/resident-machines`, `GET /api/resident-machines/{id}`: live read-only probe (status, runtime digest match, CLIs on PATH) and capacity; `executor.resident_machine_id` on Session create (explicit, workspace-scoped). |
| Durable tasks, status/output/results, cancel | Sessions/Turns/files/terminals unchanged on top; executor view adds machine reachability, lease `observed_status` and `runtime_epoch`. |
| Disconnect/reconnect, same identity | Runtime is detached from SSH (setsid, own session); SSH forwards are re-created per call (`resident://` endpoints), so a restarted control plane or a restored link reaches the same daemon and epoch. Reconcile tolerates unreachability for 900 s (`executor.unreachable`/`executor.reachable`) before the existing `outcome_unknown` rule. |
| Retry adopts, never duplicates | Agent allocation is idempotent per operation ID under `flock`, with an intent record before spawn and adoption by lease state dir if the agent died after spawning. |
| Unknown is never success/absence | SSH failures raise retryable `executor_unavailable`; only an agent holding the operation lock reports absence. Lost-but-quarantined leases keep occupying machine capacity until termination is confirmed. |
| Exact process cleanup | Ownership = pid + kernel start time + boot id + lease state dir; signals only through re-verified pidfds; descendants, and when the daemon died, processes of the lease's worktree/state started after it. |
| Separate workspaces, no ambient secrets | Per-lease `work/`, `home/`, `state/` (0700); allow-listed daemon env (no SSH user HOME, shell env or developer CLI logins); ssh child gets no `SBX_*` secrets. |
| Version honesty | Runtime source digest of the machine must equal the control plane's (`runtime_mismatch` / `runtime_incompatible`). |

Contract: `docs/specs/unified/resident-machines.md`; guide: docs-site `guides/resident-machines`.
Migration `0009_resident_machines.sql`; OpenAPI regenerated.

## Verification on Ubuntu (real SSH, real runtime)

Automated (private unprivileged sshd on 127.0.0.1 with its own host key; real ssh client, agent,
daemon, PostgreSQL; Turn content from the **labelled fake** OpenCode CLI only):

* `tests/integration/execution/test_resident_execution.py` (17): discovery scoping/no target leak,
  explicit machine required, isolated dirs + allow-listed env, file + terminal task, allocation
  retry adoption, control-plane restart → same lease/epoch, link down → `executor.unreachable`,
  Turn still running, runtime alive → link up → same epoch, succeeded; grace expiry →
  `outcome_unknown` + quarantine until confirmed; cancel + release stop exactly the owned
  processes (bystander survives); recycled-pid record never signalled; capacity; quarantined loss
  keeps capacity until confirmed (review regression); offline machine never succeeds; unknown host
  key refused; version mismatch refused; SSH forwards die with a SIGKILLed owner (regression).
* `tests/integration/api/test_resident_machines_api.py`, `tests/integration/selfhost/test_machine_cli.py`,
  `tests/unit/executors/test_resident_transport.py` (strict ssh argv, target validation, failure
  categories without host/path text, agent forced-command verbs, digest parity).
* Acceptance script self-test on Ubuntu (same script as Mac, private sshd alias, link forward):
  see the results file named in the coordinator handoff. **Not** cross-machine evidence.

## Mac → Ubuntu acceptance (to be run by Mac Codex)

`scripts/acceptance/b2_resident_mac.sh [--fake-turn]` from the Mac checkout at the B2 commit,
with the Ubuntu worktree at the same commit. It builds an isolated control plane, registers
`ubuntu-dev` (explicit params derived once from `ssh -G ubuntu-dev`, host key copied from the
Mac's known_hosts), a link-forwarded `ubuntu-link` for network cuts and a closed-port
`ubuntu-offline`, and runs `b2_resident_driver.py`. A FAKE validation-only inference endpoint on
127.0.0.1 (`fake_inference.py`) satisfies Session creation's Connection requirement; no model is
called. Results: _pending Mac run_.

## Boundaries

* Linux machines only (needs `/proc`, pidfds); logind must not kill user processes on logout.
* Not a multi-tenant sandbox: runtimes run as the SSH user (documented trust boundary).
* Turn-level checks on the machine use the deterministic fake CLI; real Codex/Claude
  subscription Turns, follow-up and cancellation on a resident machine are B3.
* Console UI unchanged (paused); new event types have no Console labels yet.
* Lease directories are retained on the machine (no automatic pruning).
* An unreachable-at-allocation machine keeps a Turn `preparing` for the existing dispatch retry
  budget (20 attempts with backoff) unless cancelled.
