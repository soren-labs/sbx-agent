# Hosted Codex credential broker

Codex / ChatGPT plan authorization is independent of Modal and GitHub.
Integrations starts a user-bound expiring authorization state and exchanges a
provider code on the VPS. `CodexProvider` supplies redirect, exchange and refresh
operations; the production default is explicitly unavailable. Mock mode uses a
deterministic rotating fake, storing fake upstream grant hashes durably too.

The shared connection vault encrypts the full credential document. The broker
issues short access leases containing no refresh grant. Runtime credentials use
the existing `SBX_ACCOUNT_CREDENTIAL` restoration shape. Hosted sandboxes cannot
write authoritative credential state: legacy CredentialSync and the sandbox CLI
refresh worker are disabled in hosted mode. The existing account scheduler and
capability types remain intact, exposed through owner-filtered registry views.
Each Codex connection allows three concurrent Sessions; each user's scheduler and
compute capacity have a five-sandbox bound. Existing cooldown feedback is reused.

Refresh runs proactively every fifteen seconds, near expiry (sixty-second margin)
and on demand. A database-serialized, committed claim exposes Refreshing while
upstream work is in flight. Contenders reread and await that claim instead of
rotating independently. CAS commits the encrypted replacement and credential
version together. Omitted refresh tokens preserve the prior grant. A reconnect or
disable supersedes any stale success or failure. Provider adapters must bound
network requests below the sixty-second claim lifetime (recommended fifteen
seconds) and classify InvalidGrant separately from transient/rate-limit errors.

Revocation enters Reauth required. Transient failures preserve the encrypted
grant and apply a durable cooldown. If a process dies after claiming rotation,
the expired claim requires reauthorization: replaying a possibly consumed refresh
token would be unsafe. A successful upstream rotation followed by database
failure likewise leaves the durable claim for containment. No token material or
upstream error body is logged. `execute` retries one authentication failure with
a fresh credential; simultaneous stale failures join the already rotated version.

Design references studied for SOR-285:
[Sub2API OAuth refresh](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/oauth_refresh_api.go)
uses lock/reread/versioned persistence, retained credential fields and race
recovery. Its
[credential persistence boundary](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/account_credentials_persistence.go)
centralizes credential writes. SBX applies those patterns to its existing
PostgreSQL connection/CAS foundation, with fail-closed interrupted-claim recovery.
No third-party code is copied.

Real authorization redirects, provider expiry/rotation semantics and authenticated
Codex execution require the production adapter and later credential validation.
The fake covers rotation, omission, three contenders, revocation, transient
failure, reactive retry, reconstruction, stale reconnect and interrupted claims.


## Official native connection (SOR-292)

`control.real_codex.NativeCodexProvider` uses the official Codex app-server
protocol for device authorization and managed refresh. The browser sees only
OpenAI's verification URL, the one-time device code and its owner-bound state;
server polling consumes completion once. The control-plane CLI must be installed
and pinned independently of sandbox compute. No invented OAuth endpoint or SBX
client registration is used. This is the native Codex integration mechanism;
SBX does not claim to be a separately registered hosted Sign in with Codex client.

An already authorized **dedicated product** login can be imported by the trusted
operator using `python -m control.real_codex --user-id USER --codex-home PATH`.
The source must be private and inside `SBX_CODEX_IMPORT_ROOT`. The development
agent's normal `.codex` directory is rejected. The importer validates native
account metadata, then encrypts selected grant fields in the connection vault;
it does not copy the native auth file into the checkout or a sandbox.

Native refresh runs in a private temporary directory on tmpfs, with an explicit
HOME/CODEX_HOME and a stripped child environment. The native CLI owns the actual
token request and rotation. Temporary grant state is removed after the broker
commits its versioned result. A cached account response after a failed refresh
cannot be mistaken for successful rotation. Interrupted claims still require
reauthorization, as described above.

Sandbox auth uses the official `chatgptAuthTokens` mode. Its native TokenData
format requires an empty `refresh_token` field and non-secret `last_refresh`
metadata. The empty field contains no refresh grant. A runtime authentication
failure retries once with the broker's current version; a second rejection marks
that version as requiring reauthorization. A stale rejection cannot invalidate a
newly rotated connection. Hosted init/turn completion and cancellation remove
the sandbox auth cache; the next operation receives a new lease. The five-minute
broker lease bounds SBX issuance; upstream OAuth access-token expiry remains
provider-controlled and cannot be shortened by changing local lease metadata.

Primary references: [app-server authentication](https://learn.chatgpt.com/docs/app-server),
[native authentication](https://learn.chatgpt.com/docs/auth), and the pinned
[0.159.2 auth manager](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/login/src/auth/manager.rs)
and [0.153.0 TokenData](https://github.com/openai/codex/blob/rust-v0.153.0/codex-rs/login/src/token_data.rs).
