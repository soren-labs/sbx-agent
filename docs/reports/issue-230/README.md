# Issue #230 evidence: retry-safe API-key issuance, scoped keys, immediate revocation

All "after" material comes from `console/e2e/api-keys.e2e.ts` against a real local control plane
(embedded PostgreSQL, disposable database, throwaway test users). Every key shown was created by
the test and revoked before it ended. The one-time key value is blurred with a CSS rule whose
computed `filter: blur(...)` is asserted before each screenshot; the list only ever shows the
public `sbx_key_<prefix>…`. `api-keys-chain.mp4` is a **continuous Playwright screen recording**
of the two tests run in sequence (`SBX_E2E_VIDEO=1`, 1440×900), not a keyframe replay. "Before"
material is the audit's staging evidence at commit `9ac722c`
(`docs/reports/ui-audit-2026-10-15/AUD20|AUD21`).

| File | Shows |
| --- | --- |
| `shots/aud21-scope-picker.png` | Create key dialog: Read only / Read & write (default) / Full access, each with what it can and cannot do |
| `shots/aud21-reveal-read-blurred.png` | One-time reveal of a read-only key (value blurred) with its access stated |
| `shots/aud21-list-scopes.png` | Key list after reload: each key's scope pill; no plaintext on the page or in `GET /api/api-keys` |
| `shots/aud21-revoked.png` | Read and write keys revoked from the UI; both get `401` on their next read and write |
| `shots/aud20-outcome-unknown.png` | Create whose response was lost three times (server committed each attempt): the dialog says the key may exist, keeps the inputs locked to the same request and offers Try again |
| `shots/aud20-same-key-after-retry.png` | Try again returns the same single key (`replayed`), one row in the list |

## Reproduction on base `018a0e0`

SDK over the real API; `POST /api/api-keys` commits, then the response is lost twice and the SDK
retries with the same `Idempotency-Key`:

```
base 018a0e0: REPRO lost_responses=2 live_keys=3 returned_key_works=200
fix:          REPRO lost_responses=2 live_keys=1 returned_key_works=200
```

`tests/integration/api/test_api_key_issuance.py` fails 8 of its 10 cases with base
`control/`, `protocol/` and `src/` (lost-response retries, conflict detection, required
`Idempotency-Key`, replay window and purge, revoke erasing the replay copy, SDK scopes).
