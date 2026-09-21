# SOR-170 prototype — guide page skeleton

> DESIGN ONLY. The shape every page under **Guides/** follows. Filled-in
> example: the Artifacts & Evidence guide.

---

# <Task name in verb form, e.g. "Snapshot and hand off work">

**Goal.** One sentence: what the reader can do after this guide.

**Before you start.** Bullet prerequisites — each a link, never a repeated
tutorial (e.g. "a deployed control plane — see
[Get Started → Deploy]"; "an `admin`-scoped key — see [Admin & API keys]").

## Steps

Numbered steps. Every command is copy-pasteable (`uv run sbx …`,
`curl -H "Authorization: Bearer $SBX_API_KEY" …`); every API call names its
endpoint and links its [API Reference] page. Code in Python uses
`examples/sbx_client.py` idioms; no invented helpers.

## Verify

The observable end state — a `GET` that returns the thing, a CLI output
line, a status field value. Always present, always checkable.

## Errors you may hit

Table of the error codes this task can produce (`{error:{code,…}}` or run
`RunError`), each row: code → why → fix → link. Codes must come from
`x-canonical` / contract files — never invented.

## Related

2–4 links: the adjacent guide, the concept page, the reference page.

---

## Filled example — Artifacts & Evidence (abridged)

**Goal.** Capture an agent's work as a durable, sha256-verified package and
hand it to another agent.

**Before you start.** A deployed control plane; an agent with a `workspace`
and a finished run ([Guide → Workspaces & Git]).

### Steps

1. `POST /v1/agents/{id}/artifacts` (optionally `run_id`, `test_command`) —
   collects `manifest.json` + `patch.diff` + `repo.bundle` + `files/*`
   while the sandbox is alive; the run record gains `artifact://<id>`.
2. `GET /v1/artifacts/{id}` — inspect the manifest (`base_sha`,
   `head_sha`, per-file sha256).
3. `GET /v1/artifacts/{id}/download?member=patch.diff` — pull bytes; read
   re-verifies checksums.
4. Hand it forward: `POST /v1/agents` with `handoff: {artifact_id}` (or
   `POST /v1/agents/{id}/handoff` on a live idle agent) — validation runs
   manifest → repo match → payload sha256 → base_sha anchor → apply →
   per-file re-check.

### Verify

`GET /v1/agents/{id}/workspace` on the consumer shows `checkout_sha` /
`head_sha` equal to the artifact's `head_sha`.

### Errors you may hit

| Code | Why | Fix |
| --- | --- | --- |
| `artifact_secret` | Secret-shaped content detected — refused before anything is persisted | Remove credential material from the worktree; retry |
| `checksum_mismatch` | Package bytes failed integrity | Re-create the artifact; do not hand-edit |
| `base_sha_mismatch` | Consumer's `base_sha` isn't the artifact's anchor | Declare the artifact producer's `checkout_sha` |
| `artifact_not_found` / `artifact_invalid` | Missing or malformed package | Check `GET /v1/artifacts`, re-create |

### Related

- [Guide → Review & handoff]
- [Concepts → Durable vs ephemeral state]
- [API Reference → artifacts]
