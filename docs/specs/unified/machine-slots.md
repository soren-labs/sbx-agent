# Machine Slots (subscription logins)

Implemented contract for subscription Machine Slots: the official login, Sessions that run on a
Slot, the per-Slot model catalog and reasoning effort.

## Model

| Term | Meaning |
| --- | --- |
| Machine Slot | One independent official subscription login: a `machine_slots` row plus one private Modal Volume v2 in the owner's Modal workspace. It is not a running VM. |
| Setup VM | A temporary `runtime="vm"` sandbox that runs the provider's official CLI against the Slot's Volume and is destroyed when the attempt ends. |
| Holder | The single VM allowed to mount the Slot's Volume: a Setup VM (`setup`) or a Session's Worker (`worker`). A Slot has at most one holder. |

Logging in to the same subscription account twice creates two Slots with two Volumes and two
independent logins. A Volume backs at most one Slot per Modal Connection. Slots add isolated
logins; they do not add subscription quota.

Providers are adapters (`control/integrations/subscriptions`). `GET /api/subscription-providers`
lists them with their `login_method` and `verification_host`, and `available=false` when the Modal
executor is not enabled.

| `provider` | Official CLI (pinned in the image) | `login_method` | Official login run in the Setup VM | Login kept under |
| --- | --- | --- | --- | --- |
| `codex` | `codex` (npm) | `device_code` | `codex login --device-auth` | `CODEX_HOME=/profile/.codex` |
| `grok` | `grok` (npm) | `device_code` | `grok login --device-auth` | `GROK_HOME=/profile/.grok` |
| `antigravity` | `agy` (release archive, SHA-512 pinned) | `authorization_code` | the `agy` first-run sign-in screen, "Google OAuth" | `HOME=/profile` |

Every provider's `HOME` and XDG directories point into the Slot's own Volume, so two Slots never
share a home, whatever their provider or account. No API key is involved in any of them.

## What the control plane stores

`machine_slots`: provider, label, optional account alias, Modal Connection, Volume name,
`volume_managed`, state, holder, safe capabilities (CLI version, verification time).
`slot_login_attempts`: mode, state, Setup VM handle, verification URL, the one-time user code
while it still has to be entered, expiry, outcome code. A pasted authorization code is not stored.

It never stores, reads, returns or logs the login itself. The official CLI writes it inside the VM
onto the Slot Volume. The control plane reads only the supervisor's state file
(`/tmp/sbx-setup/state.json`); it never lists or opens the profile directory. No Modal Secret, OIDC
token or inference key is given to a Setup VM.

## States

Slot `state`: `login_pending` (never logged in yet), `ready`, `needs_login`, `error`, `deleting`,
`deleted`. The API also returns `status`, what a user sees:

| `status` | Condition |
| --- | --- |
| `running` | a Worker holds the Slot |
| `login_pending` | a login or verification attempt is live |
| `ready` / `needs_login` / `error` / `deleting` | otherwise, the Slot `state` |

Attempt `state`: `starting` → `awaiting_user` → `verifying` → `succeeded`, or `failed`, `expired`,
`cancelled`. One live attempt per Slot (unique index).

## Login

1. `POST /api/workspaces/{id}/machine-slots` `{provider, label?, account_alias?,
   compute_connection_id?}` creates the Slot and its first attempt and enqueues `slot.login`.
   A verified Modal Connection is required (`connection_required`).
2. The Job starts the Setup VM (idempotent by the attempt's operation ID, shared image, Volume
   `sbx-slot-<id>` created if missing, mounted at `/profile`). Its main process is
   `runtime.subscriptions.setup`, which runs the adapter's official device login with stdin closed.
3. The attempt becomes `awaiting_user` with the real `verification_url`, `user_code` and
   `code_expires_at` reported by the CLI. The URL and code are returned separately; no deep link is
   invented.
   For `login_method=authorization_code` there is no `user_code`: the attempt carries
   `login.input="authorization_code"` and waits for the code the browser displays after sign-in
   (see below).
