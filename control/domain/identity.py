from dataclasses import dataclass
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@dataclass(frozen=True)
class Principal:
    user_id: str
    workspace_ids: tuple[str, ...]
    scopes: tuple[str, ...] = ("owner",)
    auth_epoch: int = 1
