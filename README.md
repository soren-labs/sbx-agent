# SBX Browser

SBX runs durable coding Sessions through the **official OpenCode CLI**, on compute provisioned with the user's **Modal connection**. PostgreSQL stores the authoritative typed state and append-only Session events. Replaceable Executors host `sbx-runtime`; they do not own Session identity. Immutable ChangeSets feed durable GitHub Delivery operations and independent Delegation child Sessions.

This is the unified 1.0 architecture defined by [RFC #167](https://github.com/soren-labs/sbx-browser/pull/167), frozen at `bf8cbe065da2f4c6bbca3781b4dce90cc31d39d3`. Start with [all thirteen canonical RFC documents](docs/architecture/unified/README.md), the [generated business API](docs/specs/unified/openapi.yaml), and [implementation evidence](docs/implementation/gpt-benchmark-progress.md).

The minimum user setup is email/password, OpenCode Zen API key, Modal Token ID/Secret, and a GitHub manual token. Codex/ChatGPT login and OAuth are unnecessary. Other Harnesses are disabled until their installed versions pass the release matrix. OpenCode 1.18.29 is experimental: native resume, JSON events and checkpoint restore are supported; steer, interactive approvals, arbitrary effort settings and native MCP transport are not advertised.

## Development

Python 3.12+, uv, Node 22.12+, and a disposable PostgreSQL 17 database are required. Provider credentials are unnecessary for deterministic tests.

```sh
uv sync
npm --prefix console ci
npm --prefix docs-site ci
python scripts/isolated_checks.py make lint
SBX_TEST_PG_DSN=postgresql://postgres@localhost:5432/sbx_test python scripts/isolated_checks.py make test
python scripts/isolated_checks.py make spec-check
python scripts/isolated_checks.py make test-e2e
python scripts/isolated_checks.py make console-build
python scripts/isolated_checks.py make docs-check
```

Each integration test migrates a fresh PostgreSQL schema and deletes it afterward. Without `SBX_TEST_PG_DSN`, PostgreSQL tests skip explicitly. HOME/XDG are isolated and ambient provider credentials are stripped before test collection. The real acceptance driver is separately opt-in and never part of `make test`.

## Run the product

Build the Console, create a private durable state directory and supply the operator's explicit database DSN:

```sh
uv run python -m control.serve --dsn postgresql://postgres@localhost:5432/sbx \
  --state-dir /var/lib/sbx --console-dir console/dist
```

Use TLS before exposing the product. Cookies are Secure by default; `--insecure-local-cookie` is only for loopback development. `deploy/compose.yaml` provides PostgreSQL plus the product image, without exposing the database. The state volume holds the encrypted-object store and an operator master key with mode 600. Back up **both** PostgreSQL and this state; losing the master key makes stored credentials unrecoverable. No provider credential belongs in server configuration.

New email registrations require verification through an operator email transport. Configure `--email-command /path/to/send-email`: its protected stdin receives an email message containing recipient, purpose and one-use code. The command must deliver the notice without logging it. Verification and password recovery forms accept this code; without a configured transport public registration fails explicitly. The trusted operator may provision an already verified account through `Identity.register(..., verified=True)`; this is not an HTTP verification bypass. The benchmark uses this operator path for its supplied test identity. Password changes revoke login sessions, API keys and Session tool grants.

The Console offers Projects, Sessions, Conversation, Activity, Changes/Delivery, Files, real lease-local PTY terminals, declared Services, child Sessions, Connections and Settings. Secrets are write-only and fields clear after submission. Zen models come from the selected validated Connection, preferring a free model. Session setup uses only selected stored connections; there is no ambient fallback.

## Operational semantics

Accepted Messages and Jobs commit with their events/outbox before a receipt is returned. Retries must reuse `Idempotency-Key` and the same body. Reads never launch or settle work. Jobs use one durable claim/fencing protocol; expired workers cannot commit. Runtime operations persist acceptance before subprocess launch, spool bounded events, and acknowledge only committed contiguous evidence.

`GET /api/workspaces/{workspace_id}/diagnostics` provides a bounded read-only snapshot of queue/claims, effect IDs, resource fences, leases, capacity and notification outbox. It contains safe IDs/reasons, never backend handles or credential material.

Checkpoint only idle Worktrees after stopping terminals and services. Checkpoints preserve files and supported OpenCode native SQLite state, not memory or PTYs. Release requires confirmed teardown; interrupted/unknown execution remains quarantined until isolation and explicit recovery acknowledgement. A linked continuation creates a fresh native Session with an explicit summary and optional immutable input ChangeSet.

Delivery materializes exact Git objects using the user's GitHub token, creates a deterministic new branch and draft PR, and reconciles lost responses. Merge separately checks exact subject/head, independent typed results and current policy, then uses GitHub's head-CAS. Strict atomic base stability is unsupported and fails closed. Child Sessions have no shipping authority. Disconnect/replacement is rejected while live or unresolved dependent compute needs its credential for teardown.

Local Executor is opt-in development infrastructure and provides no tenant isolation. Modal starts only the runtime daemon; all coding uses official CLI protocol frames. Runtime/CLI versions and source fingerprints are recorded on ExecutorLease binding.

Set `--public-origin https://console.example.com` to enforce browser origins and enable the execution-scoped shell tool gateway. Native MCP is not advertised. A separate `--preview-origin https://preview.example.com` must use a different hostname; its application proxies only declared lease-local service ports and strips Console credentials. Preview currently supports bounded HTTP rather than WebSocket apps. Neither origin is required for loopback deterministic tests.

## Cutover

This release deliberately removes Task/Agent/Run, V1/V2, hosted review/revision/workflow, legacy SDK/CLI and Console state engines. There are no deployed compatibility facades or dual writes. Historical docs under `docs/archive/legacy` are inert references. Production migration is not automatic: stop old writers, inventory/backup old data, explicitly map ownership, rehearse import conflicts/losses, and validate a fresh unified cohort before routing traffic. No production database is modified by this benchmark.
