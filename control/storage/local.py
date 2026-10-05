"""Private immutable object storage. Content hash possession is not authorization."""

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from control.domain.errors import DomainError


class LocalObjects:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def put(self, workspace, content):
        folder = self.root / workspace
        folder.mkdir(mode=0o700, exist_ok=True)
        key = uuid4().hex + "-" + hashlib.sha256(content).hexdigest()
        fd = os.open(folder / key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return key

    def get(self, workspace, key):
        if not workspace.startswith("wsp_") or "/" in key or ".." in key:
            raise DomainError("not_found")
        try:
            content = (self.root / workspace / key).read_bytes()
        except OSError:
            raise DomainError("not_found") from None
        if hashlib.sha256(content).hexdigest() != key.split("-", 1)[1]:
            raise DomainError("capture_failed")
        return content
