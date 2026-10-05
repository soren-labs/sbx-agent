# SOR-290 real Modal gate

Stack base: `hosted-alpha/sor-289-real-email`, `d20323f` (PR #157).

The native SDK adapter creates an explicit credential-bound client for every
user operation. Manual Token ID/Secret is the production path. No Modal OAuth
client exists; the production console exposes the manual path. Provisioning
creates the user's `sbx-compute` environment and App, publishes a versioned
runtime image and smoke-tests the real image before recording Ready. PostgreSQL
remains authoritative for encrypted connections, provisioning and handles.

Real gate PASS using two independently credentialed workspaces: fresh Ready;
native sandbox creation and repository/runtime bootstrap; own-sandbox listing;
both application ownership rejection and actual second-workspace SDK denial;
direct TLS HTTP/SSE; rejection of another user's signed grant; incremental
event delivery before producer completion; process-group cancellation;
control-plane reconstruction against the same PostgreSQL database and reuse of
the running listener; idempotent cleanup; reprovisioning with the published
image. All test sandboxes were terminated. Production deployment is deferred
to SOR-293.

The gate discovered that `setsid` may fork when Modal starts a process group
leader. The adapter now waits for the real child with `setsid --wait`, allowing
cancellation and exit status to reflect the command instead of its launcher.

Acceptance helper: `deploy/hosted/gates/modal.py`. It uses an ephemeral real
PostgreSQL container bound to loopback, not an operator database. The direct
stream producer is a real process writing a harmless event, not an AI Session;
real Codex coding acceptance belongs to SOR-292/293. WebSocket is not advertised
because the runtime supports SSE. No Modal tokens enter the image or sandbox.

Validation: credential-free adapter/provisioning/runtime checks (23 passed),
console typecheck/build and tests (122 passed), hosted onboarding browser smoke
(1 passed). `make lint` PASS. Final `make test`: 3,423 passed, 14 skipped.
An earlier full run hit an existing fake-Codex shutdown timeout; its isolated
retry and the final full run passed. Frozen contract files are unchanged.
