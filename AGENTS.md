# AGENTS.md

This file defines the repository-wide working rules for coding and review agents.
The task prompt is the source of scope and intent. Do not depend on an external issue
tracker to discover requirements.

## 0. Core principles

1. Read the task prompt, this file, and the relevant architecture/specification documents before editing.
2. Change only the files and modules needed to complete the requested behavior. Cross-cutting work is allowed when the task genuinely requires it, but unrelated cleanup and opportunistic refactors should be avoided.
3. Preserve repository boundaries and existing contracts unless the task explicitly requires a contract change.
4. Update tests when behavior changes or a regression needs to be prevented. Update specifications and user documentation when externally visible behavior, routes, commands, configuration, or supported workflows change.
5. Before opening a PR, run the checks relevant to the touched areas. At minimum, backend changes should run `make lint` and `make test`; frontend changes should also run `make console-check`; documentation changes should run `make docs-check` when applicable.
6. Never place credentials, tokens, passwords, production secrets, or real account data in source, fixtures, logs, PRs, or documentation. Secret-shaped fixture values must use `REDACTED`.
7. Development and tests must use isolated HOME/XDG state and stripped credentials as provided by `tests/conftest.py`. Do not rely on another worktree or ambient developer credentials.
8. Coding agents may modify deployment definitions when the task requires it, but ordinary development work must not mutate production infrastructure or use production credentials.

## 1. Repository boundaries

The target structure is defined by `docs/architecture/unified/09-repository-structure.md`.
Implemented behavior is described by `docs/specs/unified/`.

| Path | Responsibility |
| --- | --- |
| `protocol/` | Wire/data types shared by the runtime and control plane; must not depend on `control` |
| `control/domain/` | Pure domain models and invariants; no infrastructure dependencies |
| `control/application/` | Use cases, orchestration, and application ports |
| `control/persistence/` | PostgreSQL schema/migrations, unit of work, repositories, durable business authority |
| `control/jobs/` | Durable jobs, claims/fences, timers, and handlers |
| `control/api/` | The single business HTTP surface under `/api` |
| `control/executors/`, `control/runtime_client/` | Local/Modal executor implementations and the sbx-runtime client boundary |
| `control/integrations/`, `control/security/` | Git/GitHub/email/connectors, vault, credentials, authentication, redaction |
| `runtime/` | `sbx-runtime` daemon and official CLI Harness implementations; must not depend on `control` |
| `console/` | The single product frontend |
| `src/sbx/` | Python SDK and CLI |
| `docs/specs/unified/` | Implemented contracts and specifications |
| `docs-site/` | Public product documentation site |
| `docs/archive/` | Historical material only; do not treat it as an active specification |

Dependency direction is enforced by `tests/unit/test_layer_boundaries.py`.

### Change-scope rule

A task may legitimately span several areas, especially for large features or refactors.
The developer should still keep the implementation focused on the requested behavior:

- touch only modules whose behavior or contracts are actually affected;
- do not rewrite neighboring systems merely because they could be improved;
- if an unrelated defect is discovered, report it separately rather than silently expanding the task;
- prefer coherent functional boundaries over arbitrary directory-only splits.

## 2. Contracts and durable interfaces

The following interfaces have a wider blast radius and require deliberate changes:

- `docs/specs/unified/openapi.yaml` (generated; drift checked by `tests/unit/test_openapi_drift.py`);
- `protocol/runtime.py` and `docs/specs/unified/runtime.md`;
- `runtime/harnesses/protocol.py` and `docs/specs/unified/harnesses/manifests.json`;
- `control/persistence/migrations/*.sql` (append-only migrations).

When a task changes one of these contracts, update all affected implementations, tests,
SDK/CLI/Console surfaces, and specifications required to keep the repository internally
consistent. Do not preserve obsolete behavior merely for compatibility unless the task or
active specification requires it.

## 3. Developer role

The developer owns implementation quality and a reviewable handoff.

### Developer responsibilities

1. Understand the requested behavior and identify the smallest coherent change surface.
2. Inspect existing abstractions before adding new ones; reuse the established domain, application, runtime, and client boundaries.
3. Implement the feature or fix completely across the layers that are actually affected.
4. Add or update regression tests for changed behavior and important defects found during development.
5. Update specifications and public documentation when the user-visible contract changes.
6. Run relevant local checks and fix failures caused by the change.
7. Review the final diff for accidental files, secrets, unrelated edits, stale generated artifacts, and incomplete migrations/spec updates.
8. Deliver a branch/PR with a concise summary, validation results, important compatibility notes, and known non-blocking limitations.

Large tasks may be implemented in one strong-agent session and split into multiple coherent
PRs. Those PRs should be understandable and mechanically mergeable, but the overall feature
or refactor may be reviewed as a stack from its original base to its final head.

The developer should not spend model capacity on unrelated polishing after the requested
behavior is correct and the applicable review blockers are resolved.

## 4. Reviewer role

The reviewer is independent from the developer and evaluates whether the requested change is
safe and correct enough to merge. Review is a release-quality gate, not an unlimited search
for any imaginable failure.

### Reviewer responsibilities

