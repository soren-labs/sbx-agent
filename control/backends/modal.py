"""Modal Sandbox backend. Tests must not instantiate or call this class.

``import modal`` is deferred until a method runs so collecting tests cannot
open a Modal connection. Signatures match Modal SDK + P0 (SOR-28).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from control import github
from control.backend import Process, SandboxHandle, SandboxPoll, SandboxSpec
from control.config import (
    ANTIGRAVITY_IMAGE_NAME,
    CODEX_HOME,
    CODEX_SECRET_NAME,
    CPU,
    DEVIN_IMAGE_NAME,
    ENV_SNAPSHOT_TIMEOUT_S,
    ENV_SNAPSHOT_TTL_S,
    GROK_IMAGE_NAME,
    IDLE_TIMEOUT_S,
    MEMORY_MIB,
    MODAL_APP_NAME,
    OPENCODE_IMAGE_NAME,
    RUNTIME_IMAGE_NAME,
    SANDBOX_TIMEOUT_S,
    WORK_DIR,
    env_str,
)
from control.environment import ENV_BUILD_TAG
from control.workspace import CHECKOUT_FAILED, REPO_UNAVAILABLE, WorkspaceError

_GONE_ERROR_NAMES = frozenset({"ConflictError", "NotFoundError"})

# SOR-74: ``SandboxSpec.tags["provider"] == "devin"`` selects the Devin image
# and per-account credential Secrets; anything else keeps the P1 Codex path.
DEVIN_PROVIDER = "devin"
# SOR-62/SOR-80: provider tags that resolve to named provider-CLI images and
# per-account credential Secrets (never the Codex auth Secret).
ANTIGRAVITY_PROVIDER = "antigravity"
GROK_PROVIDER = "grok"
# SOR-96: provider tag that resolves to the named opencode image and
# per-account credential Secrets (``~/.local/share/opencode/auth.json``).
OPENCODE_PROVIDER = "opencode"
ACCOUNT_PROVIDERS = frozenset(
    {DEVIN_PROVIDER, ANTIGRAVITY_PROVIDER, GROK_PROVIDER, OPENCODE_PROVIDER}
)
_PROVIDER_IMAGE_NAMES = {
    ANTIGRAVITY_PROVIDER: ANTIGRAVITY_IMAGE_NAME,
    GROK_PROVIDER: GROK_IMAGE_NAME,
    OPENCODE_PROVIDER: OPENCODE_IMAGE_NAME,
}
# ``SBX_ACCOUNT_CREDENTIAL_FILE`` wrap target per provider (the file's relpath
# inside the credential blob, relative to the sandbox $HOME).
_ACCOUNT_CREDENTIAL_FILE_REL = {
    DEVIN_PROVIDER: ".local/share/devin/credentials.toml",
    ANTIGRAVITY_PROVIDER: ".gemini/antigravity-cli/antigravity-oauth-token",
    GROK_PROVIDER: ".grok/auth.json",
    OPENCODE_PROVIDER: ".local/share/opencode/auth.json",
}
_ACCOUNT_CREDENTIAL_ENV = "SBX_ACCOUNT_CREDENTIAL"
_ACCOUNT_CREDENTIAL_FILE_ENV = "SBX_ACCOUNT_CREDENTIAL_FILE"
# SOR-77: task-scoped Linear MCP. ``SBX_LINEAR_MCP_EPHEMERAL=1`` on the
# control plane opts a worker into a generated ``mcp_config.json``; the key
# travels inside the Secret dict (never ``SandboxSpec.env``) and the runner
# references it via ``${env:SBX_LINEAR_API_KEY}`` — no raw key on disk.
_LINEAR_MCP_GATE_ENV = "SBX_LINEAR_MCP_EPHEMERAL"
_LINEAR_API_KEY_ENV = "SBX_LINEAR_API_KEY"
_LINEAR_HOST_KEY_ENVS = ("SBX_LINEAR_API_KEY", "LINEAR_API_KEY")


def _load_modal() -> Any:
    import modal

    return modal


def _is_sandbox_gone(exc: BaseException) -> bool:
    """``Sandbox.terminate``/``from_id`` raise these when already shutting down."""
    return any(cls.__name__ in _GONE_ERROR_NAMES for cls in type(exc).mro())


def _codex_secret_name() -> str:
    """Shared Codex auth Secret — ``SBX_CODEX_SECRET_NAME`` renames it for
    parallel deployments (the bootstrap config's ``secrets.codex``)."""
    return env_str("SBX_CODEX_SECRET_NAME", CODEX_SECRET_NAME)


def _codex_secrets(modal: Any) -> list[Any]:
    """Secret for every Sandbox ``create`` / ``exec``.

    Prefer an ephemeral ``from_dict`` when this process has ``CODEX_AUTH_JSON``
    (local uvicorn with ``SBX_BACKEND=modal``). Otherwise the named Secret.
    """
    auth_json = os.environ.get("CODEX_AUTH_JSON")
    if auth_json:
        return [modal.Secret.from_dict({"CODEX_AUTH_JSON": auth_json})]
    return [modal.Secret.from_name(_codex_secret_name())]


def _ambient_account_blob(provider: str) -> str | None:
    """Ambient ``SBX_ACCOUNT_CREDENTIAL`` valid for ``provider``, else ``None``.

    The ambient blob is the single-account local-gate fallback; a blob whose
    ``provider`` field names a different provider belongs to another
    provider's gate and must not be attached (it would fail ``runner init``
    on the provider check). ``SBX_ACCOUNT_CREDENTIAL_FILE`` is wrapped under
    the provider's credential relpath when no valid blob is present.
    """
    raw = os.environ.get(_ACCOUNT_CREDENTIAL_ENV)
    if raw:
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            blob = None
        if isinstance(blob, dict) and blob.get("provider") == provider:
            return raw
        # Wrong-provider / malformed ambient blob is not scoped to this
        # sandbox — fall through to the explicit credential file only.
    credential_file = os.environ.get(_ACCOUNT_CREDENTIAL_FILE_ENV)
    if credential_file:
        content = Path(credential_file).read_text(encoding="utf-8")
        return json.dumps(
            {
                "provider": provider,
                "files": {_ACCOUNT_CREDENTIAL_FILE_REL[provider]: content},
            }
        )
    return None


def _aux_secret_env(provider: str) -> dict[str, str]:
    """Opt-in non-credential bridges (GitHub for all providers, Linear MCP
    for Devin only).

    These stack alongside the authoritative credential source (named Secret
    or ambient blob) because they never carry ``SBX_ACCOUNT_CREDENTIAL``.
    SOR-117: the GitHub bridge (``control.github.exec_env``) is no longer
    Devin-specific — the same ``SBX_GITHUB_EPHEMERAL`` opt-in applies to every
    provider's sandbox.
    """
    env = github.exec_env()
    if provider == DEVIN_PROVIDER and os.environ.get(_LINEAR_MCP_GATE_ENV) == "1":
        linear_key = next(
            (os.environ.get(name) for name in _LINEAR_HOST_KEY_ENVS if os.environ.get(name)),
            None,
        )
        if linear_key:
            env[_LINEAR_API_KEY_ENV] = linear_key
    return env


def _account_secret_env(provider: str, *, credential: bool = True) -> dict[str, str]:
    """Ephemeral credential env for the P2.1 local control path.

    ``credential=False`` skips the ambient account blob so a named per-account
    Secret stays authoritative; provider-validated ambient blob and the
    opt-in aux bridges are otherwise included.
    """
    env: dict[str, str] = {}
    if credential:
        blob = _ambient_account_blob(provider)
        if blob:
            env[_ACCOUNT_CREDENTIAL_ENV] = blob
    env.update(_aux_secret_env(provider))
    return env


def _account_secrets(modal: Any, provider: str, *, credential: bool = True) -> list[Any]:
    """Ephemeral single-account credential Secret for account providers."""
    env = _account_secret_env(provider, credential=credential)
    return [modal.Secret.from_dict(env)] if env else []


def _devin_secrets(modal: Any) -> list[Any]:
    """Ephemeral single-account credential for the P2.1 local control path.

    Named per-account Secrets remain authoritative when ``SandboxSpec.secrets``
    is populated.  This fallback lets a local gate process inject its own
    credential blob without persisting that blob in Modal.
    """
    return _account_secrets(modal, DEVIN_PROVIDER)


def _spec_provider(spec: SandboxSpec) -> str:
    """Provider selected for this sandbox (default ``codex``)."""
    return spec.tags.get("provider", "codex")


def _is_env_build(tags: Mapping[str, str] | None) -> bool:
    """SOR-127 credential-free environment-build sandbox marker.

    Build sandboxes produce the filesystem a snapshot captures, so they
    must never carry credentials: no named Secret, no ambient account
    blob, no Codex auth Secret — on create or on any exec (including the
    kill exec)."""
    return bool(tags) and tags.get(ENV_BUILD_TAG) == "1"


def _secrets_for(modal: Any, provider: str, secret_names: Iterable[str]) -> list[Any]:
    """Secrets for ``Sandbox.create`` / ``exec`` / the kill exec.

    Named per-account Secrets (``sbx-acct-<account_id>``, carrying the
    ``SBX_ACCOUNT_CREDENTIAL`` blob) are authoritative: when present, the
    ambient account blob is never stacked on top, so one account's local-gate
    credential cannot shadow another account's Secret (SOR-80). Without a
    named Secret the provider-validated ambient blob remains the explicit
    single-account local-gate fallback. Account-provider sandboxes never get
    the Codex auth Secret; anything else keeps the P1 Codex path unchanged.
    """
    names = list(secret_names)
    if provider in ACCOUNT_PROVIDERS:
        # Account-provider sandboxes must never mount the shared Codex auth
        # Secret even if an internal caller or deployment override supplies it.
        codex_names = {CODEX_SECRET_NAME, _codex_secret_name()}
        names = [name for name in names if name not in codex_names]
    named = [modal.Secret.from_name(name) for name in names]
    if provider in ACCOUNT_PROVIDERS:
        return [*named, *_account_secrets(modal, provider, credential=not named)]
    secrets = named if named else _codex_secrets(modal)
    aux = _aux_secret_env(provider)
    # The opt-in GitHub bridge is provider-agnostic (SOR-117): it stacks on
    # codex sandboxes exactly like the account providers.
    return [*secrets, modal.Secret.from_dict(aux)] if aux else secrets


def _resource_secrets(modal: Any, secret_names: Iterable[str]) -> list[Any]:
    """SOR-129 session-resource Secrets for ``Sandbox.create`` / ``exec``.

    Attached *alongside* the provider account/auth resolution in
    :func:`_secrets_for` — a resource ref can never shadow or strip the
    sandbox's own credential chain. Names only; values never leave Modal.
    """
    return [modal.Secret.from_name(name) for name in dict.fromkeys(secret_names)]


def _sandbox_secrets(modal: Any, spec: SandboxSpec) -> list[Any]:
    """Secrets for ``Sandbox.create``: see :func:`_secrets_for`.

    SOR-127: ``env_build`` sandboxes attach nothing at all — the filesystem
    they produce becomes a reusable snapshot, so even env-only credential
    mounts are refused.
    """
    if _is_env_build(spec.tags):
        return []
    return [
        *_secrets_for(modal, _spec_provider(spec), spec.secrets),
        *_resource_secrets(modal, spec.resource_secrets),
    ]


def _create_env(spec: SandboxSpec) -> dict[str, str]:
    env = {"CODEX_HOME": CODEX_HOME, "SBX_WORK": WORK_DIR}
    provider = _spec_provider(spec)
    if provider in (DEVIN_PROVIDER, OPENCODE_PROVIDER):
        # OpenCode auth.json is an XDG data file like the Devin credential,
        # so it takes the same HOME+XDG pinning (SOR-96).
        env.update(_devin_home_env())
    elif provider in _PROVIDER_IMAGE_NAMES:
        env.update(_agent_home_env())
    env.update(spec.env)
    return env


def _agent_home_env() -> dict[str, str]:
    """``HOME=$SBX_WORK/home`` for provider-CLI sandboxes (agy / grok)."""
    try:
        from runtime.image import agent_home_env

        return agent_home_env(WORK_DIR)
    except ImportError:
        return {"HOME": f"{WORK_DIR}/home"}


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


def _provider_image_name(provider: str, default: str) -> str:
    """Named-image override for a parallel deploy (``SBX_IMAGE_<PROVIDER>``)."""
    return env_str(f"SBX_IMAGE_{provider.upper()}", default)


def _resolve_image(modal: Any, provider: str = "codex") -> Any:
    if provider == DEVIN_PROVIDER:
        from_name = getattr(modal.Image, "from_name", None)
        if callable(from_name):
            return from_name(_provider_image_name(provider, DEVIN_IMAGE_NAME))
        try:
            from runtime.image import sbx_devin_image
        except ImportError:
            pass
        else:
            return sbx_devin_image()
        return modal.Image.debian_slim(python_version="3.12")
    provider_image = _PROVIDER_IMAGE_NAMES.get(provider)
    if provider_image is not None:
        # SOR-62/SOR-80: named image published from the build host (the CLI
        # binary is a host artifact; there is no in-band source build).
        from_name = getattr(modal.Image, "from_name", None)
        if callable(from_name):
            return from_name(_provider_image_name(provider, provider_image))
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
        return from_name(_provider_image_name("codex", RUNTIME_IMAGE_NAME))
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
        env_build: bool = False,
    ) -> None:
        self._proc = proc
        self._sandbox_id = sandbox_id
        self._pid_file = pid_file
        self._secret_names = secret_names
        self._provider = provider
        self._env_build = env_build
        self.stdout = _iter_lines(proc.stdout)

    def stderr_text(self, limit: int = 2000) -> str:
        """Buffered stderr tail after exit; ``""`` when unreadable.

        Callers only read this after ``wait()`` — stderr is what turns a bare
        "exited N" into a diagnosable failure on the run record.
        """
        try:
            text = self._proc.stderr.read()
        except Exception:
            return ""
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        return text[-limit:]

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
        # SOR-127: the kill exec on a build sandbox carries no credentials
        # either — nothing credential-shaped is mounted while a snapshot
        # can be taken.
        if self._env_build:
            return []
        return _secrets_for(modal, self._provider, self._secret_names)


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
        # SOR-129 session-resource Secret names per sandbox — re-attached on
        # every ``exec`` (Modal ``exec(env=)`` replaces the process env, so
        # create-time mounts alone would vanish from later execs).
        self._resource_secrets_by_sandbox: dict[str, list[str]] = {}

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        modal = _load_modal()
        provider = _spec_provider(spec)
        return self._create_with_image(modal, spec, _resolve_image(modal, provider))

    def _create_with_image(self, modal: Any, spec: SandboxSpec, image: Any) -> SandboxHandle:
        """``Sandbox.create`` with an explicit image (SOR-127 snapshot restores)."""
        tags = dict(spec.tags)
        sb = modal.Sandbox.create(
            "sleep",
            "infinity",
            app=modal.App.lookup(self._app_name, create_if_missing=True),
            image=image,
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
        if spec.resource_secrets:
            self._resource_secrets_by_sandbox[sb.object_id] = list(spec.resource_secrets)
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
        env_build = _is_env_build(handle.tags)
        secrets = [] if env_build else self._exec_secrets(modal, handle)
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
            env_build=env_build,
        )

    def _exec_secrets(self, modal: Any, handle: SandboxHandle) -> list[Any]:
        if _is_env_build(handle.tags):
            return []
        names = self._secrets_by_sandbox.get(handle.id) or []
        provider = handle.tags.get("provider", "codex")
        resource_names = self._resource_secrets_by_sandbox.get(handle.id) or []
        return [
            *_secrets_for(modal, provider, names),
            *_resource_secrets(modal, resource_names),
        ]

    def terminate(self, handle: SandboxHandle) -> None:
        modal = _load_modal()
        self._secrets_by_sandbox.pop(handle.id, None)
        self._resource_secrets_by_sandbox.pop(handle.id, None)
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