4. The supervisor polls the CLI's own login status. The CLI's status is the authority: a login
   process that exits non-zero does not override an accepted login.
5. Once logged in it makes one real tool-less model call, records the CLI version, runs
   `sync /profile` (the Volume v2 commit) and reports `succeeded`. A plan at its usage limit counts
   as authorized and is reported with `state_reason=usage_limited`.
6. The Job terminates the Setup VM, confirms it, clears the code and sets the Slot `ready`. There
   is no second save step.

### Sign-in that returns a code (`authorization_code`)

Antigravity's CLI has no login subcommand and no device-code flow. Its documented remote sign-in is
a terminal screen that prints an `accounts.google.com` URL and asks for the authorization code the
browser shows afterwards. The supervisor runs that screen under a PTY (no browser, no password, no
private endpoint), confirms the preselected "Google OAuth" entry, and publishes the URL.

`POST /api/machine-slots/{id}/logins/current/code` `{code}` hands the pasted code to the waiting
CLI. It is accepted only while the attempt is `awaiting_user` with `login.input` set
(`invalid_transition` otherwise) and only in the shape of an authorization code
(`validation_failed`). The code travels from the request to the Setup VM over the executor's exec
channel on stdin; it is never written to the database, the audit record (which notes only that a
code was sent), a Job, a log or a response. Inside the VM the supervisor reads it once, deletes it
and types it into the CLI; terminal output is held in memory only.

After the code is entered the attempt is `verifying`. If the CLI does not report a login within 90
seconds the attempt fails with `code_rejected` and the Slot is `needs_login`; a new login shows a
new URL. An unreachable Setup VM answers `executor_unavailable` (retryable) and the same code may
be sent again.

The CLIs for `grok` and `antigravity` have no login-status command. The supervisor asks
`grok models` / `agy models` instead; both exit 0 while signed out, so the CLI's own signed-out
sentence decides.

Failure handling: a denied login or failed verification gives `needs_login`; an unapproved code
gives `expired`; a vanished Setup VM gives `error` (`setup_vm_lost`). The control plane enforces its
own deadline in addition to the supervisor's, and the Setup VM has a hard timeout of the login
window plus five minutes, so it ends even if the control plane is down. Every terminal transition
first confirms VM termination; an unconfirmed termination retries.

| Route | Effect |
| --- | --- |
| `POST /api/machine-slots/{id}/logins` | Official login again (re-login). If it does not complete, a previously `ready` Slot stays `ready`. |
| `POST /api/machine-slots/{id}/verifications` | Check the stored login with one real CLI call on a fresh VM. |
| `DELETE /api/machine-slots/{id}/logins/current` | Cancel the live attempt and stop its VM. |
| `POST /api/machine-slots/{id}/logins/current/code` | Send the code the browser displayed (`authorization_code` providers only). |
| `PATCH /api/machine-slots/{id}` | Rename, change the account alias. |
| `POST /api/machine-slots/{id}/logout` | Destroy the Slot's Volume; the Slot becomes `needs_login`. Provider-side sessions are not revoked. |
| `DELETE /api/machine-slots/{id}?confirm=<label>` | Delete the Slot and its Volume. Refused while a Worker holds it. |

Creating a Slot with `volume_name` adopts an existing Volume in the same Modal workspace: the first
attempt only verifies it, the Slot is `volume_managed=false`, and SBX never deletes that Volume.

A Modal Connection cannot be disconnected while Slots keep Volumes in it (`connection_in_use`,
`details.machine_slots`). Slots are owner-scoped like every other resource: another owner gets
`not_found`.

## Model catalog

After a successful login or verification the supervisor asks the authenticated CLI for its models.
The adapter normalizes the answer into `capabilities.catalog`:

```json
{"status": "ready", "source": "codex app-server model/list", "observed_at": "...",
 "cli_version": "...", "default_model": "<id>", "complete": true,
 "models": [{"id": "<id>", "name": "...", "default": true,
             "reasoning": {"kind": "levels", "default": "<effort>",
                           "efforts": [{"id": "<effort>", "description": "..."}]}}]}
```

