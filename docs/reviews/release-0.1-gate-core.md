# Release 0.1 — real Modal core-provider gate (SOR-68 core lanes)

Operator: Devin (gate author)
Worktree: `/home/zheng/ai-work/p21/rel01-gate-core`, branch
`release/0.1-gate-core`, reset to the exact `release/0.1-rc-deploy` HEAD
`5ea5fc2` ("Release 0.1 RC deploy: isolated RC namespace + remote
PYTHONPATH fix") before any testing.
Plane under test: the dedicated RC deployment only — Modal app
`sbx-control-release01-rc` (app id `ap-BrxZ12LH9oND05Boznp8lN`), endpoint
`https://sorenlab2026--sbx-control-release01-rc-fastapi-app.modal.run`,
RC namespace `sbx-rc-*` (SOR-107 record). Production `sbx-control` was
never touched. Auth: bootstrap key `key_bootstrap_2ba85f236e42`
(fingerprint only — no credential value was printed, logged, or
committed at any step).

Harness: `tests/e2e_modal/v1_core_gate.py` — per account: `/v1/me` +
admin scope check, account read, `/v1/accounts/{id}/verify` reprobe,
agent create pinned to the named account, run-1 queue state
(`CREATING`/`RUNNING`), terminal run truth, SSE canonical events +
`sbx.session_meta`, in-sandbox ground truth (session.json provider/
account/native id, `turns/*.json`, credential file mode 600, marker
file), follow-up/resume on the same native session, usage honesty
(`GET /v1/agents/{id}/usage` accumulates), cancel to durable
`CANCELLED` + agent recovery, cleanup (`DELETE` → `closed`), stale
follow-up refusal (`409 session_not_runnable`), durable run history,
zero leftover tagged sandboxes, and a credential leak scan over every
captured response, SSE stream, in-sandbox file, and the RC app log
tail. Failure diagnostics (provider stderr tail, `events.raw.jsonl`
tail, the turn doc) are masked before they are recorded.

## Verdicts

| Account | Verdict | Checks | Notes |
| --- | --- | --- | --- |
| `codex-1` | **CREDENTIAL_DEFERRED** (not a product fail) | 19, 0 failed | real turn ends honestly `auth_invalid` — stale ChatGPT token, noninteractive refresh fails |
| `devin-1` | **PASS** | 46, 0 failed | two turns on one native thread, cancel, honest usage, zero leaks |
| `opencode-1` | **PASS** | 46, 0 failed | initial runs exposed a shipped default-model defect (PRODUCT_FAIL, fixed on this branch); re-run passed on the corrected model |

## codex-1 — CREDENTIAL_DEFERRED

- Account readable, status `invalid`, `models=["gpt-5.6-luna"]`.
- `/v1/accounts/codex-1/verify` reprobe returned `active` (metadata
  shape OK), but the real run-1 terminated `ERROR` in 12.8 s with
  `{"code":"auth_invalid","source":"provider","retryable":false}` —
  "Your access token could not be refreshed. Please log out and sign
  in again." The failure is reported honestly, never as success.
- Host-side check (metadata only): the Codex access/id tokens are
  expired and the noninteractive refresh fails — same external
  limitation already recorded in `docs/reviews/SOR-107-rc-deploy.md`.
  Restoring this lane needs an interactive `codex login`, out of
  scope for this gate.
- Still verified: agent create + account pinning, run dispatch,
  structured terminal error, cleanup, stale follow-up refusal,
  durable run history, zero leftover sandboxes, app-log leak scan.

## devin-1 — PASS (46/46)

- Model `swe-2-high`, account `active`, slots 8.
- run-1 `FINISHED` in 27.3 s — usage `{input:15161, output:54}`;
  SSE opened with `sbx.session_meta` (provider+account correct),
  `thread.started`, `turn.completed`.
- run-2 `FINISHED` in 10.0 s on the **same native thread**
  (`ses_…` identical across runs) and recalled the run-1 marker —
  usage `{input:15268, cached_input:8192, output:51}`.
- `GET usage` honestly accumulates: `{input:30429, cached:8192,
  output:105}`, `cost_estimate_usd` 0.0029, `sandbox_seconds` 63.3.
- run-3 cancel → `CANCELLED`, agent recovered to `idle`.
- In-sandbox truth: session.json provider/account/native id match;
  credential file mode `600`; marker file present; no leaks in
  `events.jsonl`, `events.raw.jsonl`, `session.json`, `turns/1.json`.
- Teardown: `DELETE` → `closed`; follow-up refused
  `409 session_not_runnable`; run history still readable;
  0 leftover tagged sandboxes; RC app log tail (69.9 KB) leak-clean.

## opencode-1 — PASS (46/46) after one real PRODUCT_FAIL, fixed

### The PRODUCT_FAIL found by this gate

First two real runs failed `runtime_error`/`provider` with OpenCode's
native `UnknownError` ("Unexpected server error…", turn exit 2, zero
usage). Root cause: the shipped seed defaults
`PROVIDER_DEFAULT_MODELS["opencode"]` —
`("anthropic/claude-sonnet-4.5", "openai/gpt-5.3-codex")` — name model
ids that do not exist on the account's real auth channels (no
`anthropic` channel is configured, and the registry id is dashed
`claude-sonnet-4-5`; the `openai` channel only carries
`gpt-5.3-codex-spark`). `GET /v1/models` advertised them as available
and the model-less/default path always failed upstream. Host-side
credential metadata showed the account's live channel is OpenAI OAuth;
`openai/gpt-5.6-luna` was verified working on it.

