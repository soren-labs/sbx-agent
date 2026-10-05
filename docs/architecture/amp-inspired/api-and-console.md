# Unified API and Console product architecture

All resources/routes here are proposed names, not handler implementations or changes to frozen OpenAPI files. The target has **one `/api` domain surface**, with one authorization model and shared schemas for Console, CLI, and SDK. `/auth` handles product login. Runtime transport has its own version handshake because executors may temporarily use different images; that is not a second business API.

## 1. Resource hierarchy

Workspace is the access scope, Project is reusable configuration, and Session is work. Nested paths express discovery/ownership; globally identified resources can have canonical detail paths, always checking Workspace/principal access. Use one public Session ID everywhere.

| Resource / example routes | Meaning |
| --- | --- |
| `/auth/register`, `/auth/email-verifications`, `/auth/login`, `/auth/logout`, `/api/me` | Verified email/password product identity, secure cookie sessions; product API keys for clients. External OAuth not required. |
| `/api/workspaces`, `/api/workspaces/{w}/projects` | Initially one personal Workspace; Projects index and creation. Team administration waits for product need. |
| `/api/projects/{p}`, `/api/projects/{p}/versions`, `/api/projects/{p}/environment-builds` | Project settings/current version, immutable input revisions, cache/build readiness and diagnostics. Builds are projections of Snapshot/Job state, not a second environment lifecycle. |
| `/api/workspaces/{w}/connections`, `/api/connections/{c}` | Named scoped external Connections and safe metadata. Manual connect/replace/disconnect. Credential input is write-only. |
| `/api/connections/{c}/validations`, `/api/connections/{c}/capabilities` | Async validation and timestamped actual capabilities/models. Distinguish unverified/revoked/unsupported. |
| `/api/workspaces/{w}/sessions?project_id=...&activity=...`, `/api/sessions/{s}` | Session create/list/detail/settings, pinned Project version and effective Harness, activity/compute/delivery projections. Create may accept an initial Message atomically. |
| `/api/sessions/{s}/messages`, `/api/sessions/{s}/turns`, `/api/turns/{t}` | Conversation/history and accepted work requests. Sending a Message optionally queues a Turn; response contains Message/Turn IDs and event watermark. |
| `/api/turns/{t}/cancellations`, `/api/turns/{t}/retries` | Explicit cancel intent or new retry Turn. Preparation retries and unknown-outcome restrictions follow the domain protocol. |
| `/api/sessions/{s}/events?after=...` | SSE or paged committed event replay, one Session sequence independent of runtime lease. |
| `/api/sessions/{s}/archive`, `/api/sessions/{s}/close` | Product lifecycle operations distinct from turn cancellation and compute release. Closed remains readable until retention deletes it. |
| `/api/sessions/{s}/executor`, `/api/sessions/{s}/executor/activations`, `/api/sessions/{s}/executor/releases` | Current compute projection, explicit wake/release, last checkpoint and restore limitations. Do not expose Modal credentials or handles as UI identity. |
| `/api/sessions/{s}/files?path=...`, `/api/sessions/{s}/terminals` | Scoped live file and terminal operations, with content/Worktree generation preconditions. Attachment upload has private blob ownership, limits, and explicit path destination. |
| `/api/sessions/{s}/changes`, `/api/sessions/{s}/changesets`, `/api/changesets/{cs}/files`, `/api/changesets/{cs}/diff` | Live unsealed diff versus immutable capture; manifest/file/diff lazily loaded. Historical ChangeSets readable without compute. |
| `/api/changesets/{cs}/deliveries`, `/api/deliveries/{d}`, `/api/deliveries/{d}/retries`, `/api/deliveries/{d}/merge-requests` | Ship exact subject, observe/retry transport, and separately authorize merge. API returns server-calculated eligibility/reasons with freshness and subject pins. |
| `/api/sessions/{s}/delegations`, `/api/delegations/{g}`, `/api/delegations/{g}/result`, `/api/delegations/{g}/waits` | Spawn/discover child work; publish/query validated results; durable waits. Review role uses the same resource. |
| `/api/sessions/{s}/services`, `/api/services/{i}/activations`, `/api/services/{i}/logs`, `/api/services/{i}/preview-grants` | Service instances, desired lifecycle operations, bounded logs and authenticated preview. Declaration belongs in Project version or explicit Session override. |
| `/api/jobs/{j}` (safe owner-visible subset) | Status of accepted long operations. Worker claims/payloads and operational internals stay private; a Job ID is not the user's unit of work. |

Operations return typed resource views with stable IDs, versions, status/reason, authorized actions, and observed timestamps. Mutations use idempotency keys and request fingerprints; optimistic preconditions use entity version or content digest. Slow operations return accepted resource/Job references, never hold request latency hostage to sandbox provisioning. Errors have code/category/source/retry advice and safe text. Unknown outcome, invalid credentials, unsupported capabilities, stale subject, and capacity delay are distinct.