| Provider | `source` | What the CLI gives | Efforts |
| --- | --- | --- | --- |
| `codex` | `codex app-server model/list` | JSON-RPC model list | each model's `supportedReasoningEfforts` |
| `grok` | `grok agent stdio initialize` | `_meta.modelState.availableModels` and `currentModelId` of the CLI's agent protocol | each model's `reasoningEfforts` menu ids (what `--effort` accepts); none when the model does not support effort |
| `antigravity` | `agy models` | printed text, `<slug><TAB><display name>` per line; there is no machine-readable catalog | none as a separate setting: the CLI lists one slug per effort variant (`...-high`, `...-low`), so every model has `reasoning.kind = "none"` and choosing the variant chooses the effort |

Every model id and effort is the CLI's own answer for that login; hidden models are dropped; a
default the CLI does not list is dropped. For `antigravity` only lines whose first field is a model
slug count, a line that is not a slug is ignored, and a model is the default only when the CLI
marks exactly one. If the CLI gives no usable answer the catalog is
`{"status": "unavailable", "models": []}`: nothing is guessed, and a Session can then only use the
provider default. `POST /api/machine-slots/{id}/verifications` refreshes the catalog.

## Sessions on a Slot

Session creation accepts `inference: {"mode": "subscription", "machine_slot_id": "<slot>"}` next
to `harness: {provider_id, model?, effort?}`. The default mode is `custom_api` (an `inference_api`
Connection). Rules, enforced server-side:

- The Slot must be `ready`, owned by the caller, and the Harness must be the adapter's (`codex`,
  `grok` or `antigravity`, the same id as the provider).
  The executor backend must be `modal`, and the compute Connection is the Slot's.
- `connections.inference` must be absent: a subscription Session never has an API key and never
  falls back to one. A `custom_api` Session never mounts a Slot Volume.
- `model` must be in the Slot's catalog and `effort` must be one of that model's efforts (an effort
  without a model is checked against the default model). With no catalog only the provider default
  (no model, no effort) is accepted. Per-Turn `settings.model` / `settings.effort` follow the same
  rules. The Session returns `inference` and `harness.effort`.

Execution:

- Creating the executor lease takes the Slot's holder in the same transaction. If another Session's
  live lease or a login holds it, the Turn stays `preparing` with reason `waiting_capacity` and no
  second VM is created. The holder is derived from durable lease state, so a lease that is released
  or confirmed lost frees the Slot without a separate unlock step.
- The Worker VM mounts the Slot Volume at `/profile` (never created at allocation) and nothing
  else of the Slot. `turn.start` carries `inference: {"mode": "subscription", "provider_id", "env"}`
  and no secrets. Per Harness:

  | Harness | Login location | Session home | Model and effort |
  | --- | --- | --- | --- |
  | `codex` | `CODEX_HOME` on the Volume | private | `-m <model>`, `-c model_reasoning_effort="<effort>"` |
  | `grok` | `GROK_HOME` on the Volume | private | `-m <model>`, `--effort <effort>`; no `config.toml` is written |
  | `antigravity` | `HOME` is the Volume (`agy` has no home override) | the Volume | `--model <slug>` only; `agy -p --output-format stream-json` |

  Nothing is written into the profile by SBX. Native threads, sessions and conversations live on
  the Volume and are never part of a checkpoint. The `antigravity` Harness has no custom-API mode
  (`inference_protocols` is empty), so it cannot be combined with an `inference_api` Connection.
  Measured on a signed-in `agy` 1.3.3: `--model <variant> --effort <other level>` and `--effort` on a
  model without effort both fail with `invalid model selection` before any model call. The server
  therefore rejects any `effort` on an Antigravity Slot (`validation_failed`: `harness.effort` with
  `supported: []` next to a model, `harness.model` without one because the CLI marks no default) and
  the Harness never passes `--effort`.
- After each terminal Turn and before the Worker VM is terminated the executor runs `sync /profile`
  so login refreshes are committed. A VM that died cannot be synced.
- A Turn the provider rejects as unauthenticated sets the Slot to `needs_login`.
