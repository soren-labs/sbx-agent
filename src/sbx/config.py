"""Client configuration: base URL + API key from env or a private config file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ENV = {"base_url": "SBX_BASE_URL", "api_key": "SBX_API_KEY", "workspace_id": "SBX_WORKSPACE_ID"}
DEFAULT_BASE_URL = "http://127.0.0.1:8800"


def config_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "sbx" / "client.json"


@dataclass
class ClientConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    workspace_id: str | None = None

    @classmethod
    def load(cls) -> ClientConfig:
        data: dict[str, str] = {}
        path = config_path()
        if path.exists():
            data = json.loads(path.read_text())
        for field, env in ENV.items():
            if os.environ.get(env):
                data[field] = os.environ[env]
        return cls(**{k: v for k, v in data.items() if k in ENV})

    def save(self) -> Path:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({k: v for k, v in self.__dict__.items() if v}, fh)
        return path


def reference() -> list[dict[str, str]]:
    return [
        {"field": "base_url", "environment": ENV["base_url"], "default": DEFAULT_BASE_URL},
        {
            "field": "api_key",
            "environment": ENV["api_key"],
            "default": "(none; stored 0600 by `sbx auth login`)",
        },
        {
            "field": "workspace_id",
            "environment": ENV["workspace_id"],
            "default": "(first workspace of the principal)",
        },
    ]
