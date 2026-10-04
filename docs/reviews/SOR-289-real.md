# SOR-289 real email gate

Base: `hosted-alpha/sor-288-alpha`, `01c7b1a` (frozen architecture round).

Production selection now uses the Resend adapter through the existing EmailSender
interface. The verified sender domain is `sbx-agent.com`. A dedicated real Resend
receiving inbox was provisioned outside the repository; no production DNS was
changed. The acceptance gate consumed inbound mail, not the send payload or a
mock outbox.

Real gate PASS: domain/key preflight; fresh registration; actual received OTP;
429 cooldown; resend after cooldown; invalidation of the earlier challenge;
verification; password setup; logout and later password login. A real Resend
request with a deliberately invalid placeholder key returned the bounded
`auth_unavailable` 503. No codes, passwords, keys or provider bodies were printed.

`make lint` PASS. `make test`: 3,416 passed, 14 skipped; additional adapter checks:
15 passed. Skips are existing optional PostgreSQL/browser checks. The code adds
no network dependency to the normal suite. Existing auth/HTTP mocks remain green.

Acceptance helper: `deploy/hosted/gates/email.py`. It runs in an isolated process
and temporary auth store, with real provider credentials held only in memory.
Production deployment is deferred to SOR-293. Frozen contracts are unchanged.

Limitations: mailbox delivery is proven through Resend's real inbound service;
inbox availability for unrelated recipient providers is outside this gate. The
acceptance inbox API is private beta and is not a product dependency.