class ModalSnapshotProvider:
    """SOR-127 environment snapshot/restore on Modal-native primitives.

    ``snapshot`` calls ``Sandbox.snapshot_filesystem`` — Modal's native
    filesystem snapshot, which returns an ``Image`` carrying the sandbox's
    entire filesystem; the image id is the durable ``snapshot_ref`` stored
    on the environment build record. ``restore`` rehydrates it with
    ``Image.from_id`` and creates a fresh sandbox through the same
    parameter path as :meth:`ModalBackend.create`, so a restored sandbox
    is identical modulo its filesystem.

    All Modal control credentials stay on the control plane — the worker
    inside the sandbox never sees a Modal client. Tests must not
    instantiate or call this class (``import modal`` is deferred).
    """

    def __init__(
        self,
        backend: ModalBackend,
        *,
        timeout_s: int = ENV_SNAPSHOT_TIMEOUT_S,
        ttl_s: int | None = ENV_SNAPSHOT_TTL_S,
    ) -> None:
        self._backend = backend
        self._timeout_s = timeout_s
        self._ttl_s = ttl_s

    def snapshot(self, handle: SandboxHandle) -> str:
        """``sb.snapshot_filesystem`` → durable image id (the snapshot ref)."""
        modal = _load_modal()
        sb = modal.Sandbox.from_id(handle.id)
        image = sb.snapshot_filesystem(timeout=self._timeout_s, ttl=self._ttl_s)
        ref = getattr(image, "object_id", None)
        if not ref:
            raise WorkspaceError(CHECKOUT_FAILED, "filesystem snapshot returned no image id")
        return str(ref)

    def restore(self, snapshot_ref: str, spec: SandboxSpec) -> SandboxHandle:
        """``Image.from_id`` + ``Sandbox.create`` — the snapshot's filesystem."""
        modal = _load_modal()
        from_id = getattr(modal.Image, "from_id", None)
        if not callable(from_id):
            raise WorkspaceError(REPO_UNAVAILABLE, "modal.Image.from_id is unavailable on this SDK")
        image = from_id(snapshot_ref)
        return self._backend._create_with_image(modal, spec, image)
