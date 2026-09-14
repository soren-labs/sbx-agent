"""Modal Sandbox backend. Tests must not instantiate or call this class.

``import modal`` is deferred until a method runs so collecting tests cannot
open a Modal connection. Signatures match Modal SDK + P0 (SOR-28).
"""

from __future__ import annotations

import json
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
    DEVIN_IMAGE_NAME,
    IDLE_TIMEOUT_S,
    MEMORY_MIB,
    MODAL_APP_NAME,
    RUNTIME_IMAGE_NAME,
    SANDBOX_TIMEOUT_S,
    WORK_DIR,
)

_GONE_ERROR_NAMES = frozenset({"ConflictError", "NotFoundError"})

# SOR-74: ``SandboxSpec.tags["provider"] == "devin"`` selects the Devin image
# and per-account credential Secrets; anything else keeps the P1 Codex path.
DEVIN_PROVIDER = "devin"
_ACCOUNT_CREDENTIAL_ENV = "SBX_ACCOUNT_CREDENTIAL"
_ACCOUNT_CREDENTIAL_FILE_ENV = "SBX_ACCOUNT_CREDENTIAL_FILE"


def _load_modal() -> Any:
    import modal

    return modal


def _is_sandbox_gone(exc: BaseException) -> bool:
    """``Sandbox.terminate``/``from_id`` raise these when already shutting down."""
    return any(cls.__name__ in _GONE_ERROR_NAMES for cls in type(exc).mro())


def _codex_secrets(modal: Any) -> list[Any]:
    """Secret for every Sandbox ``create`` / ``exec``.

    Prefer an ephemeral ``from_dict`` when this process has ``CODEX_AUTH_JSON``
    (local uvicorn with ``SBX_BACKEND=modal``). Otherwise the named Secret.
    """
    auth_json = os.environ.get("CODEX_AUTH_JSON")
    if auth_json:
        return [modal.Secret.from_dict({"CODEX_AUTH_JSON": auth_json})]
    return [modal.Secret.from_name(CODEX_SECRET_NAME)]


def _devin_secrets(modal: Any) -> list[Any]:
    """Ephemeral single-account credential for the P2.1 local control path.

    Named per-account Secrets remain authoritative when ``SandboxSpec.secrets``
    is populated.  This fallback lets a local gate process inject its own
    credential blob without persisting that blob in Modal.
    """
    blob = os.environ.get(_ACCOUNT_CREDENTIAL_ENV)
    if not blob:
        credential_file = os.environ.get(_ACCOUNT_CREDENTIAL_FILE_ENV)
        if credential_file:
            content = Path(credential_file).read_text(encoding="utf-8")
            blob = json.dumps(
                {
                    "provider": "devin",
                    "files": {".local/share/devin/credentials.toml": content},
                }
            )
    secret_env: dict[str, str] = {}
    if blob:
        secret_env[_ACCOUNT_CREDENTIAL_ENV] = blob
    if os.environ.get("SBX_GITHUB_EPHEMERAL") == "1":
        github_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if github_token:
            secret_env["GH_TOKEN"] = github_token
            secret_env["GITHUB_TOKEN"] = github_token
    if not secret_env:
        return []
    return [modal.Secret.from_dict(secret_env)]


def _spec_provider(spec: SandboxSpec) -> str:
    """Provider selected for this sandbox (default ``codex``)."""
    return spec.tags.get("provider", "codex")


def _sandbox_secrets(modal: Any, spec: SandboxSpec) -> list[Any]:
    """Secrets for ``Sandbox.create`` / ``exec``.

    ``spec.secrets`` names per-account Secrets (P2: ``sbx-acct-<account_id>``,
    carrying the ``SBX_ACCOUNT_CREDENTIAL`` blob). Devin sandboxes never get
    the Codex auth Secret; anything else keeps the P1 Codex path unchanged.
    """
    if spec.secrets:
        return [modal.Secret.from_name(name) for name in spec.secrets]
    if _spec_provider(spec) == DEVIN_PROVIDER:
        return _devin_secrets(modal)
    return _codex_secrets(modal)


def _create_env(spec: SandboxSpec) -> dict[str, str]:
    env = {"CODEX_HOME": CODEX_HOME, "SBX_WORK": WORK_DIR}
    if _spec_provider(spec) == DEVIN_PROVIDER:
        env.update(_devin_home_env())
    env.update(spec.env)
    return env


