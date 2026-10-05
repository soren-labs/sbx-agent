"""Runtime WS enrollment; socket registry is a disposable transport hint only."""

import hmac

from fastapi import WebSocket

from control.runtime_client.grants import runtime_token


class RuntimeIngress:
    def __init__(self, uow, master):
        self.uow, self.master = uow, master
        self.sockets = {}

    async def connect(self, socket: WebSocket):
        await socket.accept()
        hello = await socket.receive_json()
        if hello.get("type") != "hello":
            await socket.close(code=1008)
            return
        payload = hello["payload"]
        with self.uow.transaction() as repo:
            lease = repo.one("SELECT * FROM executor_leases WHERE id=%s", (payload["lease_id"],))
        valid = (
            lease
            and lease["state"] in {"allocating", "ready"}
            and lease["generation"] == payload["lease_generation"]
            and lease["session_id"] == payload["session_id"]
            and payload["protocol_major"] == 1
        )
        expected = runtime_token(self.master, lease["id"], lease["generation"]) if valid else ""
        if not valid or not hmac.compare_digest(
            socket.headers.get("authorization", ""), "Bearer " + expected
        ):
            await socket.close(code=1008)
            return
        self.sockets[lease["id"]] = socket
        try:
            while True:
                await socket.receive_json()
        finally:
            self.sockets.pop(lease["id"], None)
