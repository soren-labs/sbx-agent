---
title: Python SDK quickstart
description: Create and wait for a Task with sbx.sdk.SbxClient.
---

The SDK is part of the `sbx` package:

```python
from sbx.sdk import SbxClient

client = SbxClient()  # SBX_BASE_URL + SBX_API_KEY
created = client.tasks.create(
    "Fix the flaky date test",
    source={"repo": "https://github.com/acme/api"},
    delivery={"pull_request": {}},
)

task = client.tasks.wait(created.task.id)
print(task.status)
```

`TaskCreated` contains the high-level task plus the allocated agent and first
run. `tasks.wait()` returns the last observed Task; inspect its terminal
status rather than assuming success.

Use a context manager when the client is short-lived:

```python
with SbxClient() as client:
    created = client.tasks.create("Explain this repository")
    print(client.tasks.wait(created.task.id).status)
```

Next: [Task methods](/sdk/python/tasks/) and
[Delivery/review](/sdk/python/delivery/).
