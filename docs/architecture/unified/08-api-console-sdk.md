# One business API, Console and clients

**NORMATIVE.** [Domain](02-domain-model.md), [events](04-events-persistence-jobs.md), [security](06-connections-security.md).

## Business API contract

The target MUST expose one coherent `/api/...` business API for all deployment profiles and clients. Product auth uses `/api/auth/...`. Health endpoints `/healthz` and `/readyz` are operational. Runtime `/internal/runtime/connect` has independent wire versioning for image rollout and MUST NOT become a second business API. There MUST NOT be permanent `/v1`, `/api/v2`, `/hosted` domain routers.

OpenAPI 3.1 MUST be published as reviewed `docs/specs/unified/openapi.yaml`, with error/event/Harness/runtime/result schemas alongside it. Typed request/response definitions and generated clients MUST have CI drift checks. Resource paths below are canonical; adding optional routes cannot change their ownership/semantics. Secrets are write-only and omitted from all safe resource views.

| Routes | Methods / semantics |
| --- | --- |
| `/api/auth/register`, `/login`, `/logout`, `/email-verifications`, `/password-resets`, `/password-changes` | POST; product identity; explicit expiry/rate limits, hashed verifiers and CSRF for cookie mutations |
| `/api/me`, `/api/api-keys`, `/api/api-keys/{key_id}` | GET identity; GET/POST key list/create; DELETE revoke, plaintext once |
| `/api/workspaces`, `/api/workspaces/{w}` | GET ownership scope; personal initially; team administration later |
| `/api/workspaces/{w}/projects`, `/api/projects/{p}` | GET/POST index/create; GET/PATCH metadata with version precondition |
| `/api/projects/{p}/versions`, `/api/projects/{p}/environment-builds` | GET/POST immutable version; GET/POST build request via Job, explicit exact input key |
| `/api/workspaces/{w}/connections`, `/api/connections/{c}` | GET/POST metadata/manual creation; GET/PATCH safe metadata; DELETE explicit revoke/tombstone |
| `/api/connections/{c}/credential-versions`, `/validations`, `/capabilities` | POST write-only replacement with CAS; GET/POST safe validation operations; GET timestamped catalogs/limits |
| `/api/workspaces/{w}/sessions`, `/api/sessions/{s}` | GET/POST with initial Message optional atomically; GET/PATCH title/labels/supported settings; filter project/lifecycle/role/activity/parent |
| `/api/sessions/{s}/messages`, `/turns`, `/api/turns/{t}` | GET/POST authored input; GET ordered Turns; GET outcome/settings/evidence completeness |
| `/api/turns/{t}/cancellations`, `/retries`, `/steering`, `/approvals/{a}` | POST explicit intent/new Turn/verified injection/verified native approval; no generic Session retry guessing phase |
| `/api/sessions/{s}/events` | GET SSE (`Accept: text/event-stream`) or paged JSON, committed Session sequence only |
| `/api/sessions/{s}/archives`, `/unarchives`, `/closures`, `/continuations` | POST lifecycle commands; continuation creates linked Session with explicit handoff |
| `/api/sessions/{s}/environment-updates` | POST explicit idle-compatible repin/checkpoint operation; no implicit Project propagation |
| `/api/sessions/{s}/executor`, `/executor/activations`, `/executor/releases` | GET projection; POST explicit wake/release with Job; opaque backend handles absent from product identity |
| `/api/sessions/{s}/snapshots`, `/restores` | GET history; POST checkpoint/explicit restore operation with generation/compatibility pins |
| `/api/sessions/{s}/files`, `/file-uploads` | GET bounded list/read; PUT content with digest/generation; POST upload intent/private refs; unavailable returns diagnosis, never wakes on GET |
| `/api/sessions/{s}/terminals`, `/api/terminals/{pty}/attachments` | POST create/attach grant; lease-bound WS live channel; writer scope separate, initial barrier policy applies |
| `/api/sessions/{s}/services`, `/api/services/{i}/activations`, `/stops`, `/logs`, `/preview-grants` | GET instances/logs; POST desired actions/grants; exact declaration and current lease |
| `/api/sessions/{s}/changes`, `/changesets` | GET live observation or captured list; POST capture with expected generation/source/purpose |
| `/api/changesets/{cs}`, `/files`, `/diff`, `/applications` | GET immutable manifest/lazy files/diff; POST apply to authorized destination with base/generation pins |
| `/api/changesets/{cs}/deliveries`, `/api/deliveries/{d}` | GET/POST exact intent; GET projection with step evidence and merge eligibility |
| `/api/deliveries/{d}/retries`, `/refreshes`, `/merge-requests` | POST same-intent transport retry, explicit remote reconcile, authorized exact-subject merge operation |
| `/api/sessions/{s}/delegations`, `/api/delegations/{g}` | GET/POST child assignment; GET pinned inputs/contract/state; no special Review endpoints |
| `/api/delegations/{g}/result`, `/waits`, `/cancellations` | GET validated result; POST submit result/register wait/cancel; repeated result publication dedupes |
| `/api/blobs/uploads`, `/api/blobs/{b}/download-grants` | POST bounded private upload/download grants with ownership/integrity checks |
| `/api/jobs/{j}`, `/api/operations/{o}` | GET safe owner-visible long-action status; operation projects Job/attempt/effect evidence, not another authority |
| `/api/harnesses`, `/api/executor-backends`, `/api/models` | GET installed support/capability catalog, Connection-scoped model availability; no probe side effects |
| Later `/api/execution-presets`, `/skills`, `/tools`, `/extensions`, `/automations`, `/webhook-endpoints` | versioned configuration resources; triggers use ordinary domain commands |

