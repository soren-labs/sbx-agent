---
title: Python errors and retries
description: Handle canonical API errors separately from uncertain transport failures.
---

```python
from sbx.sdk import SbxApiError, SbxTransportError
```

## API errors

`SbxApiError` means the server responded with the canonical error body:

```python
try:
    client.tasks.create("Fix it", source={"repo": "bad://repo"})
except SbxApiError as exc:
    print(exc.status, exc.code, exc.retryable, exc.action, exc.retry_after)
```

Use the runtime-provided `retryable`, `action` and `retry_after` hints rather
than guessing from the HTTP status alone.

## Transport errors

`SbxTransportError` means the bounded HTTP exchange failed (timeout, drop,
refused connection). The mutation may already have happened server-side.
Inspect `exc.check` and read durable state before retrying an uncertain
mutation.

Automatic idempotency protects the normal SDK mutation path; see
[Idempotency](/api/idempotency/).
