"""Lease-local PTY and declared service infrastructure; never a Harness/model loop."""

import os
import pty
import signal
import subprocess
import threading

from protocol.runtime import ProtocolError

from runtime.security.redaction import Redactor


class Processes:
    def __init__(self, runtime):
        self.runtime, self.items = runtime, {}

    def open(self, payload, service=False):
        key = payload["service_id" if service else "terminal_id"]
        if key in self.items:
            return {"process_id": key, "running": self.items[key]["process"].poll() is None}
        command = payload["command"]
        if not command or any(not isinstance(s, str) or len(s) > 4000 for s in command):
            raise ProtocolError("forbidden")
        master, slave = pty.openpty()
        home = self.runtime.root / "terminal-home"
        home.mkdir(mode=0o700, exist_ok=True)
        process = subprocess.Popen(
            command,
            cwd=self.runtime.worktree,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(home),
                "LANG": "C.UTF-8",
                "TERM": "xterm-256color",
            },
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        item = {
            "process": process,
            "fd": master,
            "buffer": bytearray(),
            "start": 0,
            "lock": threading.Lock(),
            "service": service,
            "port": payload.get("port"),
        }
        self.items[key] = item

        def read():
            try:
                while chunk := os.read(master, 4096):
                    with item["lock"]:
                        item["buffer"].extend(chunk)
                        overflow = len(item["buffer"]) - 65536
                        if overflow > 0:
                            del item["buffer"][:overflow]
                            item["start"] += overflow
            except OSError:
                pass

        threading.Thread(target=read, daemon=True).start()
        return {"process_id": key, "running": True, "lease_id": self.runtime.lease_id}

    def read(self, key, after=0):
        item = self.items.get(key)
        if not item:
            raise ProtocolError("not_found")
        with item["lock"]:
            raw = bytes(item["buffer"])
            start = item["start"]
        # Redact the entire retained buffer before slicing to avoid token boundary leaks.
        text = Redactor(self.runtime.supervisor.known_secrets).clean(raw.decode("utf-8", "replace"))
        return {
            "text": text,
            "cursor": start + len(raw),
            "reset": after < start,
            "running": item["process"].poll() is None,
            "exit_code": item["process"].poll(),
        }

    def write(self, key, content):
        item = self.items.get(key)
        if not item or item["service"] or len(content) > 8192:
            raise ProtocolError("forbidden")
        os.write(item["fd"], content.encode())
        return {"written": True}

    def close(self, key):
        item = self.items.get(key)
        if not item:
            return {"stopped": True}
        process = item["process"]
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        try:
            os.close(item["fd"])
        except OSError:
            pass
        return {"stopped": True}

    def has_writer(self):
        return any(not i["service"] and i["process"].poll() is None for i in self.items.values())