def _devin_home_env() -> dict[str, str]:
    """``HOME``/XDG rooted at ``$SBX_WORK/home`` for Devin sandboxes."""
    try:
        from runtime.image import devin_runtime_env

        return devin_runtime_env(WORK_DIR)
    except ImportError:
        home = f"{WORK_DIR}/home"
        return {
            "HOME": home,
            "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_CACHE_HOME": f"{home}/.cache",
            "XDG_DATA_HOME": f"{home}/.local/share",
            "XDG_STATE_HOME": f"{home}/.local/state",
        }


def _resolve_image(modal: Any, provider: str = "codex") -> Any:
    if provider == DEVIN_PROVIDER:
        from_name = getattr(modal.Image, "from_name", None)
        if callable(from_name):
            return from_name(DEVIN_IMAGE_NAME)
        try:
            from runtime.image import sbx_devin_image
        except ImportError:
            pass
        else:
            return sbx_devin_image()
        return modal.Image.debian_slim(python_version="3.12")
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

    def __init__(
        self,
        proc: Any,
        *,
        sandbox_id: str,
        pid_file: str,
        secret_names: tuple[str, ...] = (),
        provider: str = "codex",
    ) -> None:
        self._proc = proc
        self._sandbox_id = sandbox_id
        self._pid_file = pid_file
        self._secret_names = secret_names
        self._provider = provider
        self.stdout = _iter_lines(proc.stdout)

    def wait(self) -> int:
        return int(self._proc.wait())

    def kill(self) -> None:
        modal = _load_modal()
        try:
            sb = modal.Sandbox.from_id(self._sandbox_id)
            killer = sb.exec(
                "bash",
                "-c",
                f"if [ -f {self._pid_file} ]; then "
                f"kill $(cat {self._pid_file}) 2>/dev/null || true; fi",
                bufsize=1,
                secrets=self._exec_secrets(modal),
            )
            _close_stdin(killer)
            killer.wait()
        except Exception as exc:
            if _is_sandbox_gone(exc):
                return
            raise

    def _exec_secrets(self, modal: Any) -> list[Any]:
        if self._secret_names:
            return [modal.Secret.from_name(name) for name in self._secret_names]
        if self._provider == DEVIN_PROVIDER:
            return _devin_secrets(modal)
        return _codex_secrets(modal)


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
        self._secrets_by_sandbox: dict[str, list[str]] = {}

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        modal = _load_modal()
        tags = dict(spec.tags)
        provider = _spec_provider(spec)
        sb = modal.Sandbox.create(
            "sleep",
            "infinity",
            app=modal.App.lookup(self._app_name, create_if_missing=True),
            image=_resolve_image(modal, provider),
            secrets=_sandbox_secrets(modal, spec),
            env=_create_env(spec),
            cpu=CPU,
            memory=MEMORY_MIB,
            timeout=SANDBOX_TIMEOUT_S,
            idle_timeout=IDLE_TIMEOUT_S,
            workdir=WORK_DIR,
            tags=tags,
        )
        if spec.secrets:
            self._secrets_by_sandbox[sb.object_id] = list(spec.secrets)
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
        secret_names = tuple(self._secrets_by_sandbox.get(handle.id) or ())
        provider = handle.tags.get("provider", "codex")
        secrets = self._exec_secrets(modal, handle)
        if env:
            proc = sb.exec(*wrapped, bufsize=1, env=dict(env), secrets=secrets)
        else:
            proc = sb.exec(*wrapped, bufsize=1, secrets=secrets)
        _close_stdin(proc)
        return ModalProcess(
            proc,
            sandbox_id=handle.id,
            pid_file=pid_file,
            secret_names=secret_names,
            provider=provider,
        )

    def _exec_secrets(self, modal: Any, handle: SandboxHandle) -> list[Any]:
        names = self._secrets_by_sandbox.get(handle.id)
        if names:
            return [modal.Secret.from_name(name) for name in names]
        if handle.tags.get("provider") == DEVIN_PROVIDER:
            return _devin_secrets(modal)
        return _codex_secrets(modal)

    def terminate(self, handle: SandboxHandle) -> None:
        modal = _load_modal()
        self._secrets_by_sandbox.pop(handle.id, None)
        try:
            sb = modal.Sandbox.from_id(handle.id)
            sb.terminate()
        except Exception as exc:
            if _is_sandbox_gone(exc):
                return
            raise

    def poll(self, handle: SandboxHandle) -> SandboxPoll:
        modal = _load_modal()
        try:
            sb = modal.Sandbox.from_id(handle.id)
            rc = sb.poll()
        except Exception as exc:
            if _is_sandbox_gone(exc):
                return SandboxPoll(alive=False, active_processes=0)
            raise
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
