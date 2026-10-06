---
title: Python SDK
description: SBXClient namespaces, execute(), waiting helpers and error handling.
---

The `sbx` package ships a synchronous client over the `/api` surface, built on
`httpx`.

```python
from sbx import SBXClient

client = SBXClient("http://127.0.0.1:8800", api_key="sbx_key_YOUR_KEY")
print(client.me()["workspaces"])
```

`api_key` is sent as a Bearer token. To use a cookie session instead, call
`client.login(email, password)`; the client then sends the CSRF header for you.
`workspace_id` defaults to the first workspace of the principal.

## `execute`

```python
result = client.execute(
    "Add a CONTRIBUTING.md with one paragraph.",
    repository={"full_name": "owner/repo"},
    executor={"backend": "modal"},
    deadline=1800,
    on_event=lambda e: print(e["type"]),
)
session_id, turn = result["session_id"], result["turn"]
```

`execute` creates a Session (or sends a Message to `session_id`), then follows
committed events until the accepted Turn is terminal. It accepts `project_id`,
`project_version_id`, `harness`, `executor`, `repository`, `deadline` and
`on_event`. It contains no agent loop and never treats a process exit as the
outcome. It raises `OutcomeUnknown` if the Turn ended with `outcome_unknown` and
`DeadlineExceeded` if the deadline passes.

## Namespaces

| Namespace | Methods |
| --- | --- |
| `projects` | `list`, `create`, `get`, `publish` |
| `connections` | `list`, `add`, `get`, `replace`, `validate`, `disconnect`, `wait_health` |
| `sessions` | `create`, `list`, `get`, `send`, `close`, `archive`, `executor`, `activate`, `release`, `export`, `events`, `stream` |
| `messages` | `list` |
| `turns` | `list`, `get`, `cancel`, `retry`, `acknowledge`, `wait` |
| `changesets` | `list`, `capture`, `get`, `diff`, `apply`, `wait_ready` |
| `deliveries` | `request`, `get`, `retry`, `refresh`, `merge`, `wait` |
| `delegations` | `spawn`, `get`, `result`, `cancel`, `wait_result` |
| `operations` | `get` |

Top-level helpers: `login`, `register`, `verify_email`, `me`, `create_api_key`,
`models` and the raw `get`, `post` and `request`.

## Events

`sessions.events(session_id, after=0, follow=False)` replays the journal by
sequence using the JSON endpoint; with `follow=True` it polls until the
deadline. `sessions.stream(session_id, after=0)` reads the SSE stream.

## Waiting helpers

`turns.wait` (a Turn is terminal), `delegations.wait_result` (a validated
DelegationResult exists), `changesets.wait_ready`, `deliveries.wait` and
`connections.wait_health` are separate, explicit-deadline calls. Waiting for a
Turn and waiting for a result are never interchangeable.

## Errors and retries

API failures raise `SBXError` with `code`, `message`, `status`, `details`,
`retryable`, `request_id` and `action`. `OutcomeUnknown` and `DeadlineExceeded`
subclass it.

Network failures are retried with the same `Idempotency-Key` (3 retries with
backoff), and 5xx responses to mutations are retried the same way. After the
last attempt the client raises `SBXError` with code `transport_error`. Pass
`idempotency_key=` to `sessions.send` to control the key yourself.

## Example

`examples/unified_mvp.py` runs the whole flow: pick the preferred model,
execute a Turn, wait for the ChangeSet, spawn a review, deliver and print the
pull request URL.

```bash
SBX_BASE_URL=http://127.0.0.1:8800 SBX_API_KEY=sbx_key_YOUR_KEY \
  python examples/unified_mvp.py owner/repo
```
