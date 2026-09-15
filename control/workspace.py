"""Workspace contract (SOR-83/B2, SOR-90).

An agent declares a workspace at create time — ``repo`` + ``base_ref`` +
``base_sha`` (``WorkspaceSpec``). The control plane clones the repo into a
sandbox-relative ``workdir`` and records what actually happened in a
``WorkspaceRecord``: ``checkout_sha`` (the commit checked out at prepare
time), ``head_sha`` (the current working head after agent commits or a
handoff apply) and ``reviewed_head_sha`` (the exact head an independent
reviewer signed off, pinned so the reviewed version cannot drift from the
report).

The declared ``base_sha`` is authoritative: when ``base_ref`` resolves to a
different commit, ``WorkspaceService.prepare`` raises ``base_sha_mismatch``
— a run never silently starts on the wrong code version.

Git runs inside the sandbox through ``SandboxBackend.exec``, so one code
path serves ``LocalProcessBackend`` (host subprocess; used by tests) and
Modal sandboxes (the runtime image must ship ``git``). Artifact-based
handoff lives in ``control.handoff``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from control.backend import Process, SandboxBackend, SandboxHandle
from control.sandbox_io import drain, is_local_root, sandbox_env

WORKSPACES_DICT_NAME = "sbx-workspaces"

# Machine-readable failure codes. ``base_sha_mismatch`` is the contract's
# core guarantee: any gap between the declared base and the real repo state
# fails loudly instead of running on the wrong version.
WORKSPACE_INVALID = "workspace_invalid"
WORKSPACE_NOT_FOUND = "workspace_not_found"
REPO_UNAVAILABLE = "repo_unavailable"
CHECKOUT_FAILED = "checkout_failed"
BASE_SHA_MISMATCH = "base_sha_mismatch"
HEAD_SHA_MISMATCH = "head_sha_mismatch"
CHECKSUM_MISMATCH = "checksum_mismatch"
ARTIFACT_NOT_FOUND = "artifact_not_found"
ARTIFACT_INVALID = "artifact_invalid"
WORKSPACE_ERROR_CODES = (
    WORKSPACE_INVALID,
    WORKSPACE_NOT_FOUND,
    REPO_UNAVAILABLE,
    CHECKOUT_FAILED,
    BASE_SHA_MISMATCH,
    HEAD_SHA_MISMATCH,
    CHECKSUM_MISMATCH,
    ARTIFACT_NOT_FOUND,
    ARTIFACT_INVALID,
)

_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

DEFAULT_WORKDIR = "repo"


class WorkspaceError(Exception):
    """Explicit workspace/handoff failure; ``code`` ∈ WORKSPACE_ERROR_CODES."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def is_commit_sha(value: Any) -> bool:
    return isinstance(value, str) and _COMMIT_SHA_RE.fullmatch(value) is not None


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def is_safe_relpath(value: Any) -> bool:
    """Sandbox-relative path that cannot escape the sandbox root/workdir."""
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def require_relpath(value: Any, *, code: str, what: str) -> str:
    if not is_safe_relpath(value):
        raise WorkspaceError(code, f"{what} must be a relative path inside the sandbox: {value!r}")
    return str(PurePosixPath(value))


@dataclass(frozen=True)
class WorkspaceSpec:
    """Client-declared workspace identity (agent create-time input).

    ``base_sha`` pins the exact commit the work is based on; ``base_ref``
    names where that commit is expected to sit. Validation happens here so a
    malformed declaration never reaches a sandbox.
    """

    repo: str
    base_ref: str
    base_sha: str

    def __post_init__(self) -> None:
        if not isinstance(self.repo, str) or not self.repo:
            raise WorkspaceError(WORKSPACE_INVALID, "workspace repo must be a non-empty string")
        if not isinstance(self.base_ref, str) or not self.base_ref:
            raise WorkspaceError(WORKSPACE_INVALID, "workspace base_ref must be a non-empty string")
        if not is_commit_sha(self.base_sha):
            raise WorkspaceError(
                WORKSPACE_INVALID,
                f"workspace base_sha must be a 40-hex commit sha: {self.base_sha!r}",
            )


@dataclass
class WorkspaceRecord:
    """Durable workspace state for one agent (one agent = one sandbox).

    ``checkout_sha``/``head_sha`` are None until ``prepare`` finishes — a
    record with only the declaration persisted means prepare never completed
    (the raise that interrupted it carried the reason).
    """

    agent_id: str
    repo: str
    base_ref: str
    base_sha: str
    workdir: str = DEFAULT_WORKDIR
    checkout_sha: str | None = None
    head_sha: str | None = None
    reviewed_head_sha: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def spec(self) -> WorkspaceSpec:
        return WorkspaceSpec(repo=self.repo, base_ref=self.base_ref, base_sha=self.base_sha)

    @property
    def prepared(self) -> bool:
        return self.checkout_sha is not None


