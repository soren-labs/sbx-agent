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
| `SBX_INFERENCE_ALLOW_PRIVATE_URLS` | no | `1` allows private-network/`http` inference base URLs (self-hosted gateways only) |

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

One model for the `modal`, `github` and `inference_api` kinds. Secrets are
write-only: views expose credential version id/ordinal/format only, never plaintext, ciphertext or
fingerprints. Request validation errors never echo input.

| Kind | Manual input | Validation probe (Job) | Plaintext destination |
| --- | --- | --- | --- |
| `modal` | `token_id`, `token_secret` | `App.lookup` with the token | executor worker only (sandbox gets a lease-scoped key) |
| `github` | `token` (PAT or App installation `ghs_` token) | capability probe: `GET /repos/{repo}` for Project repos (+ `GET /installation/repositories` when `GET /user` gives no identity); only `401` or no reachable repository is `reauth_required` | Delivery worker; runtime clone helper via askpass file, never URL/argv |
| `inference_api` | `api_key`, `model`, and `endpoints` (`{protocol: base_url}`) or one `base_url` + `protocol`; optional `models` | one minimal generation request per endpoint with the configured model (quota-consuming, recorded) | the CLI process environment (`SBX_INFERENCE_API_KEY`, or `ANTHROPIC_API_KEY` for Claude Code); never written to disk |

### Generic inference (`inference_api`)

Inference is bring-your-own-key and is not tied to a vendor or a Harness. A Connection holds
one API key, a default model and one base URL per wire protocol the provider speaks:
`openai_chat` (`{base_url}/chat/completions`), `openai_responses` (`{base_url}/responses`) and
`anthropic_messages` (`{base_url}/v1/messages`). Only the key is secret. Endpoints and models are
stored as the CredentialVersion's `public_config`, returned as `config` in Connection views, and
versioned with the key, so a replacement swaps both atomically. Replacing with only `api_key`
keeps the current endpoints and model.

* **Harness matching.** Each Harness manifest lists `inference_protocols` in preference order.
  A Session uses the first of them its inference Connection offers. Auto-selection considers only
  Connections that offer one; an explicit incompatible Connection is `422 validation_failed`; when
  none qualifies the Session is refused with `409 connection_required` and
  `details.inference_protocols`. `GET /api/models?provider_id=…` reports per Connection the
  `protocol` that Harness would use and `compatible`.
* **Model.** The Session's model defaults to the Connection's `model`; a caller-supplied model id
  is pinned as given. The catalog is the configured models plus the provider's model list when it
  serves one (`GET {base_url}/models`).
* **Delivery to the runtime.** The broker sends `{"inference": {"api_key"}}` in the operation
  frame's `secrets` and the chosen `{protocol, base_url, model}` in the `turn.start` payload, so
  URLs and model ids are never treated or redacted as credential values.
* **Outbound URL policy.** Base URLs are caller-controlled and probed by the control plane, so
  they must be `https` origins without userinfo, query or fragment that resolve only to public
  addresses; loopback, private, link-local and `.internal`/`.local` targets are refused and
  redirects are never followed. `SBX_INFERENCE_ALLOW_PRIVATE_URLS=1` is the operator opt-in for
  self-hosted gateways (it also allows `http`).
* **Health reasons.** `inference_rejected_key` (401/403), `inference_endpoint_or_model_rejected`
  (400/404/405/422), `inference_base_url_not_public`, `inference_base_url_redirects` map to
  `reauth_required`; `inference_rate_limited` to `degraded`; unreachable/5xx retries.

### Retired kinds

`opencode_zen` and `codex` (uploaded `auth.json`) are no longer offered: creating one or replacing
its credential is `422 validation_failed` with `details.replacement = "inference_api"`. Official
subscription credential management is a later phase. Stored rows are preserved by migration
`0004` (the kind constraint still admits them; nothing is deleted or rewritten). They are listed
with `legacy: true`, can be disconnected, are never selected for a new Session, and a Session
already pinned to one keeps running on its original credential lane.

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
