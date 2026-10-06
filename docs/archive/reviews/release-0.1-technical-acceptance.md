# Release 0.1 Technical Acceptance — 2026-09-16

Candidate reviewed initially: `5e5aa7d3bf266d82b5b852195e4fbf2999cd724f`.

## Method

The acceptance was performed from a fresh detached clone with an isolated
Modal namespace (`sbx-control-ta01`, six `sbx-ta01-*` Dicts, deployment-scoped
Secrets/images) rather than reusing the Release Gate stores. The goal was to
behave like an unrelated self-hosting user following the README and the public
`examples/sbx_client.py` SDK.

## Blocking finding and fix

Fresh onboarding imported account metadata + credential blobs into the
accounts Dict, but `sbx deploy` did not materialize the referenced
`sbx-ta01-acct-<id>` Modal Secrets. `sbx doctor` therefore reported green while
`sbx smoke --provider devin` failed immediately with `runtime_error` / missing
Secret. This contradicted the onboarding module's documented contract that
`deploy` materializes imported account Secrets.

The candidate now materializes deployment-managed account credential blobs
before image/app deployment, refreshes them on later deploy/upgrade, leaves
custom/externally-managed Secret names untouched, and makes `sbx doctor` fail
when any account record references a missing Secret.

Real re-test after the fix: 8 imported account Secrets were materialized in the
TA namespace; `sbx doctor` reported all referenced Secrets present; the same
Devin smoke changed from ERROR to FINISHED in 33 s.

## Real self-host evidence

- Fresh `sbx init` / `sbx deploy` / `sbx doctor`: PASS in isolated namespace.
- Pinned runtime builds: Devin 3000.10.21, Antigravity 1.2.3, Grok 1.0.24,
  OpenCode 1.18.29.
- Real smoke: Devin PASS, Grok PASS, Antigravity PASS.
- OpenCode: `CREDENTIAL_DEFERRED` in the current environment; the real turn
  returned `auth_invalid` / `Token refresh failed: 401`. This is external
  credential state, not the fresh-bootstrap defect.
- Codex: `CREDENTIAL_DEFERRED` from the pre-existing stale ChatGPT/Codex token;
  not re-claimed as a PASS on this exact candidate.
- Public SDK, real Grok: two-turn context PASS; second turn recalled the marker.
- New local process using only API key + `workflow_id`: recovery PASS.
- Antigravity `account_id=auto`: four sequential real agents landed on four
  distinct accounts and all FINISHED.
- Workspace/artifact chain: Grok producer created exact Git head + durable
  artifact; producer teardown did not remove it; Devin consumer applied the
  artifact and produced a second artifact whose `base_sha` exactly matched the
  producer's `head_sha`.
- Local client process was interrupted while remote state remained durable;
  a fresh process recovered all three workflow agents/runs and artifacts.
- `sbx upgrade`: PASS; all six durable Dict key counts were preserved. A fresh
  post-upgrade process recovered the same workflow and artifact lineage.
- Post-upgrade `sbx doctor`: PASS.
- Scoped workflow cleanup: PASS; live Sandbox count returned to zero.
- Final deterministic gate after the bootstrap fix: `make lint` PASS;
  unit/integration **1363 passed, 1 skipped**; Playwright **11/11 passed**.
  One pre-existing local-process timing assertion flaked once at 5.0 s vs a
  `<3 s` threshold, then passed immediately in isolation and in the complete
  green rerun.
- Tracked-file secret-shape scan: only intentional test canaries/documented
  example keys; no real credential material.
- `sbx uninstall`: PASS. Default uninstall stopped the TA app with zero live
  Sandboxes while preserving all six Dicts and eleven TA Secrets. A subsequent
  `--purge-data --purge-credentials` removed all six Dicts, all eleven Secrets,
  and local bootstrap/basic/deploy state; no TA Sandbox remained.

## Observations / limitations

- `control.onboarding verify --probe sandbox` validates runner initialization,
  not a real provider request. An expired OAuth credential may therefore pass
  that probe and still fail a real turn with `auth_invalid`. The real turn is
  authoritative for release support evidence.
- One consumer `create_artifact` HTTP call experienced a long client-side wait;
  durable evidence showed the artifact and run-ledger reference had completed.
  Server logs show artifact creation can take tens of seconds. This was not
  reproduced as a deterministic correctness failure; client interruption did
  not lose state.
- OpenCode and Codex require refreshed interactive credentials before their
  exact-candidate real lanes can be re-run.

## Acceptance conclusion

The fresh-install account-Secret blocker discovered during this acceptance is
fixed in the candidate derived from `5e5aa7d`. The updated candidate passed lint, the full deterministic + Playwright
suite, tracked-file secret scan, upgrade/recovery, and uninstall/zero-orphan
verification. Technical acceptance is **PASS**. The release may proceed to
Owner Acceptance; no tag/publication is authorized until the Owner explicitly
records `RELEASE 0.1 OWNER PASS`.
