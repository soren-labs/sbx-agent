"""Outbound product email: Resend when configured, private file sink for local use."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx


class FileMailSink:
    """Local/dev delivery into a private directory (0700/0600). Never logs tokens."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    def send(self, to: str, subject: str, body: str) -> None:
        path = self.directory / f"{int(time.time() * 1000)}-{abs(hash(to)) % 10**8}.json"
        path.write_text(json.dumps({"to": to, "subject": subject, "body": body}))
        os.chmod(path, 0o600)

    def latest(self, to: str) -> dict | None:
        for path in sorted(self.directory.glob("*.json"), reverse=True):
            data = json.loads(path.read_text())
            if data["to"] == to:
                return data
        return None


class ResendMailer:
    def __init__(self, api_key: str, sender: str) -> None:
        self._key, self.sender = api_key, sender

    def send(self, to: str, subject: str, body: str) -> None:
        httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {self._key}"},
            json={"from": self.sender, "to": [to], "subject": subject, "text": body},
            timeout=20,
        ).raise_for_status()
