---
title: Idempotency
description: Safely retry task mutations after timeouts, disconnects and uncertain responses.
---

Mutating task endpoints accept the `Idempotency-Key` request header. Reusing
the same key for the same mutation replays the stored result instead of
applying the operation twice.

Use idempotency whenever a client might lose the response after the server has
already persisted the mutation.

## Python SDK

`SbxClient` enables automatic idempotency for mutations by default:

```python
created = client.tasks.create(
    "Fix the flaky test",
    idempotency_key="job-2026-09-27-001",
)
```

Pass `idempotency_key=False` for a call only when you intentionally do not
want a key.

## Transport failure rule

A timeout/drop is not proof that the server did nothing. When the SDK raises
`SbxTransportError`, inspect its `check` field and read the named durable
resource before deciding to retry. The SDK automatically handles the safe
keyed cases it owns.

API errors (`SbxApiError`) are different: the server responded and returned a
canonical error code/retry hint.
