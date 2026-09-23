---
title: Artifacts and handoffs
description: Snapshot an agent's workspace into a durable, checksum-verified package and continue the work in another agent.
---

An **artifact** is a durable snapshot of an agent's repository workspace. It
lives in the control plane's artifact store, not in the sandbox, so it can be
downloaded and handed to another agent long after the producing agent is
closed.

A **handoff** starts an agent from someone else's work: an artifact, an exact
commit, or a pull request ref. Source code moves between agents only this way —
as a verified package or a pinned commit, never as text pasted into a prompt.

Artifacts and handoffs need a [workspace](/guides/repositories/): the producer
must have been created with `workspace`, and the consumer must declare the same
repository.

## Take a snapshot

`POST /v1/agents/{id}/artifacts` snapshots the workspace while the sandbox is
still alive. The agent must be `idle` on a live sandbox. Both body fields are
optional:

| Field | Meaning |
| --- | --- |
| `run_id` | The run the snapshot belongs to (default: the agent's latest run). The run record gets an `artifact://<artifact_id>` entry in `artifact_refs`. |
| `test_command` | A shell command to run in the workspace first. Its exit code is recorded in the manifest's `tests`. |

```bash
curl -X POST "$SBX_BASE_URL/v1/agents/$AGENT_ID/artifacts" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"test_command": "npm test"}'
```

The response (`201`) is `{"artifact": {...}}` with the manifest described
below. Each call creates a new artifact, so don't blindly retry a request that
timed out — list the agent's artifacts first.

## What an artifact contains

An artifact has format `patch` and these members:

| Member | Contents |
| --- | --- |
| `manifest.json` | The manifest: ids, shas, producer, per-file checksums, tests, payload checksums. |
| `patch.diff` | The full binary diff of the workspace against the checkout commit, including untracked files. |
| `repo.bundle` | A git bundle of the new commits (`HEAD ^base`). Present when the worktree is clean and the head moved, so a consumer gets the exact commits. |
| `files/<path>` | The raw bytes of every collected file. |

Manifest fields: `artifact_id`, `format`, `base_sha` (the producer's checkout
commit — the anchor every handoff is verified against), `head_sha`, `repo`,
`created_at`, `producer` (`agent_id`, `run_id`), `files` (`path`, `sha256`,
`size`), `tests` (`command`, `exit_code`), `payloads` (member → sha256) and
`warnings`.

What is collected is deliberately narrow:

- Only files git would transfer: tracked files plus untracked files that are
  not ignored. Build output covered by `.gitignore` is left out.
- Git internals, credential and key material (`.env*`, `*.pem`, `*.key`,
  `auth.json`, `credentials*`, SSH/GPG keys …), provider configuration
  directories and the runner's own bookkeeping are refused before they are
  read. Symlinks are not followed.
- If any collected file contains the agent's account credential or another
  sandbox secret, the whole snapshot fails with `409 artifact_secret` and
  nothing is stored.

Every member's sha256 is checked when it is written and again whenever it is
read, including downloads. A corrupted member is reported as
`artifact_invalid`; bad bytes are never served.

## Find and download artifacts

| Request | Returns |
| --- | --- |
| `GET /v1/artifacts` | `{"artifacts": [...]}`; filter with `?agent_id=`. |
| `GET /v1/artifacts/{artifactId}` | `{"artifact": {...}}`. |
| `GET /v1/artifacts/{artifactId}/download?member=…` | The member's bytes. `member` is `patch.diff` (default), `repo.bundle`, `manifest.json` or `files/<path>`. |

Downloads work after the producing agent is closed. That is also how you get
work out of an isolated sandbox: download `repo.bundle` and push the exact head
yourself.

```bash
curl -fsS "$SBX_BASE_URL/v1/artifacts/$ARTIFACT_ID/download?member=repo.bundle" \
  -H "Authorization: Bearer $SBX_API_KEY" -o work.bundle
git fetch work.bundle HEAD && git log -1 FETCH_HEAD
```

## Hand off work

There are three handoff sources. Send exactly one:

| Source | What happens |
| --- | --- |
| `artifact_id` | Applies a stored artifact. |
| `head_sha` | Checks out an exact commit. It must be reachable in the repository and descend from the declared `base_sha`. |
| `pull_request` | Fetches `ref` (`refs/pull/<n>/head`, `pull/<n>/head` or a branch) and requires it to resolve to exactly `head_sha`. |

### At create time

Declare the same `workspace` and add `handoff`. A `handoff` without
`workspace` is rejected with `400 workspace_invalid`, and a referenced
artifact must exist.

```json
{
  "prompt": { "text": "Review the change and extend the test coverage" },
  "agent": { "provider": "devin" },
  "workspace": {
    "repo": "https://github.com/acme/api",
    "base_ref": "main",
    "base_sha": "4f2a9c1e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39"
  },
  "handoff": { "artifact_id": "art-095d12783161484f" }
}
```

### Into an existing agent

`POST /v1/agents/{id}/handoff` with the same `HandoffRef` body applies a
handoff to an agent that is `idle` on a live sandbox.

### How an artifact handoff is verified

The steps run in a fixed order, and the workspace is left untouched if any of
them fails:

1. Decode and validate the manifest (`artifact_invalid`).
2. The artifact's `repo` must match the workspace.
3. Payload checksums must match (`checksum_mismatch`).
4. The workspace head must equal the artifact's `base_sha` (`base_sha_mismatch`).
5. Apply: fetch `repo.bundle` and check out the exact `head_sha`, or
   `git apply` the patch and commit.
6. Re-check every file's sha256 (`checksum_mismatch`).

After a successful handoff the consumer's `checkout_sha` and `head_sha` are
the artifact's head. That makes chains work: the artifact B produces has as its
`base_sha` the `head_sha` of A's artifact.

## Example: implement, then review in another provider

```python
from examples.sbx_client import SbxClient

REPO = {
    "repo": "https://github.com/acme/api",
    "base_ref": "main",
    "base_sha": "4f2a9c1e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39",
}

with SbxClient() as client:
    first = client.create("Add input validation to POST /users", provider="codex", workspace=REPO)
    agent_a, run_a = first["agent"], first["run"]
    client.wait(agent_a["id"], run_a["id"], timeout_s=1800)

    artifact = client.create_artifact(agent_a["id"], test_command="pytest -q")
    print(artifact["artifact_id"], artifact["tests"])
    client.close_agent(agent_a["id"])  # the artifact outlives the sandbox

    second = client.create(
        "Review the change and add missing tests",
        provider="devin",
        workspace=REPO,
        handoff={"artifact_id": artifact["artifact_id"]},
    )
    agent_b, run_b = second["agent"], second["run"]
    final = client.wait(agent_b["id"], run_b["id"], timeout_s=1800)
    print(final["status"], (final.get("result") or {}).get("text", ""))

    patch = client.download_artifact(artifact["artifact_id"])  # patch.diff bytes
```

`client.apply_handoff(agent_id, artifact_id=…)` (or `head_sha=…`) applies a
handoff to an existing agent.

## Errors

| Code | Status | Meaning |
| --- | --- | --- |
| `not_found` | 404 | `GET` or download of an unknown artifact or member. |
| `artifact_not_found` | 404 | A handoff references an artifact that does not exist. |
| `artifact_invalid` | 409 | The manifest or a member is malformed or failed its checksum on read. |
| `checksum_mismatch` | 409 | A payload or an applied file does not match its recorded sha256. |
| `artifact_secret` | 409 | Credential-shaped content was found while collecting; nothing was stored. |
| `base_sha_mismatch` | 409 | The workspace head is not the artifact's `base_sha`, or a commit does not descend from the base. |
| `head_sha_mismatch` | 409 | A pull request ref or commit disagrees with its pinned head. |
| `workspace_invalid` | 400 | Not exactly one handoff source, or a handoff without a workspace. |
| `workspace_not_found` | 404 | The agent has no workspace. |
| `turn_in_progress` / `session_not_runnable` | 409 | The agent is running, closed, or has no live sandbox. |
