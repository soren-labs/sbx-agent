# Phase One PR 2 evidence — real Session workbench and acceptance

All screenshots and the video were captured from real Google Chrome (Playwright, one throwaway
profile per test user) against the non-production staging stack on the build host: real
PostgreSQL, real Modal sandboxes, the real GitHub test repository `soren-labs/sbx-e2e-test` and
a real DeepSeek key through the generic `inference_api` Connection. No `/api` response was
intercepted or mocked. Secret inputs are masked fields; the one-time SBX API key is blurred in
the page before it exists and the capture refuses to run if the blur is not in effect; every key
created for evidence was revoked.

Automation: [`../acceptance/scripts/acceptance.mjs`](../acceptance/scripts/acceptance.mjs).
Machine-readable result: [`acceptance-result.json`](acceptance-result.json).

## New-user acceptance (one continuous run)

| Step | Evidence |
| --- | --- |
| Register, sign-in refused before verification, verify | [a01](a01-register-submitted.png), [a01](a01-login-before-verification.png), [a01](a01-email-verified.png) |
| Home with setup checklist | [a02](a02-home-setup-checklist.png) |
| Bind inference API key (DeepSeek), Modal, GitHub | [a03](a03-inference-form.png), [a04](a04-connections-ready.png) |
| Pick CLI + model, repository, task | [a05](a05-cli-and-model-picker.png), [a06](a06-repository-picker.png), [a07](a07-composer-ready.png) |
| Session starting → running with tool steps → step expanded mid-run | [a08](a08-session-starting-1.png), [a08](a08-session-running-tools.png), [a08](a08-session-running-step-expanded.png) |
| Turn finished, work group expanded | [a09](a09-session-turn1-finished.png), [a09](a09-session-work-expanded.png) |
| Follow-up in the same Session | [a10](a10-followup-running.png), [a10](a10-session-turn2.png) |
| Changes → ChangeSet → Delivery → real pull request | [a11](a11-changes-live.png), [a12](a12-changeset-ready.png), [a13](a13-delivery-pull-request.png) |
| SBX API key created (blurred), listed, revoke confirmed | [a14](a14-api-key-created-blurred.png), [a14](a14-api-key-listed.png), [a14](a14-api-key-revoke-confirm.png), [a14](a14-api-key-revoked.png) |
| Session after two Turns driven by the API key | [a15](a15-session-after-api-turns.png) |
| Mobile 390 px | [home](a16-mobile-home.png), [connections](a16-mobile-connections.png), [session](a16-mobile-session.png), [changes](a16-mobile-session-changes.png), [settings](a16-mobile-settings.png), [navigation](a16-mobile-navigation.png) |
| Second user: no access, refused without connections | [b02](b02-home-setup-checklist.png), [b03](b03-alice-session-not-found.png), [b04](b04-sessions-empty.png), [b05](b05-connections-empty.png), [b06](b06-session-refused-without-connections.png) |

API key gate (from `acceptance-result.json`): the key copied through the UI's Copy button was
given to a **separate Python SDK process**, which authenticated (`auth.via = api_key`), listed
harnesses and Sessions, and ran a real Turn in the Session; plain HTTP calls with the key
returned 200/202, without it 401, with an invalid key 401, and after revoke 401. The second user
received 404 on every one of the first user's resources (Session, events, messages, files,
ChangeSets, workspace lists, Connections, posting a message, using or deleting a Connection).

## Workbench behaviour on live Sessions

| Case | Evidence |
| --- | --- |
| Long multi-tool task, running with a step expanded (Claude Code) | [running](cli-claudecode-running.png) |
| Follow-up Turn, then the Changes panel beside the conversation | [follow-up](cli-claudecode-followup.png), [changes](cli-claudecode-changes.png) |
| Five Turns in one Session; follow-up queued while running | [running](f1-followup-running.png), [finished](f2-multi-turn-finished.png) |
| Reader scrolled into history is not pulled down; Jump to latest | [history](f3-reading-history-jump-button.png) |
| Long output bounded with "show N more lines" | [output](f4-long-output-bounded.png) |
| Activity timeline in words | [timeline](f5-activity-timeline.png) |
| Stop a running Turn → cancelled → Retry | [running](s1-running-before-stop.png), [cancelled](s2-cancelled.png), [retry](s3-retry-started.png) |
| Provider failure (unknown model) explained with Retry | [failure](e1-provider-failure.png) |
| Network loss → reconnecting banner → resumed with every step | [offline](r1-offline-reconnecting.png), [resumed](r2-resumed-finished.png) |
| Other CLIs with live events | [OpenCode running](cli-opencode-running.png), [OpenCode](cli-opencode-finished.png), [Codex](cli-codex-finished.png), [Grok Build running](cli-grokbuild-running.png), [Grok Build](cli-grokbuild-finished.png), [Command Code](cli-commandcode-finished.png) |
| Mobile conversation / workspace | [conversation](f6-mobile-conversation.png), [workspace](f7-mobile-workspace.png) |
| Light theme | [session + changes](light-session-changes.png), [home](light-home.png) |

Video: [`real-session-walkthrough.mp4`](real-session-walkthrough.mp4) — 66 s, a real Claude Code
Session on Modal: task submitted, sandbox starting, tool steps streaming, a step expanded while
running, completion, a follow-up Turn, then the Changes panel.
