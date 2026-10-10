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
| `slots` | `overview`, `list`, `providers`, `add`, `get`, `update`, `login`, `verify`, `cancel_login`, `logout`, `delete`, `models`, `wait` |
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

## Machine Slots

`client.slots` manages subscription Machine Slots, each one an independent
official Codex login. See [Cloud machines](/guides/cloud-machines/) for the
concept.

```python
from sbx import SBXClient

client = SBXClient("http://127.0.0.1:8800", api_key="sbx_key_REDACTED")


def show_code(login: dict) -> None:
    print(f"Open {login['verification_url']} and enter {login['user_code']}")


slot = client.slots.add("codex", label="Laptop", account_alias="team")
ready = client.slots.wait(slot["id"], deadline=1200, on_code=show_code)
if ready["status"] not in ("ready", "running"):
    raise SystemExit(f"slot is {ready['status']}")

model = client.slots.models(slot["id"])["models"][0]
result = client.sessions.create(
    harness={
        "provider_id": "codex",
        "model": model["id"],
        # Any id from model["reasoning"]["efforts"]; "default" may be None.
        **({"effort": e} if (e := model["reasoning"]["default"]) else {}),
    },
    executor={"backend": "modal"},
    inference={"mode": "subscription", "machine_slot_id": slot["id"]},
    message={"content": "Summarize this repository."},
)
print(result["session_id"])
```

`wait` returns the Slot whatever its status, so check `status` as shown. It
raises `SBXError` with code `timeout` if the login is still pending at the
deadline. `on_code` is called once with the `login` object, which carries
`verification_url` and `user_code`.

| Method | Effect |
| --- | --- |
| `slots.add(provider="codex", *, label, account_alias, compute_connection_id, volume_name)` | Create a Slot and start its official login. `volume_name` adopts an existing Volume. |
| `slots.get(slot_id)` | One Slot. |
| `slots.update(slot_id, **changes)` | Rename (`label`) or set `account_alias`. |
| `slots.login(slot_id)` | Run the official login again. |
| `slots.verify(slot_id)` | Re-check the stored login and refresh the model catalog. |
| `slots.wait(slot_id, *, deadline=1200.0, poll=2.0, on_code=None)` | Wait until no login is pending. |
| `slots.models(slot_id)` | The model catalog and reasoning efforts, or `{"status": "unavailable", "models": []}`. |
| `slots.cancel_login(slot_id)` | Stop the pending login. |
| `slots.logout(slot_id)` | Destroy the stored login. The Slot stays. |
| `slots.delete(slot_id, *, confirm)` | Delete the Slot and the Volume SBX created for it (an adopted Volume is kept). `confirm` repeats the Slot label. |
| `slots.overview()` | Slots with summary counts and the available providers. |
| `slots.list()` | The `items` of `overview()`. |
| `slots.providers()` | Subscription providers this deployment offers. |

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
