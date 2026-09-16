# SOR-107 — Release 0.1 isolated RC deployment record

Operator: Devin (RC deploy operator/fixer)
Base: `release/0.1-rc` reviewed HEAD `5061e1d`
("Release 0.1 RC review: independent PASS — no blocker, audit note"),
executed on branch `release/0.1-rc-deploy` in the dedicated worktree.
Result: **RC deployed and smoke-verified** on an isolated namespace; the
existing production/dev `sbx-control` deployment was never stopped,
replaced, purged, or reused. The RC app is left running for subsequent
real gates.

## Deployed RC namespace (exact names)

| Kind | Name |
| --- | --- |
| Modal app | `sbx-control-release01-rc` |
| Endpoint (sanitized) | `https://sorenlab2026--sbx-control-release01-rc-fastapi-app.modal.run` |
| Sessions Dict | `sbx-rc-sessions` |
| Runs Dict | `sbx-rc-runs` |
| Accounts Dict | `sbx-rc-accounts` |
| Workflows Dict | `sbx-rc-workflows` |
| Artifacts Dict | `sbx-rc-artifacts` |
| Workspaces Dict | `sbx-rc-workspaces` |
| Codex auth Secret | `sbx-rc-codex-auth` |
| Basic auth Secret | `sbx-rc-basic-auth` |
| v1 bootstrap Secret | `sbx-rc-v1-bootstrap` |
| Account Secret prefix | `sbx-rc-acct-` (seeded: `sbx-rc-acct-devin-1`, `sbx-rc-acct-antigravity-1`, `sbx-rc-acct-grok-1`, `sbx-rc-acct-opencode-1`; codex uses the shared Codex Secret) |
| Runtime images | `sbx-rc-runtime`, `sbx-rc-runtime-devin`, `sbx-rc-runtime-antigravity`, `sbx-rc-runtime-grok`, `sbx-rc-runtime-opencode` |

Bootstrap API key confirmed by fingerprint only: `key_bootstrap_2ba85f236e42`
(sha256 prefix `2ba85f236e42`, file mode 0600). No credential contents were
printed, logged, or committed at any step.

## Public-alpha path executed (isolated HOME/XDG)

`HOME=/home/zheng/ai-work/p21/rc-home`, env stripped (`env -i`), Modal
profile `sorenlab2026`.

| Step | Result |
| --- | --- |
| `sbx init` | config written under the isolated HOME |
| `sbx config` | resolves to the full RC namespace above |
| Exact pinned image builds | all 5 named RC images published — python 3.12, node 22, `@openai/codex@0.153.0`, devin `3000.10.21`, antigravity `1.2.3`, grok `1.0.24`, opencode `1.18.29` |
| Provider/account prep | RC Secrets created through the product credential-store path (`collect_credential_blob` → `Secret.objects.create`); contents never printed |
| `sbx deploy` | app deployed; `/v1/me` answered 200 |
| `sbx doctor` | all checks OK: 3 Secrets, 6 Dicts, bootstrap key fingerprint, api-auth scopes `['agents','admin']`, provider view, app reachable, cleanup |
| Minimal smoke | **grok: `run-1 FINISHED` in 13.5 s, `result_text = "sbx-ok"`** |
| Control restart/redeploy | `sbx deploy` re-run — clean redeploy, `/v1/me` 200 |
| Durable-state check | all 6 RC Dicts readable after redeploy |
| Upgrade seam | `sbx upgrade 0.1.0 → 0.1.0`: `sbx-rc-sessions` 7 keys, `sbx-rc-runs` 7 keys, `sbx-rc-accounts` 5 keys preserved; empty dicts (workflows/artifacts/workspaces) readable; no key loss |

## Known limitation: Codex provider credential (external, not a product defect)

Codex smoke reached provider execution (post-fix) but the turn ended
`auth_invalid`: the workspace's Codex access token is expired and the
noninteractive refresh fails ("Please log out and sign in again"). The
production credential-store copy (`credential/codex-1`) holds the same
stale token — verified by metadata only. Restoring Codex smoke requires an
interactive `codex login`, out of scope for this operator pass. The RC
`codex-1` account is marked `invalid` in `sbx-rc-accounts` (correct
health-feedback behavior); grok/devin/antigravity/opencode accounts are
`active`.

## Observation: wedged-container create latency

During one window the deployed function container wedged on a stale
ephemeral-Secret reference (Modal infra retries: `failed to resolve secret
st-…`), causing `POST /v1/agents` to exceed the smoke client's 30 s
timeout. Both queued creates still completed server-side and both runs
`FINISHED` with `sbx-ok`; the leftover idle agents were closed via
`DELETE /v1/agents/{id}` and the redeploy cycled the container. Worth a
follow-up issue: client-visible create latency under container churn.

## Bootstrap defects found and fixed on this branch (with tests)

1. **Namespace isolation was incomplete.** The bootstrap config could
   rename the app but Dict/Secret/image names and the remote function env
   still resolved to production contract names. Fixed end to end:
   `BootstrapConfig` fields + `deploy_env()`; `remote_env_overlay()`
   allowlist baked into `app.function(env=…)`; `control/app.py`,
   `control/accounts.py`, `control/api_v1/bootstrap.py`,
   `control/onboarding.py`, `control/backends/modal.py`,
   `control/modal_app.py`, `control/workspace.py`,
   `control/artifacts.py` resolve the configured names;
   `runtime/image.py::build_named_image` publishes named images
   (`SBX_IMAGE_APP` relocates the build app); `sbx/uninstall.py` sweeps
   only the configured account-Secret prefix.
2. **Remote Modal sandbox `PYTHONPATH` clobbered.** The control function's
   own `PYTHONPATH` was forwarded into sandbox execs via the shared-env
   list, shadowing the runtime image's `/opt/sbx`, so `python -m
   runtime.runner` failed (`runner init exited 1`). Fixed in
   `control/sandbox_io.py`: non-local handles pin `PYTHONPATH=/opt/sbx`;
   local handles keep the host value. Regression test
   `test_sandbox_env_modal_handle_pins_image_pythonpath`.
3. **Init stderr was swallowed.** `control/service.py` now surfaces Modal
   process stderr in runner-init errors — this is what made defect 2
   diagnosable.

Coverage: `tests/unit/sbx/test_rc_namespace.py`,
`tests/unit/control/test_rc_env_isolation.py`, plus updated bootstrap /
deploy / upgrade / uninstall / credential-scoping tests. Gates on this
branch: `ruff check` clean, `ruff format --check` clean,
`pytest tests/unit tests/integration` — **1356 passed, 1 skipped**.

## Production isolation verified

- `sbx-control` remains deployed under its original app id
  `ap-NmXudSSA47Qrqklqf7TnUF`.
- Production Dicts intact and readable: `sbx-accounts` (18 keys),
  `sbx-sessions` (68 keys), `h3-jobs` (0 keys).
- Production Secrets all present: `sbx-acct-grok-1`,
  `sbx-acct-antigravity-1`, `sbx-acct-devin-1`, `sbx-basic-auth`,
  `sbx-codex-auth`.
- One read of `credential/codex-1` was made from `sbx-accounts` and copied
  into `sbx-rc-codex-auth` through the credential-store path (metadata
  only, never printed); no production key was added, modified, or deleted.
- The RC app is intentionally left deployed for subsequent real gates.
