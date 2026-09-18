"""SOR-127 environment build / snapshot cache.

A *prepared environment* is a sandbox filesystem that already carries the
declared repo checkout plus any dependency/toolchain setup, so a second
agent with the same inputs restores it instead of paying the full
clone/setup cost again. The durable unit is the
``EnvironmentBuildRecord``, keyed by an explicit, content-derived
``environment_key`` — every invalidation input (repo, base_ref, base_sha,
provider image, workdir, setup command) is named in the key, so changing
any of them is a deliberate cache miss, never a silent stale hit.

Semantics:

* **Last-known-good** — ``snapshot_ref`` is only ever written by a
  successful build. A failed build records ``last_error``/``failed_at``
  and leaves the status ``healthy`` whenever a prior snapshot exists, so
  a transient failure can never evict a working environment.
* **Fail closed on restore** — a restored sandbox still has to prove the
  workdir sits exactly on the declared ``base_sha`` (the same
  ``base_sha_mismatch`` gate as a fresh prepare); drift is an explicit
  failure, never a fallback to a wrong version.
* **No credentials in snapshots** — build sandboxes are created with no
  account/provider Secrets (``env_build`` tag: the Modal backend attaches
  nothing, not even the ambient local-gate blob), every build exec runs
  under :func:`build_env` (never ``sandbox_env``, which forwards ambient
  credential vars), and a scrub step deletes credential-shaped paths
  before the filesystem snapshot is taken. The opt-in GitHub bridge is
  the only tolerated credential channel: it travels by env and the
  ``GIT_CONFIG_*`` helper, so the token never lands on disk.
* **Worker holds no Modal control credentials** — snapshot/restore run
  control-plane-side through the ``SnapshotProvider`` seam. Local roots
  use directory copies; Modal uses the native filesystem snapshot
  (``Sandbox.snapshot_filesystem`` → ``Image.from_id`` on restore). The
  sandboxed worker never sees a Modal client.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from control import github
from control.backend import SandboxBackend, SandboxHandle, SandboxSpec
from control.config import (
    ANTIGRAVITY_IMAGE_NAME,
    DEVIN_IMAGE_NAME,
    ENVIRONMENTS_DICT_NAME,
    GROK_IMAGE_NAME,
    OPENCODE_IMAGE_NAME,
    RUNTIME_IMAGE_NAME,
    env_str,
)
from control.run_errors import clip_message
from control.sandbox_io import is_local_root
from control.workspace import (
    BASE_SHA_MISMATCH,
    CHECKOUT_FAILED,
    DEFAULT_WORKDIR,
    REPO_UNAVAILABLE,
    WORKSPACE_INVALID,
    WorkspaceError,
    WorkspaceSpec,
    is_commit_sha,
    is_safe_relpath,
    require_relpath,
)

# Record status reflects the *last build attempt*; the servable artifact is
# ``snapshot_ref`` (set only by a successful build, never cleared by a
# failure — a failed rebuild leaves the record ``healthy``).
ENV_BUILDING = "building"
ENV_HEALTHY = "healthy"
ENV_FAILED = "failed"
ENV_STATUSES = (ENV_BUILDING, ENV_HEALTHY, ENV_FAILED)

# Sandbox tag marking a credential-free build sandbox. The Modal backend
# attaches no Secrets to ``env_build`` sandboxes at all — the ambient
# account blob and the Codex auth Secret are skipped, not just the named
# per-account Secrets — so nothing credential-shaped is ever mounted while
# the snapshot-able filesystem is produced.
ENV_BUILD_TAG = "env_build"
ENV_ROLE_TAG = "sbx_role"
ENV_ROLE_VALUE = "env-build"

# Snapshot-key schema version: bump changes every key (explicit full
# invalidation) when the key inputs or build semantics change.
_KEY_SCHEMA = 1

_SNAPSHOT_REF_RE = re.compile(r"[0-9A-Za-z_.:-]+")

# Provider tag -> default named runtime image (mirrors
# ``control.backends.modal`` image resolution so the cache key tracks the
# same image the consumer sandbox will run).
_PROVIDER_IMAGES = {
    "codex": RUNTIME_IMAGE_NAME,
    "devin": DEVIN_IMAGE_NAME,
    "antigravity": ANTIGRAVITY_IMAGE_NAME,
    "grok": GROK_IMAGE_NAME,
    "opencode": OPENCODE_IMAGE_NAME,
}

# Credential-shaped paths scrubbed from the sandbox root before snapshot.
# ``{workdir}`` entries are also applied inside the repo checkout — a
# setup command must not be able to smuggle a credential file into the
# snapshot through the workdir either.
_SCRUB_ROOT_RELPATHS = (
    ".codex",
    "home/.codex",
    ".git-credentials",
    "home/.git-credentials",
    ".netrc",
    "home/.netrc",
    ".ssh",
    "home/.ssh",
    ".gitconfig",
    "home/.gitconfig",
    ".config/gh",
    "home/.config/gh",
    # Provider credential blob restore targets (``SBX_ACCOUNT_CREDENTIAL``
    # ``files`` relpaths, relative to ``$HOME``).
    "home/.local/share/devin",
    "home/.gemini",
    "home/.grok",
    "home/.local/share/opencode",
    # Runtime/handoff staging never belongs in a reusable environment.
    "events.jsonl",
    ".sbx-handoff",
)
_SCRUB_WORKDIR_RELPATHS = (".git-credentials", ".netrc", ".ssh")

_SCRUB_SCRIPT = (
    "import pathlib, shutil, sys\n"
    "root = pathlib.Path(sys.argv[1])\n"
    "for rel in sys.argv[2:]:\n"
    "    p = root / rel\n"
    "    try:\n"
    "        if p.is_symlink() or p.is_file():\n"
    "            p.unlink()\n"
    "        elif p.is_dir():\n"
    "            shutil.rmtree(p)\n"
    "    except FileNotFoundError:\n"
    "        pass\n"
)


def image_name_for(provider: str) -> str:
    """Named runtime image for ``provider`` (``SBX_IMAGE_<PROVIDER>`` override)."""
    default = _PROVIDER_IMAGES.get(provider, RUNTIME_IMAGE_NAME)
    return env_str(f"SBX_IMAGE_{provider.upper()}", default)


@dataclass(frozen=True)
class EnvironmentSpec:
    """Cache-key inputs for one reusable prepared environment.

    ``repo``/``base_ref``/``base_sha`` are the workspace declaration;
    ``provider`` + ``image`` pin the runtime image the build ran under;
    ``workdir`` is the sandbox-relative checkout location; ``setup`` is an
    optional shell command for dependency/toolchain setup executed in the
    workdir after checkout. Every field feeds :func:`environment_key` —
    invalidation is explicit: change an input, get a new key.
    """

    repo: str
    base_ref: str
    base_sha: str
    provider: str = "codex"
    workdir: str = DEFAULT_WORKDIR
    image: str = ""
    setup: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.repo, str) or not self.repo:
            raise WorkspaceError(WORKSPACE_INVALID, "environment repo must be a non-empty string")
        if not isinstance(self.base_ref, str) or not self.base_ref:
            raise WorkspaceError(
                WORKSPACE_INVALID, "environment base_ref must be a non-empty string"
            )
        if not is_commit_sha(self.base_sha):
            raise WorkspaceError(
                WORKSPACE_INVALID,
                f"environment base_sha must be a 40-hex commit sha: {self.base_sha!r}",
            )
        if not isinstance(self.provider, str) or not self.provider:
            raise WorkspaceError(WORKSPACE_INVALID, "environment provider must be non-empty")
        if not is_safe_relpath(self.workdir):
            raise WorkspaceError(
                WORKSPACE_INVALID,
                f"environment workdir must be a safe relative path: {self.workdir!r}",
            )
        for key in ("image", "setup"):
            value = getattr(self, key)
            if not isinstance(value, str):
                raise WorkspaceError(WORKSPACE_INVALID, f"environment {key} must be a string")

    @property
    def key(self) -> str:
        return environment_key(self)


def environment_key(spec: EnvironmentSpec) -> str:
    """Deterministic cache key: sha256 over the canonical JSON of every input."""
    canonical = json.dumps(
        {
            "v": _KEY_SCHEMA,
            "repo": spec.repo,
            "base_ref": spec.base_ref,
            "base_sha": spec.base_sha,
            "provider": spec.provider,
            "workdir": spec.workdir,
            "image": spec.image,
            "setup": spec.setup,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class EnvironmentBuildRecord:
    """Durable record for one environment key (last-known-good semantics).

    ``snapshot_ref`` is the servable artifact — an opaque provider ref
    (Modal image id, local snapshot id) written only by a successful build.
    A failed build records ``last_error``/``failed_at`` and keeps the
    status ``healthy`` whenever a snapshot already exists, so a failed
    build never replaces a healthy build.
    """

    key: str
    repo: str
    base_ref: str
    base_sha: str
    provider: str = "codex"
    workdir: str = DEFAULT_WORKDIR
    image: str = ""
    setup: str = ""
    status: str = ENV_BUILDING
    snapshot_ref: str | None = None
    attempts: int = 0
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    built_at: str | None = None
    failed_at: str | None = None

    @property
    def usable(self) -> bool:
        """Whether this record serves a snapshot right now."""
        return self.snapshot_ref is not None


def env_record_to_dict(record: EnvironmentBuildRecord) -> dict[str, Any]:
    return asdict(record)


def env_record_from_dict(data: Any) -> EnvironmentBuildRecord:
    """Strict-ish decode; raises ``ValueError`` on unusable payloads."""
    if not isinstance(data, dict):
        raise ValueError("environment record is not a dict")
    record = EnvironmentBuildRecord(
        key=_required_str(data, "key"),
        repo=_required_str(data, "repo"),
        base_ref=_required_str(data, "base_ref"),
        base_sha=_required_str(data, "base_sha"),
    )
    if not is_commit_sha(record.base_sha):
        raise ValueError("environment record field base_sha must be a commit sha")
    for key in ("provider", "image", "setup"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"environment record field {key} must be a string")
        setattr(record, key, value or ("codex" if key == "provider" else ""))
    workdir = data.get("workdir", DEFAULT_WORKDIR)
    if not is_safe_relpath(workdir):
        raise ValueError("environment record field workdir must be a safe relative path")
    record.workdir = workdir
    status = data.get("status", ENV_BUILDING)
    if status not in ENV_STATUSES:
        raise ValueError(f"environment record field status must be one of {ENV_STATUSES}")
    record.status = status
    snapshot_ref = data.get("snapshot_ref")
    if snapshot_ref is not None and (
        not isinstance(snapshot_ref, str) or not _SNAPSHOT_REF_RE.fullmatch(snapshot_ref)
    ):
        raise ValueError("environment record field snapshot_ref must be a safe ref string")
    record.snapshot_ref = snapshot_ref
    attempts = data.get("attempts", 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("environment record field attempts must be a non-negative int")
    record.attempts = attempts
    for key in ("last_error", "built_at", "failed_at"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"environment record field {key} must be a string")
        setattr(record, key, value)
    for key in ("created_at", "updated_at"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"environment record field {key} must be a string")
        setattr(record, key, value or "")
    return record


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"environment record missing {key}")
    return value


@runtime_checkable
class EnvironmentStore(Protocol):
    """Persistence for ``EnvironmentBuildRecord``s, keyed by environment key."""

    def get(self, key: str) -> EnvironmentBuildRecord | None:
        """Return the record, or None when absent."""

    def put(self, record: EnvironmentBuildRecord) -> None:
        """Insert or replace a record."""

    def delete(self, key: str) -> None:
        """Remove a record (explicit invalidation)."""

    def list(self) -> list[EnvironmentBuildRecord]:
        """All records (inspection / operator invalidation)."""


class InMemoryEnvironmentStore:
    """Thread-safe dict store for tests and ephemeral deployments."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> EnvironmentBuildRecord | None:
        with self._lock:
            raw = self._items.get(key)
        if raw is None:
            return None
        try:
            return env_record_from_dict(raw)
        except ValueError as exc:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"stored environment record {key} is corrupt: {exc}"
            ) from exc

    def put(self, record: EnvironmentBuildRecord) -> None:
        with self._lock:
            self._items[record.key] = env_record_to_dict(record)

    def delete(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def list(self) -> list[EnvironmentBuildRecord]:
        with self._lock:
            raws = list(self._items.values())
        return [env_record_from_dict(raw) for raw in raws]


class FileEnvironmentStore:
    """Local durable store: ``root/<key>.json`` (atomic writes)."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise WorkspaceError(
                WORKSPACE_INVALID, f"environment key must be a sha256 hex: {key!r}"
            )
        return self._root / f"{key}.json"

    def get(self, key: str) -> EnvironmentBuildRecord | None:
        try:
            raw = json.loads(self._path(key).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"stored environment record {key} is corrupt: {exc}"
            ) from exc
        try:
            return env_record_from_dict(raw)
        except ValueError as exc:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"stored environment record {key} is corrupt: {exc}"
            ) from exc

    def put(self, record: EnvironmentBuildRecord) -> None:
        path = self._path(record.key)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(env_record_to_dict(record), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)

    def delete(self, key: str) -> None:
        with self._lock:
            self._path(key).unlink(missing_ok=True)

    def list(self) -> list[EnvironmentBuildRecord]:
        try:
            paths = sorted(self._root.glob("*.json"))
        except OSError:
            return []
        out: list[EnvironmentBuildRecord] = []
        for path in paths:
            record = self.get(path.stem)
            if record is not None:
                out.append(record)
        return out


class ModalDictEnvironmentStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal."""

    def __init__(self, name: str = ENVIRONMENTS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def get(self, key: str) -> EnvironmentBuildRecord | None:
        raw = self._d().get(key)
        if raw is None:
            return None
        try:
            return env_record_from_dict(raw)
        except ValueError as exc:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"stored environment record {key} is corrupt: {exc}"
            ) from exc

    def put(self, record: EnvironmentBuildRecord) -> None:
        self._d().put(record.key, env_record_to_dict(record))

    def delete(self, key: str) -> None:
        try:
            self._d().pop(key)
        except KeyError:
            return

    def list(self) -> list[EnvironmentBuildRecord]:
        out: list[EnvironmentBuildRecord] = []
        for raw in self._d().values():
            record = env_record_from_dict(raw)
            out.append(record)
        return out


@runtime_checkable
class SnapshotProvider(Protocol):
    """Filesystem snapshot/restore seam — control-plane side only.

    The worker inside a sandbox never holds Modal control credentials:
    snapshot and restore are driven by the control plane through this
    interface. ``snapshot`` returns an opaque provider ref persisted on the
    build record; ``restore`` creates a fresh sandbox pre-populated with
    the snapshotted filesystem.
    """

    def snapshot(self, handle: SandboxHandle) -> str:
        """Snapshot the sandbox filesystem; return an opaque snapshot ref."""

    def restore(self, snapshot_ref: str, spec: SandboxSpec) -> SandboxHandle:
        """Create a new sandbox whose filesystem starts as ``snapshot_ref``."""


class LocalSnapshotProvider:
    """Directory-copy ``SnapshotProvider`` for ``LocalProcessBackend``.

    Snapshots are immutable directory copies under ``root/<ref>``; restore
    creates a fresh sandbox via the backend and copies the snapshot
    contents in — the same shape as a Modal filesystem snapshot restore.
    """

    def __init__(self, backend: SandboxBackend, root: Path | str) -> None:
        self._backend = backend
        self._root = Path(root)

    def snapshot(self, handle: SandboxHandle) -> str:
        if not is_local_root(handle):
            raise WorkspaceError(
                WORKSPACE_INVALID, "LocalSnapshotProvider requires a local sandbox root"
            )
        ref = uuid.uuid4().hex
        dest = self._root / ref
        tmp = self._root / f".{ref}.tmp"
        self._root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(handle.root, tmp, symlinks=True)
        tmp.rename(dest)  # atomic publish — no half-copied snapshot is servable
        return ref

    def restore(self, snapshot_ref: str, spec: SandboxSpec) -> SandboxHandle:
        src = self._require(snapshot_ref)
        handle = self._backend.create(spec)
        if not is_local_root(handle):
            raise WorkspaceError(
                WORKSPACE_INVALID, "LocalSnapshotProvider requires a local sandbox root"
            )
        shutil.copytree(src, handle.root, symlinks=True, dirs_exist_ok=True)
        return handle

    def _require(self, snapshot_ref: str) -> Path:
        if not isinstance(snapshot_ref, str) or not _SNAPSHOT_REF_RE.fullmatch(snapshot_ref):
            raise WorkspaceError(WORKSPACE_INVALID, f"unsafe snapshot ref: {snapshot_ref!r}")
        src = self._root / snapshot_ref
        if not src.is_dir():
            raise WorkspaceError(
                REPO_UNAVAILABLE, f"environment snapshot {snapshot_ref!r} is not available"
            )
        return src


def build_env(handle: SandboxHandle, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Credential-free exec env for environment builds.

    Deliberately NOT ``sandbox_env``: that seam forwards ambient
    ``CODEX_AUTH_JSON`` / ``SBX_ACCOUNT_CREDENTIAL`` into matching
    sandboxes, and a build sandbox must produce a credential-free
    filesystem for the snapshot. ``HOME`` is pinned inside the sandbox
    root so toolchain/dependency caches land in the snapshot and
    credential-shaped writes land in the scrub zone. The opt-in GitHub
    bridge env is allowed: it is env-only — the ``$GH_TOKEN`` value is
    expanded by the credential helper at git runtime and never touches
    the filesystem.
    """
    env = {
        "SBX_WORK": str(handle.root),
        "HOME": str(handle.root / "home"),
        "PYTHONUNBUFFERED": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if not is_local_root(handle):
        env["PYTHONPATH"] = "/opt/sbx"  # runtime.image.PYTHONPATH_REMOTE
    env.update(github.exec_env())
    if extra:
        safe_extra = dict(extra)
        # The GitHub token/helper env enters only through the opt-in seam.
        for key in list(safe_extra):
            if github.owns_env_key(key):
                safe_extra.pop(key, None)
        env.update(safe_extra)
    return env


def _git(
    backend: SandboxBackend, handle: SandboxHandle, args: list[str], *, cwd: str | None = None
) -> tuple[int, list[str]]:
    directory = str(handle.root / cwd) if cwd else str(handle.root)
    proc = backend.exec(handle, ["git", "-C", directory, *args], env=build_env(handle))
    lines = list(proc.stdout)
    return proc.wait(), lines


def _rev_parse(
    backend: SandboxBackend, handle: SandboxHandle, workdir: str, ref: str
) -> str | None:
    code, lines = _git(backend, handle, ["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=workdir)
    if code != 0 or not lines:
        return None
    sha = lines[0].strip()
    return sha if is_commit_sha(sha) else None


def _scrub_relpaths(workdir: str) -> list[str]:
    return [
        *_SCRUB_ROOT_RELPATHS,
        *(f"{workdir}/{rel}" for rel in _SCRUB_WORKDIR_RELPATHS),
    ]


class EnvironmentService:
    """Build / resolve / restore prepared environments.

    ``build`` runs the full prepare in a dedicated credential-free builder
    sandbox and publishes a filesystem snapshot on success; ``resolve``
    serves the last-known-good snapshot for a spec; ``restore`` creates a
    consumer sandbox from a record's snapshot. A build failure is an
    explicit ``WorkspaceError`` and updates the record in place — the
    previous healthy snapshot is never evicted by it.
    """

    def __init__(
        self,
        backend: SandboxBackend,
        store: EnvironmentStore,
        *,
        snapshots: SnapshotProvider | None = None,
        setup: str = "",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend
        self._store = store
        self._snapshots = snapshots
        self._setup = setup
        self._clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    @property
    def store(self) -> EnvironmentStore:
        return self._store

    @property
    def snapshots(self) -> SnapshotProvider | None:
        return self._snapshots

    def _now(self) -> str:
        return self._clock().isoformat()

    def spec_for(
        self,
        workspace: WorkspaceSpec | Mapping[str, Any],
        *,
        provider: str = "codex",
        workdir: str = DEFAULT_WORKDIR,
        setup: str | None = None,
    ) -> EnvironmentSpec:
        """Build the ``EnvironmentSpec`` a workspace declaration maps to.

        ``image`` resolves to the provider's named runtime image so the
        cache key tracks exactly what the consumer sandbox will boot.
        ``setup`` defaults to the service's configured setup command.
        """
        if isinstance(workspace, WorkspaceSpec):
            spec = workspace
        else:
            spec = WorkspaceSpec(
                repo=str(workspace.get("repo") or ""),
                base_ref=str(workspace.get("base_ref") or ""),
                base_sha=str(workspace.get("base_sha") or ""),
            )
        return EnvironmentSpec(
            repo=spec.repo,
            base_ref=spec.base_ref,
            base_sha=spec.base_sha,
            provider=provider,
            workdir=workdir,
            image=image_name_for(provider),
            setup=self._setup if setup is None else setup,
        )

    def get(self, key: str) -> EnvironmentBuildRecord | None:
        return self._store.get(key)

    def resolve(self, spec: EnvironmentSpec) -> EnvironmentBuildRecord | None:
        """The servable record for ``spec``, or None on a cache miss.

        A record is servable while it holds a ``snapshot_ref`` — a failed
        rebuild keeps serving the last-known-good snapshot rather than
        surfacing a miss that would silently degrade to a cold build.
        """
        record = self._store.get(spec.key)
        if record is None or not record.usable:
            return None
        return record

    def invalidate(self, key: str) -> None:
        """Explicit invalidation: drop the record for ``key`` entirely."""
        self._store.delete(key)

    def list(self) -> list[EnvironmentBuildRecord]:
        return self._store.list()

    def restore(self, record: EnvironmentBuildRecord, spec: SandboxSpec) -> SandboxHandle:
        """Create a consumer sandbox from ``record``'s snapshot."""
        if self._snapshots is None:
            raise WorkspaceError(
                WORKSPACE_INVALID, "no snapshot provider configured for environment restores"
            )
        if record.snapshot_ref is None:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"environment {record.key} has no snapshot to restore"
            )
        return self._snapshots.restore(record.snapshot_ref, spec)

    def _build_lock(self, key: str) -> threading.Lock:
        with self._locks_lock:
            return self._locks.setdefault(key, threading.Lock())

    def try_build(self, spec: EnvironmentSpec) -> EnvironmentBuildRecord:
        """``build`` that never raises — used for best-effort cache fills.

        The returned record carries the outcome (``status``/``last_error``);
        a failure is durable diagnostics, not a swallowed exception.
        """
        try:
            return self.build(spec)
        except Exception:
            try:
                record = self._store.get(spec.key)
            except Exception:
                record = None
            if record is not None:
                return record
            return EnvironmentBuildRecord(
                key=spec.key,
                repo=spec.repo,
                base_ref=spec.base_ref,
                base_sha=spec.base_sha,
                provider=spec.provider,
                workdir=spec.workdir,
                image=spec.image,
                setup=spec.setup,
                status=ENV_FAILED,
                last_error="build failed before a record could be persisted",
            )

    def build(self, spec: EnvironmentSpec) -> EnvironmentBuildRecord:
        """Build (or rebuild) the prepared environment for ``spec``.

        Steps, all inside a dedicated credential-free builder sandbox:
        create (``env_build`` tag, no Secrets) → clone → resolve
        ``base_ref`` → verify ``base_sha`` (fail closed) → checkout →
        optional setup command → credential scrub → filesystem snapshot.
        The record is persisted ``building`` first so a crash mid-build
        still leaves an inspectable state; on success it goes ``healthy``
        with the new ``snapshot_ref``, on failure the error is recorded
        while any prior snapshot stays servable.
        """
        key = spec.key
        with self._build_lock(key):
            record = self._store.get(key) or EnvironmentBuildRecord(
                key=key,
                repo=spec.repo,
                base_ref=spec.base_ref,
                base_sha=spec.base_sha,
                provider=spec.provider,
                workdir=spec.workdir,
                image=spec.image,
                setup=spec.setup,
                created_at=self._now(),
            )
            record.status = ENV_BUILDING
            record.attempts += 1
            record.updated_at = self._now()
            self._store.put(record)
            handle: SandboxHandle | None = None
            try:
                # Credential-free by construction: the ``env_build`` tag and
                # empty Secret lists mean nothing credential-shaped is ever
                # mounted while the snapshot-able filesystem is produced.
                handle = self._backend.create(
                    SandboxSpec(
                        tags={
                            "provider": spec.provider,
                            ENV_BUILD_TAG: "1",
                            ENV_ROLE_TAG: ENV_ROLE_VALUE,
                            "env_key": spec.key,
                        },
                        secrets=[],
                        resource_secrets=[],
                        env={},
                    )
                )
                self._prepare_filesystem(handle, spec)
                if self._snapshots is None:
                    raise WorkspaceError(
                        WORKSPACE_INVALID,
                        "no snapshot provider configured for environment builds",
                    )
                snapshot_ref = self._snapshots.snapshot(handle)
                record.snapshot_ref = snapshot_ref
                record.status = ENV_HEALTHY
                record.built_at = self._now()
                record.last_error = None
            except Exception as exc:
                record.last_error = clip_message(str(exc))[:300]
                record.failed_at = self._now()
                # Last-known-good: a prior snapshot stays servable; only a
                # never-built key reports failed.
                record.status = ENV_HEALTHY if record.snapshot_ref else ENV_FAILED
                record.updated_at = self._now()
                self._store.put(record)
                raise
            finally:
                if handle is not None:
                    try:
                        self._backend.terminate(handle)
                    except Exception:
                        pass
            record.updated_at = self._now()
            self._store.put(record)
            return record

    def _prepare_filesystem(self, handle: SandboxHandle, spec: EnvironmentSpec) -> None:
        """Prepare the builder sandbox filesystem ahead of the snapshot."""
        workdir = require_relpath(spec.workdir, code=WORKSPACE_INVALID, what="workdir")
        self._clone(handle, spec, workdir)
        self._verify_base(handle, spec, workdir)
        if spec.setup:
            self._run_setup(handle, spec, workdir)
        self._sanitize_remote_url(handle, spec, workdir)
        self._scrub(handle, workdir)

    def _clone(self, handle: SandboxHandle, spec: EnvironmentSpec, workdir: str) -> None:
        code, _ = _git(self._backend, handle, ["clone", "--", spec.repo, workdir])
        if code != 0:
            raise WorkspaceError(
                REPO_UNAVAILABLE,
                f"environment build: git clone "
                f"{github.redact_url_credentials(spec.repo)!r} failed (exit {code})",
            )

    def _verify_base(self, handle: SandboxHandle, spec: EnvironmentSpec, workdir: str) -> None:
        resolved = None
        for candidate in (spec.base_ref, f"origin/{spec.base_ref}"):
            resolved = _rev_parse(self._backend, handle, workdir, candidate)
            if resolved is not None:
                break
        if resolved is None:
            raise WorkspaceError(
                CHECKOUT_FAILED,
                f"environment build: base_ref {spec.base_ref!r} does not resolve to a commit",
            )
        if resolved != spec.base_sha:
            raise WorkspaceError(
                BASE_SHA_MISMATCH,
                f"environment build: declared base_sha {spec.base_sha} does not match "
                f"{spec.base_ref} at {resolved}",
            )
        code, _ = _git(self._backend, handle, ["checkout", "--detach", spec.base_sha], cwd=workdir)
        if code != 0:
            raise WorkspaceError(
                CHECKOUT_FAILED,
                f"environment build: git checkout {spec.base_sha} failed (exit {code})",
            )
        actual = _rev_parse(self._backend, handle, workdir, "HEAD")
        if actual != spec.base_sha:
            raise WorkspaceError(
                BASE_SHA_MISMATCH,
                f"environment build: checkout drifted — HEAD is {actual}, expected {spec.base_sha}",
            )

    def _run_setup(self, handle: SandboxHandle, spec: EnvironmentSpec, workdir: str) -> None:
        """Dependency/toolchain setup inside the workdir (``bash -lc``)."""
        proc = self._backend.exec(
            handle,
            ["bash", "-lc", f"cd {shlex.quote(spec.workdir)} && {spec.setup}"],
            env=build_env(handle),
        )
        for _ in proc.stdout:
            pass
        if proc.wait() != 0:
            raise WorkspaceError(CHECKOUT_FAILED, "environment build: setup command failed")

    def _sanitize_remote_url(
        self, handle: SandboxHandle, spec: EnvironmentSpec, workdir: str
    ) -> None:
        """Strip userinfo from ``origin`` so a credential-bearing clone URL
        cannot survive into the snapshot via ``.git/config``."""
        safe_url = github.redact_url_credentials(spec.repo)
        code, _ = _git(
            self._backend, handle, ["remote", "set-url", "origin", safe_url], cwd=workdir
        )
        if code != 0:
            raise WorkspaceError(
                CHECKOUT_FAILED, "environment build: failed to sanitize origin URL"
            )

    def _scrub(self, handle: SandboxHandle, workdir: str) -> None:
        """Delete credential-shaped paths before the filesystem snapshot."""
        if is_local_root(handle):
            root = Path(handle.root)
            for rel in _scrub_relpaths(workdir):
                path = root / rel
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        shutil.rmtree(path)
                except FileNotFoundError:
                    pass
            return
        proc = self._backend.exec(
            handle,
            [
                "python3",
                "-c",
                _SCRUB_SCRIPT,
                str(handle.root),
                *_scrub_relpaths(workdir),
            ],
            env=build_env(handle),
        )
        for _ in proc.stdout:
            pass
        if proc.wait() != 0:
            raise WorkspaceError(CHECKOUT_FAILED, "environment build: credential scrub failed")


__all__ = [
    "ENV_BUILDING",
    "ENV_BUILD_TAG",
    "ENV_FAILED",
    "ENV_HEALTHY",
    "ENV_ROLE_TAG",
    "ENV_ROLE_VALUE",
    "ENV_STATUSES",
    "EnvironmentBuildRecord",
    "EnvironmentService",
    "EnvironmentSpec",
    "EnvironmentStore",
    "FileEnvironmentStore",
    "InMemoryEnvironmentStore",
    "LocalSnapshotProvider",
    "ModalDictEnvironmentStore",
    "SnapshotProvider",
    "build_env",
    "env_record_from_dict",
    "env_record_to_dict",
    "environment_key",
    "image_name_for",
]
