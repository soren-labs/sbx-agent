# Issue #229 evidence: project configuration, checks, services and delivery capability

All "after" material comes from `console/e2e/project-config.e2e.ts` against a real local control
plane (embedded PostgreSQL, local runtime, fake OpenCode CLI and fake connectors; no cloud account,
no real GitHub, no credentials on screen). `project-config-chain.mp4` is a **continuous Playwright
screen recording** of the three tests run in sequence (`SBX_E2E_VIDEO=1`, 1440×900), not a
keyframe replay. "Before" material is the audit's staging evidence at commit `9ac722c`
(`docs/reports/ui-audit-2026-10-15/AUD03|AUD06|AUD15|AUD17`).

| File | Shows |
| --- | --- |
| `shots/aud03-aud06-publish-dialog.png` | Publish dialog for a fully populated spec: kept settings (effort, env, delivery policy, unknown field), exact argv chips for `smoke=python3 -c "print(123)"`, and the field-level change list |
| `shots/aud17-declare-service.png` | Declaring a service in the New project form, with its argv |
| `shots/aud17-service-running-logs.png` | Declared service started from the Services tab, its log line, Start disabled while running |
| `shots/aud17-service-stopped.png` | The same service after Stop |
| `shots/aud17-projectless-empty.png` | Services empty state for a Session without a Project |
| `shots/aud15-projectless-delivery.png` | A ready ChangeSet of a Session without a repository: reason + Download patch, no PR form |

## Reproductions on base `018a0e0`

AUD03 — the publish payload the base Console built (`repository`/`checks`/`defaults` only, provider
hardcoded to `opencode`) applied over a fully populated spec changes 34 field paths, including
`ship_policy.draft` (false → true), the required review count (2 → 1), the dropped `TestResult`
requirement, `ship_policy.transport`, every service field, all environment variables and
`defaults.harness.effort`/`provider_id`.

AUD06 — base `parseChecks` executed with the same argv the runtime receives:

```
{"line":"smoke=python3 -c \"print(123)\"","argv":["python3","-c","\"print(123)\""],"output":""}
{"line":"spaced=python3 -c \"import sys; print(sys.argv[1])\" \"dir with space/f.txt\"","argv":["python3","-c","\"import","sys;","print(sys.argv[1])\"","\"dir","with","space/f.txt\""],"output":"exit 1: SyntaxError: unterminated string literal (detected at line 1)"}
```

`tests/integration/api/test_project_config.py` fails 11 of its 22 cases with base `control/`
(full-spec preservation, unknown/secret fields, empty arguments, the argv vectors as real checks,
delivery capability, patch export, service source/logs).
