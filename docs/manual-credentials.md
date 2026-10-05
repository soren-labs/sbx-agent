# Minimum-dependency hosted setup (SOR-295)

Register, verify email and set a password. In Integrations, configure Modal Token
ID/Secret, an OpenCode Zen API key and a GitHub token. Create a Session with
OpenCode and an accessible repository. ChatGPT/Codex remains optional. The
existing Session, workspace, revision, independent review and delivery engines
are reused. No social login, GitHub App installation, GitHub OAuth or Modal OAuth
is required. Existing App-only and Codex connections remain supported.

## Persistence and secret lifetime

GitHub (`github_token`) and Zen (`opencode`) use `hosted_connections` through
`ConnectionStore`, with the existing AES-GCM vault and authenticated context
`user_id:provider:connection_id`. No migration or separate credential database is
needed. Production still requires PostgreSQL and a stable deployment-managed
`SBX_CONNECTION_ENCRYPTION_KEY`; retain that key across control-plane restarts.

Connect/replace validates before saving, so a rejected replacement leaves the
existing connection intact. Explicit validation marks rejected credentials
Invalid; transient upstream failures keep the prior state. Disconnect removes
the encrypted secret and exposes Disabled. This removes SBX's copy; revoke the
credential at its issuer to invalidate it outside SBX. Secrets are never returned
by connection APIs and the Console never writes them to browser storage.

Modal disconnect refuses with HTTP 409
`modal_resources_require_cleanup_before_disconnect` while the owner's compute
records are not released or a sandbox create/restore is unresolved. Close the
owner's Sessions and retry after successful teardown. Runtime provisioning also
blocks disconnect with `modal_provisioning_in_progress_retry_disconnect`; finish
or reconcile provisioning before retrying. Refusal preserves the encrypted
credential, runtime metadata and connection state, including across restarts;
another owner's resources cannot be used or deleted to unblock disconnect.

Create/restore commits an owner-scoped `hosted_sandbox_creates` claim before
calling Modal, serialized with disconnect using the connection's database lock.
A crash or ambiguous provider failure retains that claim and teardown authority.
An operator must reconcile the owner's Modal resources using the retained
credential and confirm cleanup before removing an unresolved claim; elapsed time
alone never releases it. Successfully terminated sandbox records (`released`)
do not block disconnect. Filesystem snapshots alone are not live compute; future
restore still requires a ready Modal connection.

Repository enumeration, repository probes, runner exec, server-side push/PR and
merge select the authenticated owner's token. A manual GitHub record takes
precedence over existing App installations, including after disconnect: SBX
never silently falls back to an App token. Owners without a manual record retain
the existing App behavior. Production startup no longer requires App configuration.

Zen launch and follow-up materialize only that owner's API credential through
`SBX_ACCOUNT_CREDENTIAL`. The standard OpenCode adapter is used. Native operation
cleanup removes the auth cache after init/turn, including failed operations.
Events redact the actual injected manual keys as well as known vendor formats;
workspace/revision scans reject files containing the current Zen or GitHub secret.

## GitHub permissions

Use a classic PAT with `repo` (`public_repo` for public-only access), or a
fine-grained token with the selected repositories, Metadata read, Contents
read/write and Pull requests read/write. Approve organization access/SSO where
required. Connect verifies identity, classic scopes and repository enumeration;
repository probes recheck access before use. GitHub does not offer a universal
non-mutating introspection endpoint proving every fine-grained write permission.
Actual PR/write refusals therefore return instructions to check repository
selection, organization approval and Contents/Pull requests permissions.

The API surface is narrow: GET/POST/DELETE `/hosted/connections/github`, POST
`/hosted/connections/github/validate`, and GET `/hosted/repositories`. POST accepts
`{"token":"REDACTED"}`. Status returns safe metadata only.

## OpenCode Zen contract and model selection

Verified against the installed CLI **1.18.29**, the repository's existing pin,
and its [auth source](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/auth/index.ts),
[global paths](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/core/src/global.ts),
[provider source](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/provider/provider.ts)
and [run source](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/cli/cmd/run.ts).
The data file is `$XDG_DATA_HOME/opencode/auth.json`, with XDG rooted under the
sandbox's isolated home. Its Zen entry is:

```json
{"opencode":{"type":"api","key":"REDACTED"}}
```

The existing adapter invokes `opencode run ... --format json -m opencode/MODEL
--dir WORKDIR --auto`; follow-ups add `--session NATIVE_SESSION_ID`. Alternate
ambient API-key/auth-content/config bridges are stripped. Hosted Zen writes a
non-secret config selecting only the Zen provider and pins `small_model` to the
chosen model, keeping title generation on the selected free model too.
[OpenCode configuration docs](https://opencode.ai/docs/config/#models) describe
that lightweight model setting. The hosted image includes both pinned CLIs and
its provisioning smoke checks their versions.

GET/POST/DELETE `/hosted/connections/opencode` and POST
`/hosted/connections/opencode/validate` mirror GitHub; POST accepts
`{"api_key":"REDACTED"}`. Validation combines Zen's key-authenticated
[/models endpoint](https://opencode.ai/docs/zen/#models) with OpenCode's
[models.dev metadata](https://models.dev). A models listing alone is insufficient
authentication proof: candidates must also pass a minimal inference request.

The deliberately narrow MVP picker offers successfully probed, tool-capable
chat-completions models, checking at most twelve candidates. Zero input/output
cost models come first; when any work, paid models are not probed. Otherwise the
first accessible paid candidate is offered. Models using other API transports
and broader discovery are deferred. Connect/validate may generate a minimal
inference charge when only paid models are available. Validate refreshes the
persisted model list. The default is the first proved model; unadvertised models
are rejected and invalid/disconnected connections advertise no fallback catalog.

## Acceptance

Deterministic tests cover encrypted connect/replace/validate/disconnect/status,
owner isolation, restart reconstruction, repository access, GitHub delivery/PR/
review/merge token selection, Zen catalog defaulting, real runner launch/follow-up
with a fake official CLI, auth materialization/cleanup, runtime key redaction,
provider propagation to independent reviews, and Console forms/composer.

`tests/integration/control/test_manual_connections_postgres.py` runs against an
explicit disposable local PostgreSQL via `SBX_TEST_MANUAL_POSTGRES_URL`. It skips
when that opt-in is absent; normal tests require no cloud credentials.

On 2026-10-05, a safely available environment Zen key passed live discovery and
minimal inference validation for `opencode/space-bunny-free`. The installed
1.18.29 CLI completed a real runner turn with that free model; captured output
contained no key and the credential cache was removed. Earlier attempts returned
an upstream/server error; they are not counted as successes. No production Modal
sandbox or hosted deployment was exercised. No manually supplied GitHub token
was available in the environment, so GitHub write acceptance remains deterministic;
no branches or PRs were created in the disposable test repository.

GitHub App/browser OAuth, Modal OAuth, other provider OAuth and broader enterprise
login are retained/deferred enhancements. This change does not merge or deploy
itself, alter frozen contracts, or replace the runtime/delivery architecture.
