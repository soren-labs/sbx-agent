---
title: Tasks with Python
description: Preflight, create, wait, follow up, cancel and retry through client.tasks.
---

## Preflight

```python
check = client.tasks.preflight(
    "Fix the flaky test",
    source={"repo": "https://github.com/acme/api"},
    delivery={"pull_request": {}},
)
```

Preflight is side-effect free.

## Create and wait

```python
created = client.tasks.create(
    "Fix the flaky test",
    source={"repo": "https://github.com/acme/api"},
)
task = client.tasks.wait(created.task.id, timeout_s=600)
```

## Follow up

```python
run = client.tasks.followup(created.task.id, "Add a regression test")
run = client.runs.wait(created.agent.id, run.id)
```

## Cancel or retry

```python
client.tasks.cancel(created.task.id)
client.tasks.retry(created.task.id, mode="run")
client.tasks.retry(created.task.id, mode="delivery")
```

## Read state

```python
detail = client.tasks.get(created.task.id)
runs = client.tasks.runs(created.task.id)
```

The SDK methods map directly to documented `/v1/tasks` operations; core task
workflows do not require `client.http` calls.
