"""Modal Sandbox backend. Tests must not instantiate or call this class.

``import modal`` is deferred until a method runs so collecting tests cannot
open a Modal connection. Signatures match Modal SDK + P0 (SOR-28).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from control.backend import Process, SandboxHandle, SandboxPoll, SandboxSpec
from control.config import (
    CODEX_HOME,
    CODEX_SECRET_NAME,
    CPU,
    IDLE_TIMEOUT_S,
    MEMORY_MIB,
    MODAL_APP_NAME,
    RUNTIME_IMAGE_NAME,
    SANDBOX_TIMEOUT_S,
    WORK_DIR,
)


def _load_modal() -> Any:
    import modal

    return modal


def _resolve_image(modal: Any) -> Any:
    try:
        import runtime.image as runtime_image

        for name in ("image", "runtime_image", "IMAGE", "sbx_runtime"):
            obj = getattr(runtime_image, name, None)
            if obj is not None:
                return obj
    except ImportError:
        pass
    from_name = getattr(modal.Image, "from_name", None)
    if callable(from_name):
        return from_name(RUNTIME_IMAGE_NAME)
    return modal.Image.debian_slim(python_version="3.12")


def _iter_lines(stream: Any) -> Iterator[str]:
    for chunk in stream:
        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
        yield from text.splitlines()


class ModalProcess:
    """Line-oriented wrapper around Modal ``ContainerProcess``."""

    def __init__(self, proc: Any, *, sandbox_id: str, pid_file: str) -> None:
        self._proc = proc
        self._sandbox_id = sandbox_id
        self._pid_file = pid_file
        self.stdout = _iter_lines(proc.stdout)

    def wait(self) -> int:
        return int(self._proc.wait())

    def kill(self) -> None:
        modal = _load_modal()
        sb = modal.Sandbox.from_id(self._sandbox_id)
        killer = sb.exec(
            "bash",
            "-c",
            f"if [ -f {self._pid_file} ]; then "
            f"kill $(cat {self._pid_file}) 2>/dev/null || true; fi",
            bufsize=1,
        )
        _close_stdin(killer)
        killer.wait()


def _close_stdin(proc: Any) -> None:
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return
    stdin.write_eof()
    drain = getattr(stdin, "drain", None)
    if callable(drain):
        drain()


class ModalBackend:
    """Production ``SandboxBackend`` using Modal Sandboxes.

    Not exercised by ``make test``. Real verification is WP2-H (SOR-42).
    """

    def __init__(self, app_name: str | None = None) -> None:
        self._app_name = app_name or os.environ.get("SBX_MODAL_APP_NAME", MODAL_APP_NAME)

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        modal = _load_modal()
        tags = dict(spec.tags)
        auth_json = os.environ.get("CODEX_AUTH_JSON")
        if auth_json:
            secrets = [modal.Secret.from_dict({"CODEX_AUTH_JSON": auth_json})]
        else:
            secrets = [modal.Secret.from_name(CODEX_SECRET_NAME)]
        sb = modal.Sandbox.create(
            "sleep",
            "infinity",
            app=modal.App.lookup(self._app_name, create_if_missing=True),
            image=_resolve_image(modal),
            secrets=secrets,
            env={"CODEX_HOME": CODEX_HOME, "SBX_WORK": WORK_DIR},
            cpu=CPU,
            memory=MEMORY_MIB,
            timeout=SANDBOX_TIMEOUT_S,
            idle_timeout=IDLE_TIMEOUT_S,
            workdir=WORK_DIR,
            tags=tags,
        )
        return SandboxHandle(id=sb.object_id, root=Path(WORK_DIR), tags=tags)

    def exec(
        self,
        handle: SandboxHandle,
        argv: list[str],
        env: Mapping[str, str] | None = None,
    ) -> Process:
        modal = _load_modal()
        sb = modal.Sandbox.from_id(handle.id)
        pid_file = f"/tmp/sbx-exec-{uuid.uuid4().hex}.pid"
        wrapped = ["bash", "-c", f'echo $$ > {pid_file}; exec "$@"', "sbx-exec", *argv]
        if env:
            proc = sb.exec(*wrapped, bufsize=1, env=dict(env))
        else:
            proc = sb.exec(*wrapped, bufsize=1)
        _close_stdin(proc)
        return ModalProcess(proc, sandbox_id=handle.id, pid_file=pid_file)

    def terminate(self, handle: SandboxHandle) -> None:
        modal = _load_modal()
        sb = modal.Sandbox.from_id(handle.id)
        sb.terminate()

    def poll(self, handle: SandboxHandle) -> SandboxPoll:
        modal = _load_modal()
        sb = modal.Sandbox.from_id(handle.id)
        rc = sb.poll()
        alive = rc is None
        return SandboxPoll(alive=alive, active_processes=1 if alive else 0)

    def list(self, tags: Mapping[str, str] | None = None) -> list[SandboxHandle]:
        modal = _load_modal()
        app = modal.App.lookup(self._app_name, create_if_missing=True)
        app_id = getattr(app, "app_id", None)
        found: list[SandboxHandle] = []
        for sb in modal.Sandbox.list(app_id=app_id, tags=dict(tags) if tags else None):
            try:
                sb_tags = dict(sb.get_tags())
            except Exception:
                sb_tags = dict(tags) if tags else {}
            if tags and any(sb_tags.get(k) != v for k, v in tags.items()):
                continue
            found.append(SandboxHandle(id=sb.object_id, root=Path(WORK_DIR), tags=sb_tags))
        return found
