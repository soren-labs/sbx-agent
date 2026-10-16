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
| `slots` | `overview`, `list`, `providers`, `add`, `get`, `update`, `login`, `verify`, `cancel_login`, `submit_code`, `logout`, `delete`, `models`, `wait` |
| `operations` | `get` |

Top-level helpers: `login`, `register`, `verify_email`, `me`, `create_api_key`,
`revoke_api_key`, `models` and the raw `get`, `post` and `request`.

`create_api_key(name, scopes=["write"])` returns the key's plaintext in `key`
(`scopes` is `["read"]`, `["write"]` or `["*"]`; omitted means `["*"]`). A lost
response is retried with the same `Idempotency-Key` and returns the same key.
If the secret can no longer be shown, it raises `SBXError` with code
`secret_unavailable`. `details["api_key"]["id"]` is the key to revoke.

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
official login to Codex, Grok Build or Antigravity. See
[Cloud machines](/guides/cloud-machines/) for the concept.

Start by listing the providers this deployment offers:

```python
from sbx import SBXClient

client = SBXClient("http://127.0.0.1:8800", api_key="sbx_key_REDACTED")

for provider in client.slots.providers():
    print(provider["provider_id"], provider["login_method"], provider["verification_host"])
```

`login_method` is `device_code` (Codex, Grok Build) or `authorization_code`
(Antigravity). The two need different callbacks in `wait`.

### Device code (Codex, Grok Build)

`on_code` is called once with the `login` object, which carries
`verification_url` and `user_code`:

```python
def show_code(login: dict) -> None:
    print(f"Open {login['verification_url']} and enter {login['user_code']}")


slot = client.slots.add("grok", label="Laptop", account_alias="team")
ready = client.slots.wait(slot["id"], deadline=1200, on_code=show_code)
if ready["status"] not in ("ready", "running"):
    raise SystemExit(f"slot is {ready['status']}")

catalog = client.slots.models(slot["id"])
model = catalog["models"][0]
result = client.sessions.create(
    harness={
        "provider_id": slot["provider"],
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
deadline.

### Pasted code (Antigravity)

Antigravity has no device code. The user signs in to Google in a browser, and the
browser then shows a code. `on_input` is called once per login attempt with the
`login` object, which carries `verification_url`. Return the code the user pasted
(a string) to submit it; return `None` to leave it. SBX does not store the code.

```python
from getpass import getpass


def ask_code(login: dict) -> str | None:
    print(f"Open {login['verification_url']}, sign in, then copy the code it shows.")
    return getpass("Code: ").strip() or None


slot = client.slots.add("antigravity", label="Work")
ready = client.slots.wait(slot["id"], deadline=1200, on_input=ask_code)
```

To send a code yourself, call `submit_code` while the login is waiting for it:

```python
client.slots.submit_code(slot["id"], "PASTED_CODE")
```

`submit_code` is refused with `invalid_transition` ("this login is not waiting
for a code") when the login is a device-code login or no code is wanted.

### Models

`slots.models(slot_id)` returns the catalog as the signed-in CLI reported it,
with its `source`. Antigravity's `source` is `agy models`. If the list could not
be read, the result is `{"status": "unavailable", "models": []}`; a Session on
that machine can then use only the provider default, with no model and no effort.

| Method | Effect |
| --- | --- |
| `slots.add(provider="codex", *, label, account_alias, compute_connection_id, volume_name)` | Create a Slot and start its official login. `provider` is `codex`, `grok` or `antigravity`. `volume_name` adopts an existing Volume. |
| `slots.get(slot_id)` | One Slot. |
| `slots.update(slot_id, **changes)` | Rename (`label`) or set `account_alias`. |
| `slots.login(slot_id)` | Run the official login again. |
| `slots.verify(slot_id)` | Re-check the stored login and refresh the model catalog. |
| `slots.wait(slot_id, *, deadline=1200.0, poll=2.0, on_code=None, on_input=None)` | Wait until no login is pending. `on_code(login)` fires for device codes; `on_input(login)` fires for pasted codes. |
| `slots.submit_code(slot_id, code)` | Send the code the browser showed after an Antigravity sign-in. The code is not stored. |
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

`examples/subscription_machines.py` adds a machine for one provider, completes
its login and prints its models. It can also start one Session on it.

```bash
SBX_BASE_URL=http://127.0.0.1:8800 SBX_API_KEY=sbx_key_YOUR_KEY \
  python examples/subscription_machines.py grok
```