Cookie and product API-key authentication resolve the same principal and domain policy. Cookie mutations require CSRF defenses and origin checks; API keys have explicit scopes. Operator-only diagnostics use a separately authorized route scope, not a shared Basic dashboard. Do not retain hosted routes as a parallel product API. Runtime/preview/terminal grants are short-lived, narrowly scoped, revocable capabilities; clients cannot use them to perform Session business mutations.

The event stream supplies ordered facts; querying the resource supplies current actions/eligibility. A "retry" button must identify Turn, Delivery, restore, or validation explicitly. No resource handler guesses which legacy phase failed.

## 2. Console information architecture

Amp's [Threads](https://ampcode.com/docs/threads), [Projects](https://ampcode.com/docs/projects), and [Orbs](https://ampcode.com/docs/orbs) support this product organization. The following component boundaries and state rules are original SBX design.

| Surface | Product purpose | Server authority / component boundary |
| --- | --- | --- |
| Projects index/detail | Reuse codebase/environment/settings; list related Sessions and environment readiness. | `Project`/versions; feature module owns project editor and creation picker. |
| Sessions sidebar/list | Recent/pinned/archived work with project/author/activity filters. | Session list projection; URL holds filters; pins/order preferences may be browser-local. |
| New Session composer | Choose Project or no Project, official CLI/model, executor/resource defaults, then send task. | Server resolves effective inputs/Connection capabilities; UI displays actual options and setup gaps. |
| Conversation | Messages, attachments, follow-ups, output and explicit queued Turn order. | Message/Turn projections; drafts local, accepted input persisted immediately. |
| Activity | Tool/process observations and operational milestones without overwhelming chat. | Committed events; per-item expansion/loading local; no client-derived terminal verdict. |
| Changes | Live diff, capture state, immutable ChangeSet selection, review subject and Ship actions. | Worktree observation/ChangeSet/Delivery; lazy per-file diff. No direct duplicate PR state. |
| Files | Bounded browsing/read/upload/edit of current Worktree; historical saved files where captured. | Runtime-backed query via grants; absent compute gives wake/checkpoint choices. Editing preconditions are server-enforced. |
| Terminal | Shared session-local shell for inspection and user commands. | PTY belongs to lease; reconnect/replacement state explicit. Input capability separated from read. |
| Preview/Services | Declared services, readiness/logs/URLs and lifecycle operations. | Service instances; private preview origin/proxy, no Console cookies in untrusted app. |
| Child Sessions | Assignment, status, result, and navigation to full child conversation. | Delegation/result; review is a preset of this component, not a hosted-only island. |
| Connections | Manual add/replace/validate/disconnect; capabilities and safe errors. | Connection/credential version; submitted secret fields cleared immediately and never cached. |
| Settings/account | Verified email, password changes, product API keys, display preferences. | Identity for security; local preferences for theme/language/layout. |

Session detail is a conversation column plus selected work pane (Activity, Changes, Files, Terminal, Services/Preview, Children). Connection/setup prompts are actionable capability-specific messages, not mandatory Codex/OAuth onboarding. Project environment setup errors are visible before queued work starts. Do not expose Task/Agent/Run terms, namespace IDs, CLI transport choices, or API versions merely because implementation has them.

## 3. Browser state ownership

One typed API client and one resource-query/event store per application replace `api`/`prototype`/`hosted` API seams. This does not require a new frontend state framework; retain existing stack where it can satisfy the ownership rules.

- Server projections own Session/Turn/Delivery/delegation state and authorized actions. Client reducers consume committed event IDs once, update/invalidate the same query resources, and never "settle" work themselves.
- Initial detail/history response includes the event watermark. Replay begins after it; a gap or reducer mismatch triggers resync. Message parts have stable IDs so an event after reconnect cannot concatenate an already-seen cumulative provider fragment.
- Local cache is a disposable optimization scoped by user + Workspace + resource ID + schema version. Purge on logout/access loss; bound size/retention. Sensitive credentials and native auth state never enter localStorage/IndexedDB. Session content caching policy can be disabled per product privacy settings.
- Optimistic submission uses a client request ID tied to server dedupe. Accepted IDs replace draft state; network uncertainty retries the same key. UI does not start another Turn just because an SSE connection dropped.
- Expensive diffs/files are paged/lazy; terminal output/service logs use bounded live channels, not persisted into the global conversation reducer.
- Server-provided merge eligibility is displayed with exact subject and stale/blocked reasons. Clicking merge sends the subject/version pin; the server revalidates. UI approval badges alone cannot authorize a merge.

## 4. Extension boundaries

Execution presets may simplify provider/model/effort choices but only select verified official CLI settings. Project repository instructions and native skills/MCP remain provider capabilities. A future coordinator view is a Session role and child navigator; a future scheduled-work editor writes an Automation definition targeting ordinary Messages/Turns. Neither introduces a second Console state architecture.

Final visual styling, Amp brand imitation, mobile voice, billing, multiplayer editing, and a plugin marketplace are outside this proposal. Preserve current accessibility, responsive layout, localization/theme, and error recovery behavior during later UI replacement.