`/internal/tools` and `/internal/credentials` MAY implement authenticated private callbacks under narrowed grants. Product clients MUST NOT call them for Session business mutations. Preview lives on its dedicated origin; terminal/file streams are authorized through API-issued capabilities, not direct ownerless tunnels.

All mutations MUST carry `Idempotency-Key`; safe low-level clients MUST generate/reuse one across network uncertainty. Requests with changed payload under a reused key conflict. Update actions MUST include expected entity version and content/generation/subject pins as relevant. Lists use `limit,cursor`, deterministic timestamp+ID ordering and bounded pages. Global detail IDs still check Workspace/principal. No route treats possession of an ID as access.

Immediate creates return 201; accepted long operations return 202 with resource IDs, `operation_id`, `job_id` when meaningful, current state/version and `event_watermark`. Message acceptance MUST return Message/Turn IDs before provisioning. Duplicate requests return original committed IDs; no request waits on a sandbox boot. Job payload/worker holder/raw credentials are never public.

Error shape MUST be `error{code,category,message,retryable,retry_after?,action?,details,request_id}` with safe details. Initial codes MUST distinguish `not_found`, `forbidden`, `version_conflict`, `idempotency_conflict`, `unsupported_capability`, `credential_invalid`, `connection_revoked`, `waiting_capacity`, `rate_limited`, `quota_exhausted`, `runtime_incompatible`, `executor_unavailable`, `context_unavailable`, `context_mismatch`, `outcome_unknown`, `output_contract_invalid`, `capture_failed`, `stale_subject`, `remote_head_changed`, `delivery_unresolved`, `history_reset_required`, `invalid_cursor`. Category/source/retry advice MUST NOT conflate authentication, provider outcome and platform transport.

Example original resource request (identifiers illustrative; no credentials):

```json
{
  "project_version_id": "pver_example",
  "harness": {"provider_id": "opencode", "model": "verified-model-id"},
  "executor": {"backend": "modal", "resource_class": "standard"},
  "message": {"routing": "queue", "content": [{"kind": "text", "text": "Fix the failing check"}]},
  "role": "developer"
}
```

Unknown/unsupported model settings MUST fail capability validation, never silently rewrite to Codex defaults. Native approval actions MUST carry approval/Execution identity and expiry; unsupported providers expose no fake action.

