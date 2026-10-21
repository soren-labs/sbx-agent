"""FAKE inference endpoint for the B2 acceptance only: answers SBX's Connection validation.

SBX needs one inference Connection to create a Session, and validates it with a minimal
generation request, the model list and two tiny reasoning probes. This responder returns
fixed canned JSON for exactly those calls on 127.0.0.1; it calls no model, forwards nothing,
and B2 runs no model Turn through it. Every request (method, path) is appended to a log so
the acceptance can show what it answered.

    python fake_inference.py PORT_FILE REQUEST_LOG
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "fake-validation-only"
COMPLETION = {
    "id": "fake-b2",
    "object": "chat.completion",
    "model": MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "FAKE"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "completion_tokens_details": {"reasoning_tokens": 0},
    },
}


class Handler(BaseHTTPRequestHandler):
    log_path: Path

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self) -> None:
        with self.log_path.open("a") as fh:  # method and path only, never headers (the key)
            fh.write(f"{self.command} {self.path}\n")

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        if self.path.rstrip("/").endswith("/v1/models"):
            self._reply(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._reply(404, {"error": {"message": "FAKE endpoint: not served"}})

    def do_POST(self) -> None:  # noqa: N802
        self._record()
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.rstrip("/").endswith("/v1/chat/completions"):
            self._reply(200, COMPLETION)
        else:
            self._reply(404, {"error": {"message": "FAKE endpoint: not served"}})

    def log_message(self, *args: object) -> None:
        return


def main() -> int:
    port_file, request_log = Path(sys.argv[1]), Path(sys.argv[2])
    Handler.log_path = request_log
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_file.write_text(str(server.server_address[1]))
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
