# Phase One acceptance report

Status: implemented and merged to `main`; **not released, tagged or deployed**. Ready for
independent pre-release review.

## Scope delivered

| Requirement | Result |
| --- | --- |
| Generic custom inference API keys, decoupled from the CLI | `inference_api` Connection: API key + default model + one base URL per protocol (`openai_chat`, `openai_responses`, `anthropic_messages`). DeepSeek preset; any compatible provider. |
| Five real official CLIs | OpenCode, Codex, Claude Code, Grok Build, Command Code — pinned versions in the Modal image, real execution, tool events, native session resume, usage. |
| Console, SDK, CLI, docs, i18n updated | Onboarding, Connections, Session creation, Session workbench, Settings; `connections.add_inference`, `harnesses()`, `--endpoint/--model/--harness`; specs and docs site; en + zh-CN. |
| Vendor credentials removed from new flows | OpenCode Zen and Codex `auth.json` cannot be created or selected; stored rows preserved by an additive migration and still serve Sessions pinned to them. |
| Console defects repaired | See "Defects found in the browser" below. |
| Final acceptance | New user → verify → bind Modal + GitHub + inference key → real coding Session → pull request → SBX API key from the UI used by a separate client → second-user isolation. Passed. |

## Merged pull requests

| PR | Merge commit | Content | Evidence |
| --- | --- | --- | --- |
| [#194](https://github.com/soren-labs/sbx-agent/pull/194) | `fb5e7ba` | Generic inference Connection, five CLI adapters, migration `0004`, SDK/CLI, Console connection and creation flows, specs/docs | [pr-01](pr-01/README.md) |
| [#195](https://github.com/soren-labs/sbx-agent/pull/195) | `b72adc4` | Session workbench, shell/onboarding/settings polish, reconnect handling, redaction and usage fixes, acceptance | [pr-02](pr-02/README.md) |

## Per-CLI verification

Real runs with DeepSeek `deepseek-flash`: two or more Turns per Session (tool use, then a
follow-up answered through the CLI's native session resume), on the local executor and in Modal
sandboxes through the API, and in Modal sandboxes through the Console in Chrome (both the
original and the workbench UI).

| Harness | Official CLI | Protocols accepted | Verified with | Works |
| --- | --- | --- | --- | --- |
| `opencode` | opencode-ai 1.18.35 | chat, anthropic, responses | chat (all three probed at the CLI level) | execution, tool events, incremental text, resume, usage |
| `codex` | @openai/codex 0.162.0 | responses | responses | execution, tool events, resume, usage |
| `claude` | @anthropic-ai/claude-code 2.1.295 | anthropic | anthropic | execution, tool events, resume, usage, auth-failure fast path |
| `grok` | @xai-official/grok 1.0.50 | chat | chat | execution, tool events, resume, usage |
| `commandcode` | command-code 1.79.2 | chat, anthropic, responses | chat (all three probed at the CLI level) | execution, tool events, incremental text, resume, usage |

Declared `unsupported` for all five: `interrupt` (Stop is a supervisor process-group stop),
`steer`, `interactive_approval`, `model_discovery` (models come from the Connection),
`credential_writeback`. Declared `unknown` (the CLI has it, SBX does not wire it): `mcp`,
`skills`, `attachments`, `effort_settings`, `account_portable_resume`.

Gaps and caveats:

- **Command Code**: its headless entrypoint requires *some* Command Code account key to be
  present even in its documented `--local-only` BYOK mode. The adapter supplies a fixed
  non-secret placeholder that local-only mode never transmits; no account is used. This needs an
  explicit product decision.
- **Codex** speaks only the Responses API; a chat-only provider cannot drive it (the Console says
  which protocol is missing).
- **Resume across sandboxes** relies on the checkpointed native state and a stable worktree path;
  it is covered by unit tests for OpenCode and by same-sandbox live runs for all five. A live
  cross-sandbox restore was not exercised for the four new CLIs.
- Text streams per completed block for Claude Code, Grok Build and Codex (that is what their
  CLIs emit); OpenCode and Command Code update incrementally.

## Acceptance run

One continuous Chrome run, new accounts, real services
([result JSON](pr-02/acceptance-result.json), [automation](acceptance/scripts/acceptance.mjs)):

| Gate | Result |
| --- | --- |
| Register → sign-in refused until verified → verify → sign in | pass |
| Bind inference key (three DeepSeek endpoints probed), Modal, GitHub | all `ready` |
| Real coding Session on `soren-labs/sbx-e2e-test` (Modal, Claude Code) with a follow-up | both Turns succeeded |
| ChangeSet → Delivery | real pull request opened; closed and branch deleted afterwards |
| API key created and copied in the UI, used by a separate Python SDK process | authenticated as `api_key`, ran a real Turn; 401 without key, with an invalid key, and after revoke |
| Second user without credentials | 404 on every resource of the first user; own Session refused with a clear message |
| Stop/Retry, provider failure, offline reconnect, five-Turn Session, reload, 390 px | pass |

## Defects found in the browser and fixed

- Session screen showed hardcoded token usage, cost and "Effort: low"; Connections showed
  invented repositories and a fake Modal progress list; the model picker offered models that do
  not exist. All replaced by server data.
- Workspace ignored the light theme; sign-in button white on white in light theme.
- Composer popovers opened under the top bar, making their first options unclickable.
- A dropped event stream froze the Session silently.
- Inference model ids and base URLs were redacted out of error messages as if they were secrets.
- Token usage semantics differed per CLI.
- Sessions list broken at 390 px; pages without gutters; navigation highlighted an invented
  "Review" queue for every Session route; setup steps overflowed their card.

## Checks on `main`

`make lint`, `make test` (384 passed, 1 skipped), `make console-check` (68 tests + build),
`make docs-check`: pass. GitHub checks green on both PRs.

## Remaining items for the reviewer

- Decide on the Command Code placeholder (above).
- `make mvp-acceptance` (the older scripted live check) was updated to BYOK inference but not
  re-run; the browser acceptance above supersedes it for this phase.
- `console/scripts/capture-ui.py` still intercepts `/api` with fixtures. It was not used for any
  evidence here and should not be treated as acceptance tooling.
- Not done by design: Phase Two CI/janitor/release work, any release, tag or production deploy.
