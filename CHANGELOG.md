# Changelog

## [Unreleased] — unified architecture cutover (RFC 167)

- Rebuilt the product on the unified architecture: durable Sessions,
  replaceable ExecutorLeases, logical Worktrees, official-CLI Harnesses,
  the supervised `sbx-runtime` daemon, append-only `session_events` plus
  typed projections, durable Jobs with claims/fences/outbox.
- Added immutable ChangeSets (canonical `subject_digest`), exact-subject
  Delivery (branch/push/draft PR), and Delegation child Sessions with
  typed result contracts.
- One business API at `/api`, one typed Console client/state model, one
  Python SDK (`sbx.sdk.unified`) and CLI (`sbx` / `python -m sbx`).
- Connections: email/password auth, OpenCode Zen API key, Modal token
  pair, GitHub personal token — secrets only in encrypted
  CredentialVersion records.
- Removed the legacy Task/Agent/Run/V1/V2/hosted machinery per the RFC
  deletion gates; no compatibility facade, no dual writes.