### Fix on this branch

- `control/api_v1/bootstrap.py` — defaults corrected to
  `("openai/gpt-5.6-luna", "opencode/claude-sonnet-4-5")` (the two
  real auth channels: OpenAI OAuth + the OpenCode Zen API key).
- Regression coverage: the pinned assertion in
  `tests/unit/api_v1/test_bootstrap.py` updated to the corrected
  pair; mirror values fixed in `tests/fakes/mock_api.py`,
  `tests/unit/runner/test_opencode_{adapter,turn}.py`.
- Gate lane default in `v1_core_gate.py` updated to
  `openai/gpt-5.6-luna`.
- `61 passed` on the touched unit-test files; `make lint` clean.
- Known residual: the **deployed** RC account record still advertises
  the stale pair — seeded `models` refresh only at control-plane boot
  (`_upsert_seeded`) or via `SBX_OPENCODE_MODELS`; no PATCH endpoint
  exists. Any redeploy picks up the corrected defaults.

### Passing re-run (model `openai/gpt-5.6-luna`)

- run-1 `FINISHED` in 27.4 s — usage `{input:4971, cached_input:7168,
  output:66, reasoning:20}`; `sbx.session_meta` first SSE frame with
  provider+account; native session `ses_f57c8a73…`.
- run-2 `FINISHED` in 7.1 s on the **same native session**, recalled
  the marker — usage `{input:538, cached_input:5632, output:19,
  reasoning:12}`.
- `GET usage` accumulates: `{input:5509, cached:12800, output:85,
  reasoning:32}`, `cost_estimate_usd` 0.0027, `sandbox_seconds` 58.6.
- run-3 cancel → `CANCELLED`, agent recovered `idle`.
- In-sandbox: session.json provider/account/native id match;
  `.local/share/opencode/auth.json` mode `600`; marker file present;
  no leaks in events/session/turn files.
- Teardown: `closed`; stale follow-up `409 session_not_runnable`;
  run history readable; 0 leftover sandboxes; app logs leak-clean.
- Zen-channel note: `opencode/…` model ids resolve but this account
  has no Zen balance — that is account funding, not a product defect.

## Promotion decision

`docs/providers.md`: opencode moved **Preview → Experimental** — the
SOR-96 promotion bar (real Modal gate + ≥2-turn resume on a real
`auth.json`) is now met. It was not promoted on fixture/replay
evidence; the real account path passed first.

## Global results

- Leak scan: `me`, account, both SSE streams, all in-sandbox files,
  and the RC app log tail — 0 secret-shaped hits for every lane
  (JWT / `sk-` / `sbx_` key / env-watchlist patterns).
- Leftover sandboxes after cleanup: 0 on every lane.
- Per-run artifacts (ignored, not committed):
  `tests/e2e_modal/artifacts/v1_gate_{codex-1,devin-1,opencode-1}.json`.

## Limitations

- Codex lane needs an interactive `codex login` to refresh the real
  credential; deferred, not a defect.
- The RC plane still runs rc-deploy code; the corrected opencode
  defaults take effect on the next deploy/boot.
- `openai/gpt-5.6-luna` was the verified channel model; the second
  shipped default (`opencode/claude-sonnet-4-5`) resolves but is
  unfunded on this account.