## Console model and state

| Surface | Authoritative server source | Local state permitted |
| --- | --- | --- |
| Projects | Project/version/environment-build projections | form draft, selected tab/filter |
| Sessions | Session lifecycle/role/activity and authorized actions | URL filters, selected Session, display preferences |
| Conversation | Messages/parts/Turns with ordinal and effective settings | composer draft, optimistic request key |
| Activity | committed events/observed tool projection | expansion and paging |
| Changes | live observation, immutable subject selection, Delivery gates | selected file/subject, lazy diff view |
| Files | runtime bounded observation or historical manifest | editor draft; content preconditions required on save |
| Terminal | lease-local PTY/grants/process status | bounded buffer/resize; no claimed restored process |
| Services/Preview | current service realization and authorized preview grant | pane/navigation; untrusted app separate origin |
| Child Sessions | Delegation inputs/results/waits | child navigation and role presentation |
| Connections | safe config/health/catalog/version | ephemeral secret entry only, cleared after submission |
| Settings | identity/product API-key security, local preferences | language/theme/layout/accessibility preferences |

One typed API client under `console/src/api/` and one query/event store under `state/` MUST replace hosted/prototype/product facades. No UI code decides terminal success, credential readiness, review validation or merge eligibility. Server eligibility includes exact subject/version/reasons/freshness; merge sends those pins and server revalidates. Role/provider name branches SHOULD be replaced by manifest capabilities.

Initial snapshot/history MUST include watermark from the same DB snapshot. Replay starts after it. Reducers MUST dedupe seq/event IDs; message parts MUST apply revisioned delta-versus-replacement semantics, never append cumulative provider text twice. SSE drop MUST NOT create another Turn. Gaps/reset/schema incompatibility fetch a new projection snapshot. Optional stream multiplexing MAY use the same envelopes/watermarks, never a second cursor scheme.

Caches MUST be disposable, size/retention bounded and scoped by user/Workspace/resource/schema version. Logout/access loss purges them; credentials/native auth/grants MUST NOT enter localStorage/IndexedDB. Diffs/files/logs/PTY buffers are separately bounded and lazy. Accepted queued Message stays visible through checkpoint/provisioning loss. UI retry labels MUST name Turn, Delivery, restore or validation. Availability and saved recovery point must be visible independently of conversation outcome. Existing responsive layout, i18n/theme and accessibility SHOULD survive component rewrites.

## SDK and CLI

Keep published Python package `sbx` under `src/sbx/`; replace legacy namespaces in one coordinated major client release. Resource namespaces MUST be `projects`, `connections`, `sessions`, `messages`, `turns`, `changesets`, `deliveries`, `delegations`, `operations`. Thin convenience `execute(prompt, session_id?, project_version_id?, settings...)` MUST create/continue ordinary resources and stream committed events until the accepted Turn is terminal. It MUST NOT contain a local proprietary agent loop or interpret process exit as terminal authority.

SDK reconnect/wait MUST have explicit deadlines, cursor replay, stable mutation keys and unknown-outcome errors. Waiting for a Turn and waiting for a validated DelegationResult are different methods. Stream-json MAY offer an explicitly versioned lossy client projection for integrations; it MUST not replace the SBX event envelope or promise universal Amp/Claude compatibility. A separate TypeScript SDK MAY share the Console generated transport later, without blocking initial release.

CLI MUST expose `sbx sessions create/list/show/send/events/cancel/continue/export/close`, `projects`, `connections add/replace/validate/disconnect`, `changesets capture/apply`, `deliveries request/retry/merge`, `delegations spawn/wait/result/cancel`, and explicit `operations` diagnostics. Remote execution uses API; Local uses an enrolled local runtime through the same control authority. Later `sbx runner` registers served roots outbound under explicit grants. Connection input SHOULD use protected stdin/file UX, not secrets in argv or shell history. Deployment/doctor commands are explicit operator tools, never hidden Session orchestration or ambient provider login fallback.
