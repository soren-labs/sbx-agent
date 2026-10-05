"""Optional outbound TLS WS carrying the same operation/evidence frames as HTTP."""

import asyncio
import json

from protocol.runtime import OperationFrame
from websockets.asyncio.client import connect


async def connect_control(runtime, url):
    if not url.startswith("wss://") and not url.startswith("ws://127.0.0.1:"):
        raise ValueError("TLS required")
    while True:
        try:
            async with connect(
                url,
                additional_headers={"Authorization": "Bearer " + runtime.token},
                max_size=2_000_000,
            ) as socket:
                await socket.send(json.dumps({"type": "hello", "payload": runtime.hello()}))
                async for raw in socket:
                    frame = json.loads(raw)
                    kind = frame["type"]
                    if kind == "operation.request":
                        result = runtime.accept(OperationFrame.model_validate(frame["payload"]))
                    elif kind == "operation.status":
                        result = runtime.journal.get(frame["operation_id"])
                    elif kind == "events.batch":
                        result = {
                            "runtime_epoch": runtime.journal.epoch,
                            "events": runtime.journal.events(frame.get("after", 0)),
                        }
                    elif kind == "events.ack":
                        runtime.journal.ack(frame["local_seq"])
                        result = {"ack": frame["local_seq"]}
                    else:
                        result = {"error": "unsupported_capability"}
                    await socket.send(
                        json.dumps({"request_id": frame.get("request_id"), "payload": result})
                    )
        except (OSError, Exception):
            await asyncio.sleep(1)
