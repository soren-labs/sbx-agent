---
title: Troubleshooting
description: Common first-run failures and what to do about them.
---

| Symptom | Cause and action |
| --- | --- |
| Process exits: `SBX_DATABASE_URL, SBX_VAULT_KEYS and SBX_RUNTIME_MASTER_KEY are required` | Export all three before `serve`, `worker` or `migrate`. |
| `GET /readyz` returns `503` | The control plane cannot read PostgreSQL; check `SBX_DATABASE_URL`. |
| Cannot sign in after registering | Email verification is required. Without `SBX_RESEND_API_KEY` the link is in `$SBX_DATA_DIR/mail/*.json`. Tokens expire after 24 hours. |
| `429 rate_limited` on login | 10 failures per email in 15 minutes. Wait and retry. |
| `csrf_failed` in the Console | The browser origin is not in `SBX_ALLOWED_ORIGINS`, or the CSRF token is missing. Set the public URL and origins to the Console address. |
| `validation_failed` mentioning `Idempotency-Key` | Every mutation needs the header. The SDK and CLI add it. |
| Connection stays `unverified` | Validation is a background Job; make sure workers run (`serve` runs them). `reauth_required` means the provider rejected the credential: replace it. |
| Turn waits with `connection_required` or `credential_invalid` | Add or replace the inference API key, Modal or GitHub Connection. For `connection_required` with `inference_protocols`, add an endpoint for one of the listed protocols. |
| Turn waits with `waiting_capacity` | Connection capacity is in use, or the Session's cloud machine is running another Session; the Turn continues when it frees. |
| Cloud machine stays **Login pending** | Open the sign-in page and finish it before the code expires. For Codex and Grok Build, enter the one-time code. For Antigravity, paste the code the browser shows and choose **Send code**. The page updates by itself; if the code or link expired, choose **Log in again**. |
| Antigravity machine shows **Needs login** with the reason `code_rejected` ("The provider did not accept that code") | The pasted code was wrong, already used, or too old. Choose **Log in again** to get a new sign-in link, then paste the newest code the browser shows. |
| `invalid_transition`: "this login is not waiting for a code" from `sbx slots code` or `submit_code` | A code was sent to a device-code machine (Codex or Grok Build), a code was sent a second time after it was accepted, or no login was waiting for one. Only Antigravity asks for a pasted code, and only while its login waits for it. Check the state with `sbx slots show SLOT_ID`. |
| Antigravity login ends as expired | The sign-in link or pasted code was not used before the login window closed. Choose **Log in again** for a new link. |
| Grok Build or Antigravity machine shows **Needs login** after a Session | The provider rejected the stored login during a Turn, or the login was signed out. Choose **Log in again**, then send the Turn again. |
| Antigravity machine has no model picker | SBX could not read the list that `agy models` prints, so the machine works with the provider default only. Choose **Check login and refresh models**. If the list still cannot be read, Sessions use the default model. |
| Cloud machine shows **Needs login** after it worked | The provider rejected the stored login or it was signed out. Choose **Log in again**. |
| Cloud machine shows **Error** | The setup VM was lost or the Volume could not be saved. Choose **Check login**; if it persists, check the Modal Connection. |
| No models listed for a cloud machine | The CLI gave no catalog. Choose **Check login and refresh models**; Sessions can still run on the provider default. |
| `validation_failed` on `harness.model` or `harness.effort` | The model is not in the machine's catalog, or it does not list that effort. `details.supported` has the valid values. |
| `executor_unavailable` on Files, Terminal or Services | No live executor. Reads never wake compute; activate it from the Session. |
| Turn ended `outcome_unknown` | SBX could not prove the result. Inspect the Worktree, then acknowledge the Turn before retrying. See [Sessions and Turns](/guides/sessions-and-turns/). |
| Delivery blocked with `remote_head_changed` | The remote branch holds another head. Reconcile with a refresh; SBX never force-pushes. |
| Merge fails with `gate_blocked` | A required result is missing, stale or for a different `subject_digest`, checks are not green, or the head moved. Read the reasons in the response. |
| `409 connection_in_use` on disconnect | Live leases or Executions use the Connection. Release them first. |
| Preview grant fails | No preview origin is configured; preview is not available. |
| `invalid_cursor` on events | The cursor is ahead of the journal or `after` and `Last-Event-ID` disagree. Fetch a fresh snapshot and resume from its watermark. |