1. Review the requested behavior, the changed code, affected contracts, tests, and the final integrated state.
2. Prefer concrete, reproducible findings. A blocking finding should identify the affected path, preconditions, impact, and a plausible reproduction or execution sequence.
3. Distinguish product-breaking defects from hardening suggestions, theoretical edge cases, style preferences, and speculative risks.
4. For a large stacked change, review the cumulative diff from the original base to the final stack head first. Individual PRs may then be checked for ordering, dependency, and merge hygiene.
5. Batch blocking findings whenever practical. Avoid a review loop where one small issue is reported, fixed, and re-reviewed in isolation while other material issues remain undiscovered.
6. After fixes, verify that reported P0/P1 findings are resolved and that the fixes did not introduce material regressions. Do not restart an unbounded adversarial search from scratch on every round.
7. PASS the change when required CI/checks are green and no reproducible P0 or P1 findings remain.

## 5. Review severity and merge policy

Only **P0** and **P1** findings block merge.

### P0 — critical blocker

A P0 is a critical security, data-integrity, or system-safety failure, or a catastrophic
logic defect with unacceptable impact. Examples include:

- authentication or authorization bypass;
- cross-user or cross-workspace data access;
- credential/secret disclosure;
- attacker-triggerable remote code execution or equivalent trust-boundary compromise;
- unrecoverable corruption or loss of authoritative data;
- a wrong irreversible external effect, such as mutating or merging the wrong repository/subject;
- a fundamental logic failure that makes the product broadly unsafe to operate.

P0 severity is driven by impact and exploitability. A security flaw does not become
non-blocking merely because an ordinary user would be unlikely to trigger it accidentally.

### P1 — material product blocker

A P1 is a reproducible defect that materially breaks a supported product workflow for a
normal user, or under reasonably expected operating conditions such as ordinary retries,
cancellation, restart, transient network failure, or supported concurrency.

Examples include:

- a normal Session/Turn/Delivery workflow producing the wrong durable result;
- a supported retry or cancellation path causing a duplicate or incorrect effect;
- a common restart/recovery path leaving the product persistently unusable or incorrect;
- an API/Console/CLI mismatch that prevents a supported feature from working;
- a major functional regression that a normal user is reasonably likely to encounter.

A finding should **not** be classified as P1 merely because an arbitrarily contrived,
multi-failure sequence can make some function fail. Rare, highly artificial combinations
that are non-security-sensitive, do not corrupt authoritative data, do not cause a wrong
irreversible external effect, and are recoverable should normally be P2/P3 or a follow-up
hardening item rather than a merge blocker.

### P2/P3 — non-blocking findings

P2/P3 findings may include:

- rare recoverable edge cases outside normal supported usage;
- hardening opportunities;
- maintainability or observability improvements;
- minor UX defects;
- performance improvements that do not break the supported flow;
- speculative issues without a concrete reachable path.

Record useful P2/P3 findings, but do not keep a change in an endless review/fix cycle solely
to eliminate every possible edge case.

### Merge stop condition

A change is ready to merge when:

- required checks are green;
- there are no known reproducible P0 findings;
- there are no known reproducible P1 findings within supported/normal product behavior;
- previously reported P0/P1 fixes have appropriate regression coverage where practical.

The goal of review is to establish that normal users can use the affected product behavior
reliably and that no major security or integrity issue is known. The goal is **not** to prove
that a sufficiently creative reviewer can never construct any sequence that produces a
recoverable failure.

## 6. Tests and local environment

```bash
make lint           # ruff check + ruff format --check
make test           # unit + integration tests with embedded PostgreSQL and no cloud credentials
make console-check  # Console typecheck + Vitest + production build
make docs-check     # documentation site build and link checks
```

| Variable | Meaning |
| --- | --- |
| `SBX_DATABASE_URL` | PostgreSQL connection string; PostgreSQL is the business authority |
| `SBX_VAULT_KEYS` | `kid:base64key[,...]`; first key is active for CredentialVersion envelope encryption |
| `SBX_RUNTIME_MASTER_KEY` | Hex key used to derive per-lease sbx-runtime authentication keys |
| `SBX_EXECUTORS` | Enabled executors; default `local,modal` |
| `SBX_PUBLIC_URL` / `SBX_ALLOWED_ORIGINS` / `SBX_COOKIE_SECURE` | Console origin and cookie policy |
| `SBX_RESEND_API_KEY` | Optional email provider credential; local outbox is used when absent |
| `SBX_MAIL_FROM` | Required when Resend is enabled; must use a verified sender domain |
| `SBX_INFERENCE_ALLOW_PRIVATE_URLS` | `1` lets inference Connections target private-network or `http` base URLs; off by default |

`tests/conftest.py` strips host credentials (including `SBX_TEST_*` and
`SBX_BENCHMARK_*`) and isolates HOME/XDG. Tests must receive required configuration
explicitly. `import modal` is restricted to `control/executors/modal.py` and
`control/integrations/connectors/modal.py`; `make test` must never contact a real Modal
account. Live checks such as `make smoke-modal`, `make check-connectors`, and
`make mvp-acceptance` are explicit opt-in operations and must clean up resources they create.

## 7. Secrets and production boundaries

- Never commit `.env`, `auth.json`, `.modal.toml`, API tokens, passwords, private keys, or production credentials.
- Fixtures, stubs, mocks, logs, PR descriptions, and documentation must use `REDACTED` for secret-shaped values.
- User credentials are stored as encrypted CredentialVersions and delivered to runtimes only through scoped grants; plaintext credentials must not be returned by the API or written to logs/events.
- Ordinary coding and review agents must not be given production mutation credentials.
- Production deployment, DNS changes, production database mutation, secret rotation, and similar privileged actions should occur through the protected release/operations path rather than as an incidental step of feature development.
