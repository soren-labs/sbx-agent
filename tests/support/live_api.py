"""A real HTTP SBX API for subprocess clients (MCP stdio, CLI): the same app as ApiStack.

uvicorn serves the composition root on an ephemeral 127.0.0.1 port, and a worker thread drives
Turns exactly as in the in-process SDK tests. Nothing here reaches a cloud account.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import httpx
import uvicorn

from tests.support.api import ApiStack


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveApi:
    def __init__(self, stack: ApiStack, *, idle_sleep: float = 0.05) -> None:
        self.stack = stack
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._idle_sleep = idle_sleep
        self._stop = threading.Event()
        self._server = uvicorn.Server(
            uvicorn.Config(
                stack.app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="off",
            )
        )
        self._threads = [
            threading.Thread(target=self._server.run, daemon=True),
            threading.Thread(
                target=stack.worker.run_forever,
                args=(self._stop,),
                kwargs={"idle_sleep": idle_sleep},
                daemon=True,
            ),
        ]

    def __enter__(self) -> LiveApi:
        for thread in self._threads:
            thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.base_url}/healthz", timeout=1).status_code == 200:
                    return self
            except httpx.TransportError:
                time.sleep(0.05)
        raise AssertionError("the live API did not start")

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._server.should_exit = True
        for thread in self._threads:
            thread.join(timeout=10)
        self.stack.shutdown()
