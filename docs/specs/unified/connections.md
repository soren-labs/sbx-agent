# Identity, Connections and credential boundaries

Implements RFC 06 and the RFC 08 auth/connection routes.

## Control-plane configuration

`python -m control.composition {serve,worker,migrate}` reads these variables. `serve` and
`worker` apply pending migrations at startup under an advisory lock.

| Variable | Required | Meaning |
| --- | --- | --- |
| `SBX_DATABASE_URL` | yes | PostgreSQL DSN, the only business authority |
| `SBX_VAULT_KEYS` | yes | `kid:base64(32 bytes)[,…]`; first key encrypts, all keys decrypt |
| `SBX_RUNTIME_MASTER_KEY` | yes | hex; per-lease runtime keys are derived from it. Changing it invalidates live leases |
| `SBX_PUBLIC_URL` | no (`http://localhost:8800`) | origin used in email links |
| `SBX_ALLOWED_ORIGINS` | no (`SBX_PUBLIC_URL`) | comma-separated origins accepted for cookie mutations |
| `SBX_COOKIE_SECURE` | no (`0`) | `1` marks session cookies Secure; set it behind HTTPS |
| `SBX_RESEND_API_KEY` | no | send product email via Resend; otherwise mail goes to `$SBX_DATA_DIR/mail` |
| `SBX_MAIL_FROM` | with Resend | sender on a Resend-verified domain; startup fails if it is missing |
| `SBX_DATA_DIR` | no (`./.sbx-data`) | local blobs and the mail outbox |
| `SBX_EXECUTORS` | no (`local,modal`) | enabled Executor backends |
| `SBX_WORKER_THREADS` | no (`4`) | Job worker threads per process |

Keep `SBX_VAULT_KEYS` and `SBX_RUNTIME_MASTER_KEY` in a secret manager, never in the database.
Losing every vault key makes stored CredentialVersions undecryptable.

## Product identity

* `POST /api/auth/register` returns `202` with the same body whether or not the email already exists.
  The email verification token is single-use and expires after 24 h; only its hash is stored.
* `POST /api/auth/login` requires a verified email and argon2id password verification (constant
  work for unknown accounts). It is rate limited to 10 failures per email per 15 minutes (`429`).
  It sets the `sbx_session` cookie (HttpOnly, SameSite=Lax, Secure when configured) and the
  `sbx_csrf` cookie, which is readable so the client can echo it as `X-CSRF-Token`.
* Cookie mutations require a valid CSRF token and an allowed `Origin`. API keys (`sbx_key_…`,
  plaintext returned once, stored hashed) need neither. Both resolve the same
  `Principal(user, workspaces, scopes, auth_epoch)`.
* API key scopes are `*` > `write` > `read` (validated at mint; unknown stored scopes grant
  nothing). Every mutating route requires `write` (`*` for API-key management and password
  changes) and every read requires `read`; a missing scope is `403 forbidden` before any effect.
  Cookie sessions are full scope.
* Changing or resetting a password bumps `auth_epoch`, which revokes cookie sessions.

## Connections

One model for the `modal`, `github`, `opencode_zen` and optional `codex` kinds. Secrets are
write-only: views expose credential version id/ordinal/format only, never plaintext, ciphertext or
fingerprints. Request validation errors never echo input.

| Kind | Manual input | Validation probe (Job) | Plaintext destination |
| --- | --- | --- | --- |
| `modal` | `token_id`, `token_secret` | `App.lookup` with the token | executor worker only (sandbox gets a lease-scoped key) |
| `github` | `token` (PAT or App installation `ghs_` token) | capability probe: `GET /repos/{repo}` for Project repos (+ `GET /installation/repositories` when `GET /user` gives no identity); only `401` or no reachable repository is `reauth_required` | Delivery worker; runtime clone helper via askpass file, never URL/argv |
| `opencode_zen` | `api_key` | one minimal free-model chat request: 401 = rejected, free-tier gate = authenticated (quota-consuming, recorded) | OpenCode `auth.json` in isolated HOME, scrubbed after each Turn |
| `codex` | `auth_json` (allowlisted shape) | format only | Codex isolated `CODEX_HOME` |

The model catalog is Zen `/v1/models` crossed with public models.dev pricing. Free models are
marked `usable_via: official_opencode_cli`, which reflects the provider's rule that the free tier
runs only inside OpenCode. The preferred model is the first free model in a fixed preference order
(`big-pickle` first). Project/Session resolution defaults the model to the preferred one when the
caller sets none.

* CredentialVersions use AES-256-GCM. The AAD binds `{workspace, connection, credential version,
  format}`; the keyring (`SBX_VAULT_KEYS=kid:b64,...`, first key active) lives outside the
  database, and older keys still decrypt after rotation.
* Replace appends a new version, CASes the Connection version, increments the revocation epoch,
  invalidates grants/health/catalog, and enqueues validation of the exact version. A validation
  commits only if `(current version, revocation epoch, connection version, configured)` still
  matches; otherwise it is discarded.
* Disconnect is refused with `409 connection_in_use` while live or quarantined executor leases,
  live executions or quarantined capacity depend on the Connection. Teardown authority is kept until
  those are released, so live Modal compute is never orphaned. Once allowed, it revokes and
  tombstones the Connection, cancels queued Jobs targeting it, and the broker refuses decrypt from
  then on. Provider-side token revocation is reported separately (`not_performed`).
* Selection considers only the Session's own workspace Connections: explicit IDs, or auto-selection
  among configured ones by priority, skipping `reauth_required`. Environment variables, host files,
  other principals and operator credentials are never consulted.