def record_to_dict(record: WorkspaceRecord) -> dict[str, Any]:
    return asdict(record)


def record_from_dict(data: Any) -> WorkspaceRecord:
    """Strict-ish decode; raises ``ValueError`` on unusable payloads."""
    if not isinstance(data, dict):
        raise ValueError("workspace record is not a dict")
    record = WorkspaceRecord(
        agent_id=_required_str(data, "agent_id"),
        repo=_required_str(data, "repo"),
        base_ref=_required_str(data, "base_ref"),
        base_sha=_required_str(data, "base_sha"),
    )
    if not is_commit_sha(record.base_sha):
        raise ValueError("workspace record field base_sha must be a commit sha")
    workdir = data.get("workdir", DEFAULT_WORKDIR)
    if not is_safe_relpath(workdir):
        raise ValueError("workspace record field workdir must be a safe relative path")
    record.workdir = str(PurePosixPath(workdir))
    for key in ("checkout_sha", "head_sha", "reviewed_head_sha"):
        value = data.get(key)
        if value is not None and not is_commit_sha(value):
            raise ValueError(f"workspace record field {key} must be a commit sha")
        setattr(record, key, value)
    for key in ("created_at", "updated_at"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"workspace record field {key} must be a string")
        setattr(record, key, value or "")
    return record


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"workspace record missing {key}")
    return value


@runtime_checkable
class WorkspaceStore(Protocol):
    """Persistence for ``WorkspaceRecord``s, keyed by agent id."""

    def get(self, agent_id: str) -> WorkspaceRecord | None:
        """Return the record, or None when absent."""

    def put(self, record: WorkspaceRecord) -> None:
        """Insert or replace a record."""

    def delete(self, agent_id: str) -> None:
        """Remove a record (retry of a failed prepare)."""


class InMemoryWorkspaceStore:
    """Thread-safe dict store for tests and ephemeral deployments."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str) -> WorkspaceRecord | None:
        with self._lock:
            raw = self._items.get(agent_id)
        return record_from_dict(raw) if raw is not None else None

    def put(self, record: WorkspaceRecord) -> None:
        with self._lock:
            self._items[record.agent_id] = record_to_dict(record)

    def delete(self, agent_id: str) -> None:
        with self._lock:
            self._items.pop(agent_id, None)


class FileWorkspaceStore:
    """Local durable store: ``root/<agent_id>/workspace.json`` (atomic writes)."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _path(self, agent_id: str) -> Path:
        return self._root / agent_id / "workspace.json"

    def get(self, agent_id: str) -> WorkspaceRecord | None:
        try:
            raw = json.loads(self._path(agent_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"stored workspace record for {agent_id} is corrupt: {exc}"
            ) from exc
        try:
            return record_from_dict(raw)
        except ValueError as exc:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"stored workspace record for {agent_id} is corrupt: {exc}"
            ) from exc

    def put(self, record: WorkspaceRecord) -> None:
        path = self._path(record.agent_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record_to_dict(record), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)

    def delete(self, agent_id: str) -> None:
        with self._lock:
            self._path(agent_id).unlink(missing_ok=True)


class ModalDictWorkspaceStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal."""

    def __init__(self, name: str = WORKSPACES_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def get(self, agent_id: str) -> WorkspaceRecord | None:
        raw = self._d().get(agent_id)
        return record_from_dict(raw) if raw is not None else None

    def put(self, record: WorkspaceRecord) -> None:
        self._d().put(record.agent_id, record_to_dict(record))

    def delete(self, agent_id: str) -> None:
        try:
            self._d().pop(agent_id)
        except KeyError:
            return


@dataclass(frozen=True)
class GitResult:
    code: int
    lines: list[str]


def run_git(
    backend: SandboxBackend,
    handle: SandboxHandle,
    args: list[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> GitResult:
    """Run ``git -C <dir> <args>`` inside the sandbox.

    ``cwd`` is relative to the sandbox root (default: the root itself).
    ``stderr`` is not part of the backend contract — callers branch on the
    exit code and stdout lines only.
    """
    directory = str(handle.root / cwd) if cwd else str(handle.root)
    proc = backend.exec(handle, ["git", "-C", directory, *args], env=sandbox_env(handle, env))
    return _collect(proc)


def _collect(proc: Process) -> GitResult:
    lines = list(proc.stdout)
    return GitResult(proc.wait(), lines)


def git_rev_parse(
    backend: SandboxBackend, handle: SandboxHandle, workdir: str, ref: str
) -> str | None:
    """Resolve ``ref`` to a commit sha inside ``workdir``; None if it doesn't."""
    res = run_git(backend, handle, ["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=workdir)
    if res.code != 0 or not res.lines:
        return None
    sha = res.lines[0].strip()
    return sha if is_commit_sha(sha) else None


def git_head(backend: SandboxBackend, handle: SandboxHandle, workdir: str) -> str | None:
    return git_rev_parse(backend, handle, workdir, "HEAD")


def git_checkout(backend: SandboxBackend, handle: SandboxHandle, workdir: str, sha: str) -> None:
    res = run_git(backend, handle, ["checkout", "--detach", sha], cwd=workdir)
    if res.code != 0:
        raise WorkspaceError(
            CHECKOUT_FAILED, f"git checkout {sha} failed in {workdir} (exit {res.code})"
        )


def git_is_ancestor(
    backend: SandboxBackend, handle: SandboxHandle, workdir: str, base: str, head: str
) -> bool:
    """True when ``base`` is an ancestor of (or equal to) ``head``."""
    res = run_git(backend, handle, ["merge-base", "--is-ancestor", base, head], cwd=workdir)
    return res.code == 0


def write_payload(
    backend: SandboxBackend, handle: SandboxHandle, relative: str, data: bytes
) -> None:
    """Write binary ``data`` at a sandbox-root-relative path (mirrors
    ``sandbox_io.write_file`` for bytes)."""
    path = handle.root / relative
    if is_local_root(handle):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return
    encoded = base64.b64encode(data).decode("ascii")
    script = (
        "import base64, pathlib;"
        f"p=pathlib.Path({str(path)!r});"
        "p.parent.mkdir(parents=True, exist_ok=True);"
        f"p.write_bytes(base64.b64decode({encoded!r}))"
    )
    proc = backend.exec(handle, ["python3", "-c", script], env=sandbox_env(handle))
    if drain(proc) != 0:
        raise WorkspaceError(
            CHECKOUT_FAILED, f"failed to stage payload {relative} in sandbox {handle.id}"
        )


def sha256_file(backend: SandboxBackend, handle: SandboxHandle, relative: str) -> str | None:
    """sha256 hex of a sandbox file; None when the file is absent/unreadable."""
    path = handle.root / relative
    if is_local_root(handle):
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
    script = (
        "import hashlib, pathlib, sys;"
        f"p=pathlib.Path({str(path)!r});"
        "sys.stdout.write(hashlib.sha256(p.read_bytes()).hexdigest()) "
        "if p.is_file() else sys.exit(3)"
    )
    proc = backend.exec(handle, ["python3", "-c", script], env=sandbox_env(handle))
    res = _collect(proc)
    if res.code != 0 or not res.lines:
        return None
    digest = res.lines[0].strip()
    return digest if is_sha256(digest) else None


class WorkspaceService:
    """Declare + prepare workspaces; records the actual checkout state.

    ``prepare`` is explicit-failure: any gap between the declared
    ``base_sha`` and the real repo state raises ``WorkspaceError`` and leaves
    the record without ``checkout_sha`` — it never proceeds on a mismatched
    version.
    """

    def __init__(
        self,
        backend: SandboxBackend,
        store: WorkspaceStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def backend(self) -> SandboxBackend:
        return self._backend

    @property
    def store(self) -> WorkspaceStore:
        return self._store

    def _now(self) -> str:
        return self._clock().isoformat()

    def get(self, agent_id: str) -> WorkspaceRecord | None:
        return self._store.get(agent_id)

    def save(self, record: WorkspaceRecord) -> WorkspaceRecord:
        record.updated_at = self._now()
        self._store.put(record)
        return record

    def drop(self, agent_id: str) -> None:
        """Delete a record so a failed ``prepare`` can be retried."""
        self._store.delete(agent_id)

    def prepare(
        self,
        handle: SandboxHandle,
        agent_id: str,
        spec: WorkspaceSpec,
        *,
        workdir: str = DEFAULT_WORKDIR,
    ) -> WorkspaceRecord:
        """Clone ``spec.repo`` into ``workdir``, verify the declared base,
        check it out, and record the actual ``checkout_sha``/``head_sha``.

        The declaration is persisted before any git work runs, so a failed
        prepare still leaves an inspectable (unprepared) record.
        """
        workdir = require_relpath(workdir, code=WORKSPACE_INVALID, what="workdir")
        if self._store.get(agent_id) is not None:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"workspace already declared for agent {agent_id}"
            )
        now = self._now()
        record = WorkspaceRecord(
            agent_id=agent_id,
            repo=spec.repo,
            base_ref=spec.base_ref,
            base_sha=spec.base_sha,
            workdir=workdir,
            created_at=now,
            updated_at=now,
        )
        self._store.put(record)
        self._clone(handle, spec.repo, workdir)
        resolved = self._resolve_ref(handle, workdir, spec.base_ref)
        if resolved != spec.base_sha:
            raise WorkspaceError(
                BASE_SHA_MISMATCH,
                f"declared base_sha {spec.base_sha} does not match {spec.base_ref} at {resolved}",
            )
        git_checkout(self._backend, handle, workdir, spec.base_sha)
        actual = git_head(self._backend, handle, workdir)
        if actual != spec.base_sha:
            raise WorkspaceError(
                BASE_SHA_MISMATCH,
                f"checkout drifted: HEAD is {actual}, expected {spec.base_sha}",
            )
        record.checkout_sha = actual
        record.head_sha = actual
        return self.save(record)

    def refresh_head(self, handle: SandboxHandle, agent_id: str) -> WorkspaceRecord:
        """Re-read the workdir HEAD after agent commits; updates ``head_sha``."""
        record = self._require(agent_id)
        if not record.prepared:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"workspace for agent {agent_id} is not prepared"
            )
        actual = git_head(self._backend, handle, record.workdir)
        if actual is None:
            raise WorkspaceError(
                CHECKOUT_FAILED, f"no HEAD in workdir {record.workdir} for agent {agent_id}"
            )
        record.head_sha = actual
        return self.save(record)

    def mark_reviewed(self, agent_id: str, head_sha: str | None = None) -> WorkspaceRecord:
        """Pin ``reviewed_head_sha`` — the exact version a reviewer signed off.

        Defaults to the recorded ``head_sha``. An explicit value must equal
        the recorded head: reviewing anything else is ``head_sha_mismatch``
        rather than a silently mislabeled review.
        """
        record = self._require(agent_id)
        sha = head_sha or record.head_sha
        if sha is None:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"workspace for agent {agent_id} has no head to review"
            )
        if not is_commit_sha(sha):
            raise WorkspaceError(
                WORKSPACE_INVALID, f"reviewed head must be a 40-hex commit sha: {sha!r}"
            )
        if record.head_sha is not None and sha != record.head_sha:
            raise WorkspaceError(
                HEAD_SHA_MISMATCH,
                f"reviewed head {sha} does not match workspace head {record.head_sha}",
            )
        record.reviewed_head_sha = sha
        return self.save(record)

    def _require(self, agent_id: str) -> WorkspaceRecord:
        record = self._store.get(agent_id)
        if record is None:
            raise WorkspaceError(WORKSPACE_NOT_FOUND, f"no workspace for agent {agent_id}")
        return record

    def _clone(self, handle: SandboxHandle, repo: str, workdir: str) -> None:
        res = run_git(self._backend, handle, ["clone", "--", repo, workdir])
        if res.code != 0:
            raise WorkspaceError(REPO_UNAVAILABLE, f"git clone {repo!r} failed (exit {res.code})")

    def _resolve_ref(self, handle: SandboxHandle, workdir: str, base_ref: str) -> str:
        """Resolve ``base_ref`` to a commit sha in the fresh clone.

        After ``git clone`` the default branch exists locally; other branches
        live under ``origin/``. Both forms are tried, then the verbatim ref
        (``refs/...``, tags, ``origin/x``) already covers the rest.
        """
        for candidate in (base_ref, f"origin/{base_ref}"):
            sha = git_rev_parse(self._backend, handle, workdir, candidate)
            if sha is not None:
                return sha
        raise WorkspaceError(CHECKOUT_FAILED, f"base_ref {base_ref!r} does not resolve to a commit")


__all__ = [
    "ARTIFACT_INVALID",
    "ARTIFACT_NOT_FOUND",
    "BASE_SHA_MISMATCH",
    "CHECKOUT_FAILED",
    "CHECKSUM_MISMATCH",
    "DEFAULT_WORKDIR",
    "FileWorkspaceStore",
    "GitResult",
    "HEAD_SHA_MISMATCH",
    "InMemoryWorkspaceStore",
    "ModalDictWorkspaceStore",
    "REPO_UNAVAILABLE",
    "WORKSPACE_ERROR_CODES",
    "WORKSPACE_INVALID",
    "WORKSPACE_NOT_FOUND",
    "WORKSPACES_DICT_NAME",
    "WorkspaceError",
    "WorkspaceRecord",
    "WorkspaceService",
    "WorkspaceSpec",
    "WorkspaceStore",
    "git_checkout",
    "git_head",
    "git_is_ancestor",
    "git_rev_parse",
    "is_commit_sha",
    "is_safe_relpath",
    "is_sha256",
    "record_from_dict",
    "record_to_dict",
    "require_relpath",
    "run_git",
    "sha256_file",
    "write_payload",
]
