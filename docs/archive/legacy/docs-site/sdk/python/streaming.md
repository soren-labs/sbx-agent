---
title: Streaming with Python
description: Watch run SSE events and resume after disconnects.
---

Task status is product-level state; run SSE is the live event stream.

```python
for event in client.runs.watch(created.agent.id, created.run.id):
    print(event.id, event.type, event.data)
```

`client.runs.watch()` reconnects with bounded retry/resume semantics. For a
specific lower-level recovery flow you can use `client.runs.resume(...)`.

Always keep a polling fallback through `client.tasks.get()` or
`client.runs.get()`: the durable record decides the final outcome even if the
last SSE frame was lost.

See [Streaming guide](/guides/streaming/) for event types and `Last-Event-ID`.
