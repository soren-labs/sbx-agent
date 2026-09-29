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
import shlex
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from control import github
from control.backend import Process, SandboxBackend, SandboxHandle
from control.config import WORKSPACES_DICT_NAME, env_float
from control.latency import observe
from control.run_errors import clip_message
from control.sandbox_io import drain, is_local_root, sandbox_env

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
REVIEW_REQUIRED = "review_required"
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
    REVIEW_REQUIRED,
)

_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

# ``git check-ref-format`` essentials: no option-like leading '-', no control
# chars / space / ~^:?*[\, no '..' / '@{' / trailing '.lock', no empty or
# dot-prefixed components, no leading/trailing '/' or trailing '.'.  Shell
# metacharacters `; $ ' `` are refused too — refs only ever reach git via
# argv, but nothing legitimate uses them.
_REF_FORBIDDEN = re.compile(r"[\x00-\x20 ~^:?*\[\\;$'`]|\.\.|\@\{")

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


def is_safe_ref(value: Any) -> bool:
    """Whether ``value`` is a usable git ref name (branch / fetch refspec src).

    Mirrors ``git check-ref-format``'s load-bearing rules so a declared
    branch, target or PR ref can never smuggle an option, a path escape or
    config syntax into a git invocation.
    """
    if not isinstance(value, str) or not value:
        return False
    if value == "@" or value.startswith(("-", "/")) or value.endswith(("/", ".")):
        return False
    if _REF_FORBIDDEN.search(value):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock")
        for part in value.split("/")
    )


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


# SOR-128 git collaboration policy keys (``git`` on agent create). The
# resolved policy is persisted verbatim on the workspace record so a later
# publish needs no restating — and so drift between declaration and record
# is inspectable.
GIT_POLICY_KEYS = (
    "branch",
    "push",
    "auto_create_pr",
    "auto_publish",
    "merge",
    "target",
    "draft",
    "title",
    "body",
)


def validate_git_policy(git: Any) -> None:
    """Shape/cross-field check of a declared git policy dict.

    Route-time validation: refuses a non-dict, unknown keys, unsafe ref
    names and ``auto_create_pr`` without ``push`` before any sandbox work
    starts. ``WorkspaceError(WORKSPACE_INVALID)`` on violation.
    """
    if not isinstance(git, dict):
        raise WorkspaceError(WORKSPACE_INVALID, "git policy must be an object")
    unknown = set(git) - set(GIT_POLICY_KEYS)
    if unknown:
        raise WorkspaceError(WORKSPACE_INVALID, f"unknown git policy keys: {sorted(unknown)!r}")
    if git.get("auto_create_pr") and not git.get("push"):
        raise WorkspaceError(WORKSPACE_INVALID, "git.auto_create_pr requires git.push")
    if git.get("auto_publish") and not git.get("push"):
        raise WorkspaceError(WORKSPACE_INVALID, "git.auto_publish requires git.push")
    if git.get("merge") and not git.get("auto_create_pr"):
        # A mergeable policy needs a recorded pull request; only
        # auto_create_pr produces one.
        raise WorkspaceError(WORKSPACE_INVALID, "git.merge requires git.auto_create_pr")
    for key in ("branch", "target"):
        value = git.get(key)
        if value is not None and not is_safe_ref(value):
            raise WorkspaceError(
                WORKSPACE_INVALID, f"git.{key} is not a safe git ref name: {value!r}"
            )
    for key in ("title", "body"):
        value = git.get(key)
        if value is not None and not isinstance(value, str):
            raise WorkspaceError(WORKSPACE_INVALID, f"git.{key} must be a string")


def normalize_git_policy(git: Any, *, agent_id: str, base_ref: str) -> dict[str, Any] | None:
    """Resolve a declared git policy to its durable form (None passes through).

    ``branch`` defaults to ``sbx/<agent_id>`` and ``target`` to the
    workspace's declared ``base_ref`` — the record always carries the
    resolved values, never the caller's omissions.
    """
    if git is None:
        return None
    validate_git_policy(git)
    branch = git.get("branch") or f"sbx/{agent_id}"
    if not is_safe_ref(branch):
        raise WorkspaceError(
            WORKSPACE_INVALID, f"resolved git.branch is not a safe ref name: {branch!r}"
        )
    target = git.get("target") or base_ref
    return {
        "branch": branch,
        "push": bool(git.get("push")),
        "auto_create_pr": bool(git.get("auto_create_pr")),
        "auto_publish": bool(git.get("auto_publish")),
        "merge": bool(git.get("merge")),
        "target": target,
        "draft": bool(git.get("draft")),
        "title": git.get("title"),
        "body": git.get("body"),
    }


@dataclass
class WorkspaceRecord:
    """Durable workspace state for one agent (one agent = one sandbox).

    ``checkout_sha``/``head_sha`` are None until ``prepare`` finishes — a
    record with only the declaration persisted means prepare never completed
    (the raise that interrupted it carried the reason).

    SOR-128 fields: ``git`` is the resolved collaboration policy (None when
    the agent declared none); ``branch`` the work branch the workspace
    materializes and publishes; ``pushed_head_sha`` the head last verified
    on the remote; ``pull_request`` the structured PR metadata a publish
    recorded — ``{number, url, state, ref, head_sha, head_branch?, base,
    draft, review_comment_url?}``.

    SOR-178 fields: ``merge`` is the durable merge record a review-gated
    ``merge`` wrote — ``{merged, merge_commit_sha, head_sha, merged_at}``;
    ``publish_error`` is the last publish failure (explicit or automatic),
    cleared on the next successful publish — a failed auto-publish never
    rewrites the finished run's verdict but is never silent either.
    """

    agent_id: str
    repo: str
    base_ref: str
    base_sha: str
    workdir: str = DEFAULT_WORKDIR
    checkout_sha: str | None = None
    head_sha: str | None = None
    # Last materialization's ``git status --porcelain`` verdict — untracked/
    # modified content mints a patch revision without moving ``head_sha``,
    # so sha comparison alone cannot see it.
    dirty: bool = False
    reviewed_head_sha: str | None = None
    git: dict[str, Any] | None = None
    branch: str | None = None
    pushed_head_sha: str | None = None
    pull_request: dict[str, Any] | None = None
    merge: dict[str, Any] | None = None
    publish_error: str | None = None
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
    for key in ("checkout_sha", "head_sha", "reviewed_head_sha", "pushed_head_sha"):
        value = data.get(key)
        if value is not None and not is_commit_sha(value):
            raise ValueError(f"workspace record field {key} must be a commit sha")
        setattr(record, key, value)
    dirty = data.get("dirty", False)
    if not isinstance(dirty, bool):
        raise ValueError("workspace record field dirty must be a bool")
    record.dirty = dirty
    branch = data.get("branch")
    if branch is not None and not is_safe_ref(branch):
        raise ValueError("workspace record field branch must be a safe git ref")
    record.branch = branch
    record.git = _git_policy_from_dict(data.get("git"))
    record.pull_request = _pull_request_from_dict(data.get("pull_request"))
    record.merge = _merge_from_dict(data.get("merge"))
    publish_error = data.get("publish_error")
    if publish_error is not None and not isinstance(publish_error, str):
        raise ValueError("workspace record field publish_error must be a string")
    record.publish_error = publish_error
    for key in ("created_at", "updated_at"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"workspace record field {key} must be a string")
        setattr(record, key, value or "")
    return record


def _git_policy_from_dict(data: Any) -> dict[str, Any] | None:
    """Strict decode of a stored git policy; None passes through."""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("workspace record field git must be an object")
    unknown = set(data) - set(GIT_POLICY_KEYS)
    if unknown:
        raise ValueError(f"workspace record field git has unknown keys: {sorted(unknown)!r}")
    out: dict[str, Any] = {}
    for key in ("push", "auto_create_pr", "auto_publish", "merge", "draft"):
        value = data.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"workspace record field git.{key} must be a bool")
        out[key] = bool(value)
    for key in ("branch", "target", "title", "body"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"workspace record field git.{key} must be a string")
        out[key] = value
    return out


_PULL_REQUEST_KEYS = (
    "number",
    "url",
    "state",
    "ref",
    "head_sha",
    "head_branch",
    "base",
    "draft",
    "review_comment_url",
)


def _pull_request_from_dict(data: Any) -> dict[str, Any] | None:
    """Strict decode of stored PR metadata; None passes through."""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("workspace record field pull_request must be an object")
    unknown = set(data) - set(_PULL_REQUEST_KEYS)
    if unknown:
        raise ValueError(
            f"workspace record field pull_request has unknown keys: {sorted(unknown)!r}"
        )
    number = data.get("number")
    if number is not None and (isinstance(number, bool) or not isinstance(number, int)):
        raise ValueError("pull_request.number must be an int")
    head_sha = data.get("head_sha")
    if head_sha is not None and not is_commit_sha(head_sha):
        raise ValueError("pull_request.head_sha must be a commit sha")
    draft = data.get("draft")
    if draft is not None and not isinstance(draft, bool):
        raise ValueError("pull_request.draft must be a bool")
    out: dict[str, Any] = {"number": number, "head_sha": head_sha, "draft": bool(draft)}
    for key in ("url", "state", "ref", "head_branch", "base", "review_comment_url"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"pull_request.{key} must be a string")
        out[key] = value
    return out


_MERGE_KEYS = (
    "merged",
    "merge_commit_sha",
    "head_sha",
    "merged_at",
)


def _merge_from_dict(data: Any) -> dict[str, Any] | None:
    """Strict decode of stored merge metadata; None passes through."""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("workspace record field merge must be an object")
    unknown = set(data) - set(_MERGE_KEYS)
    if unknown:
        raise ValueError(f"workspace record field merge has unknown keys: {sorted(unknown)!r}")
    merged = data.get("merged")
    if merged is not None and not isinstance(merged, bool):
        raise ValueError("merge.merged must be a bool")
    out: dict[str, Any] = {"merged": bool(merged)}
    for key in ("merge_commit_sha", "head_sha"):
        value = data.get(key)
        if value is not None and not is_commit_sha(value):
            raise ValueError(f"merge.{key} must be a commit sha")
        out[key] = value
    merged_at = data.get("merged_at")
    if merged_at is not None and not isinstance(merged_at, str):
        raise ValueError("merge.merged_at must be a string")
    out["merged_at"] = merged_at
    return out


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

    def list_records(self) -> Iterable[tuple[str, dict[str, Any]]]:
        """All ``(agent_id, raw record)`` pairs for read-model listings.

        Dict-backed stores answer with one index-doc read instead of a
        full ``items()`` enumeration (SOR-271 round 2: the /v2 session
        list paid one workspace get per row, which scales with history)."""


class InMemoryWorkspaceStore:
    """Thread-safe dict store for tests and ephemeral deployments."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str) -> WorkspaceRecord | None:
        with self._lock:
            raw = self._items.get(agent_id)
        if raw is None:
            return None
        try:
            return record_from_dict(raw)
        except ValueError as exc:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"stored workspace record for {agent_id} is corrupt: {exc}"
            ) from exc

    def put(self, record: WorkspaceRecord) -> None:
        with self._lock:
            self._items[record.agent_id] = record_to_dict(record)

    def delete(self, agent_id: str) -> None:
        with self._lock:
            self._items.pop(agent_id, None)

    def list_records(self) -> Iterable[tuple[str, dict[str, Any]]]:
        with self._lock:
            return list(self._items.items())


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

    def list_records(self) -> Iterable[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(self._root.glob("*/workspace.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                out.append((path.parent.name, raw))
        return out


_WS_GET_CACHE_TTL_S = env_float("SBX_WS_GET_CACHE_TTL_S", 0.75)

# SOR-268 round 4: read-through TTL on the ``__workspaces__`` index doc.
# ``list_records`` feeds the /v2 list page's ws map and the SSE status
# path; repeat calls inside ~1s then cost zero remote ops. ``put`` /
# ``delete`` write-through the index they just composed — this process's
# own mutations never go stale; a cross-container write settles within
# the TTL.
_WS_LIST_CACHE_TTL_S = env_float("SBX_WS_LIST_CACHE_TTL_S", 1.0)


class ModalDictWorkspaceStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal.

    SOR-268: ~0.75s per-record read-through cache on ``get`` — the V2
    settle/view paths re-read the same workspace several times per
    request and the SSE hub ticks it; mutations go through
    ``get_fresh`` so a read-modify-write never runs on a cached row.

    SOR-271 round 2: workspace rows also ride a ``__workspaces__`` index
    doc written atomically by ``put`` — ``list_records`` is one point
    read, so the /v2 session list no longer pays a remote get per row.
    """

    _INDEX_KEY = "__workspaces__"

    def __init__(self, name: str = WORKSPACES_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None
        self._get_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._list_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._lock = threading.Lock()
        self._ilock = threading.Lock()

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def _get_uncached(self, agent_id: str) -> dict[str, Any] | None:
        with observe("modal_dict.get", store=self._name, key=agent_id):
            raw = self._d().get(agent_id)
        with self._lock:
            self._get_cache[agent_id] = (time.monotonic(), raw if isinstance(raw, dict) else None)
        return raw if isinstance(raw, dict) else None

    def _decode(self, raw: dict[str, Any] | None, agent_id: str) -> WorkspaceRecord | None:
        if raw is None:
            return None
        try:
            return record_from_dict(raw)
        except ValueError as exc:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"stored workspace record for {agent_id} is corrupt: {exc}"
            ) from exc

    def get(self, agent_id: str) -> WorkspaceRecord | None:
        with self._lock:
            cached = self._get_cache.get(agent_id)
        if cached is not None and time.monotonic() - cached[0] < _WS_GET_CACHE_TTL_S:
            raw = cached[1]
        else:
            raw = self._get_uncached(agent_id)
        return self._decode(raw, agent_id)

    def get_fresh(self, agent_id: str) -> WorkspaceRecord | None:
        """Uncached read for mutation paths (read-modify-write)."""
        return self._decode(self._get_uncached(agent_id), agent_id)

    @staticmethod
    def _batch(d: Any, writes: dict[str, Any]) -> None:
        """One-RPC multi-key write via ``Dict.update``; older clients
        degrade to per-key puts."""
        update = getattr(d, "update", None)
        if callable(update):
            update(writes)
            return
        for key, value in writes.items():
            d.put(key, value)

    def _read_index(self) -> dict[str, Any]:
        raw = self._d().get(self._INDEX_KEY)
        return dict(raw) if isinstance(raw, dict) else {}

    def put(self, record: WorkspaceRecord) -> None:
        raw = record_to_dict(record)
        # ``Dict.update`` carries the row and its index entry in one
        # atomic write — ``list_records`` never sees a torn pair.
        with self._ilock:
            index = self._read_index()
            index[record.agent_id] = dict(raw)
            with observe("modal_dict.put", store=self._name, key=record.agent_id):
                self._batch(self._d(), {record.agent_id: raw, self._INDEX_KEY: index})
        with self._lock:
            self._get_cache[record.agent_id] = (time.monotonic(), raw)
            self._list_cache = (time.monotonic(), index)

    def iter_records(self) -> Iterable[tuple[str, dict[str, Any]]]:
        """Full ``items()`` enumeration — migration/self-heal path only."""
        out: list[tuple[str, dict[str, Any]]] = []
        with observe("modal_dict.items", store=self._name):
            items: Iterator[tuple[Any, Any]] = self._d().items()
            for key, raw in items:
                if isinstance(key, str) and not key.startswith("__") and isinstance(raw, dict):
                    out.append((key, raw))
        return out

    def list_records(self) -> Iterable[tuple[str, dict[str, Any]]]:
        """One index-doc read instead of an ``items()`` full scan.

        A Dict predating the index falls back to ``iter_records`` once
        and self-heals the index doc; a dropped or corrupt index heals
        the same way on the next listing. Results memoize for
        ``_WS_LIST_CACHE_TTL_S`` (writes merge in) — mutation paths use
        ``list_records_fresh`` when they must see every entry."""
        with self._lock:
            hit = self._list_cache
        if hit is not None and time.monotonic() - hit[0] < _WS_LIST_CACHE_TTL_S:
            return list(hit[1].items())
        index = self._read_index_or_migrate()
        with self._lock:
            self._list_cache = (time.monotonic(), index)
        return list(index.items())

    def list_records_fresh(self) -> Iterable[tuple[str, dict[str, Any]]]:
        """Uncached index read for paths that must see every entry."""
        index = self._read_index_or_migrate()
        with self._lock:
            self._list_cache = (time.monotonic(), index)
        return list(index.items())

    def _read_index_or_migrate(self) -> dict[str, dict[str, Any]]:
        try:
            with observe("modal_dict.get", store=self._name, key=self._INDEX_KEY):
                raw = self._d().get(self._INDEX_KEY)
        except Exception:
            raw = None
        if isinstance(raw, dict):
            return raw
        records = dict(self.iter_records())
        try:
            with self._ilock:
                self._batch(
                    self._d(),
                    {self._INDEX_KEY: {agent_id: dict(rec) for agent_id, rec in records.items()}},
                )
        except Exception:
            pass
        return records

    def delete(self, agent_id: str) -> None:
        # Index first: a crash between the two writes leaves an orphaned
        # workspace row (invisible to listings, converged by the next
        # self-heal) — never an index entry pointing at a missing row.
        index: dict[str, dict[str, Any]] | None = None
        try:
            with self._ilock:
                index = self._read_index()
                if agent_id in index:
                    del index[agent_id]
                    self._batch(self._d(), {self._INDEX_KEY: index})
        except Exception:
            index = None
        if index is not None:
            with self._lock:
                self._list_cache = (time.monotonic(), index)
        try:
            with observe("modal_dict.pop", store=self._name, key=agent_id):
                self._d().pop(agent_id)
        except KeyError:
            pass
        with self._lock:
            self._get_cache.pop(agent_id, None)


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
    github_repo: str | None = None,
) -> GitResult:
    """Run ``git -C <dir> <args>`` inside the sandbox.

    ``cwd`` is relative to the sandbox root (default: the root itself).
    ``stderr`` is not part of the backend contract — callers branch on the
    exit code and stdout lines only. ``github_repo`` (clone URL or
    ``owner/repo``) lets the GitHub App bridge mint a repo-scoped token.
    """
    directory = str(handle.root / cwd) if cwd else str(handle.root)
    proc = backend.exec(
        handle,
        ["git", "-C", directory, *args],
        env=sandbox_env(handle, env, github_repo=github_repo),
    )
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


def git_checkout(
    backend: SandboxBackend,
    handle: SandboxHandle,
    workdir: str,
    sha: str,
    *,
    branch: str | None = None,
) -> None:
    """Check out ``sha`` — onto ``branch`` (``-B``, created/reset) when the
    workspace's git policy declares one, detached otherwise."""
    if branch is not None:
        if not is_safe_ref(branch):
            raise WorkspaceError(WORKSPACE_INVALID, f"unsafe branch name: {branch!r}")
        res = run_git(backend, handle, ["checkout", "-B", branch, sha], cwd=workdir)
    else:
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


def git_fetch_ref(
    backend: SandboxBackend,
    handle: SandboxHandle,
    workdir: str,
    ref: str,
    *,
    remote: str = "origin",
    github_repo: str | None = None,
) -> str:
    """Fetch ``ref`` from ``remote`` into ``FETCH_HEAD``; return its commit sha.

    Covers PR refs (``refs/pull/<n>/head`` / ``pull/<n>/head``) and branch
    names alike — the refspec is passed to ``git fetch`` verbatim after the
    ref-name safety check. A fetch failure is ``REPO_UNAVAILABLE``; a
    fetched object that is not a commit is ``CHECKOUT_FAILED``.
    """
    if not is_safe_ref(ref):
        raise WorkspaceError(WORKSPACE_INVALID, f"unsafe fetch ref: {ref!r}")
    if remote.startswith("-"):
        raise WorkspaceError(WORKSPACE_INVALID, "fetch remote must not look like an option")
    res = run_git(
        backend,
        handle,
        ["fetch", "--no-tags", remote, ref],
        cwd=workdir,
        github_repo=github_repo,
    )
    if res.code != 0:
        raise WorkspaceError(
            REPO_UNAVAILABLE,
            f"git fetch {remote} {ref} failed in {workdir} (exit {res.code})",
        )
    sha = git_rev_parse(backend, handle, workdir, "FETCH_HEAD")
    if sha is None:
        raise WorkspaceError(CHECKOUT_FAILED, f"fetched ref {ref} did not resolve to a commit")
    return sha


def git_ls_remote(
    backend: SandboxBackend,
    handle: SandboxHandle,
    workdir: str,
    ref: str,
    *,
    remote: str = "origin",
    github_repo: str | None = None,
) -> str | None:
    """Resolve ``ref`` on ``remote`` (``git ls-remote``); None when absent."""
    if not is_safe_ref(ref):
        raise WorkspaceError(WORKSPACE_INVALID, f"unsafe remote ref: {ref!r}")
    if remote.startswith("-"):
        raise WorkspaceError(WORKSPACE_INVALID, "ls-remote must not look like an option")
    res = run_git(backend, handle, ["ls-remote", remote, ref], cwd=workdir, github_repo=github_repo)
    if res.code != 0:
        raise WorkspaceError(
            REPO_UNAVAILABLE, f"git ls-remote {remote} {ref} failed (exit {res.code})"
        )
    for line in res.lines:
        parts = line.split("\t", 1) if "\t" in line else line.split(None, 1)
        if len(parts) == 2 and parts[1].strip() == ref:
            sha = parts[0].strip()
            return sha if is_commit_sha(sha) else None
    return None


def git_push(
    backend: SandboxBackend,
    handle: SandboxHandle,
    workdir: str,
    refspec: str,
    *,
    remote: str = "origin",
    github_repo: str | None = None,
) -> None:
    """Push ``refspec`` (e.g. ``HEAD:refs/heads/sbx/review``) to ``remote``.

    For github.com remotes the opt-in ``SBX_GITHUB_EPHEMERAL`` bridge (when
    armed) supplies the credential helper via the exec env — the token never
    appears in argv or on disk. ``GIT_TERMINAL_PROMPT=0`` (set by
    ``sandbox_env``) makes a missing credential a fast explicit failure.
    """
    if remote.startswith("-") or refspec.startswith("-"):
        raise WorkspaceError(WORKSPACE_INVALID, "push remote/refspec must not look like an option")
    res = run_git(backend, handle, ["push", remote, refspec], cwd=workdir, github_repo=github_repo)
    if res.code != 0:
        raise WorkspaceError(
            REPO_UNAVAILABLE, f"git push {remote} {refspec} failed (exit {res.code})"
        )


def create_pull_request(
    backend: SandboxBackend,
    handle: SandboxHandle,
    repo: str,
    *,
    head: str,
    base: str,
    title: str,
    body: str = "",
    draft: bool = False,
) -> dict[str, Any]:
    """Open a GitHub pull request from inside the sandbox via the REST API.

    ``repo`` is the declared clone URL (``https://github.com/owner/repo`` or
    ``git@github.com:owner/repo``); ``head`` is a branch already pushed to
    ``origin`` (see :func:`git_push`). Requires the opt-in GitHub bridge —
    the request runs as ``curl`` under ``bash -c`` so ``$GH_TOKEN`` expands
    inside the sandbox env and never appears in argv.
    """
    if not github.injection_enabled():
        raise WorkspaceError(
            REPO_UNAVAILABLE,
            "GitHub injection is off — export SBX_GITHUB_EPHEMERAL=1 with a "
            "GH_TOKEN/GITHUB_TOKEN to open pull requests from sandboxes",
        )
    slug = github.repo_slug(repo)
    if slug is None:
        raise WorkspaceError(
            WORKSPACE_INVALID,
            f"not a github.com repo URL: {github.redact_url_credentials(repo)!r}",
        )
    payload = json.dumps({"title": title, "head": head, "base": base, "body": body, "draft": draft})
    script = (
        "curl -sS -X POST "
        f"https://api.github.com/repos/{slug}/pulls "
        '-H "Accept: application/vnd.github+json" '
        '-H "Authorization: Bearer $GH_TOKEN" '
        f"--data {shlex.quote(payload)} "
        "-w '\\n%{http_code}'"
    )
    proc = backend.exec(handle, ["bash", "-c", script], env=sandbox_env(handle, github_repo=repo))
    lines = list(proc.stdout)
    code = proc.wait()
    http_code = lines[-1].strip() if lines else ""
    text = "\n".join(lines[:-1])
    if code != 0 or not http_code.isdigit() or not (200 <= int(http_code) < 300):
        # The response body is GitHub's, but it lands on a run record — clip
        # + redact it through the same seam as run errors (SOR-82) so no
        # token-shaped fragment can echo back.
        detail = clip_message(text)[:200]
        raise WorkspaceError(
            REPO_UNAVAILABLE,
            f"GitHub PR create for {slug} failed "
            f"(exit {code}, http {http_code or '?'})" + (f": {detail}" if detail else ""),
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkspaceError(
            REPO_UNAVAILABLE, f"GitHub PR create for {slug} returned no JSON"
        ) from exc
    return data if isinstance(data, dict) else {"result": data}


def create_issue_comment(
    backend: SandboxBackend,
    handle: SandboxHandle,
    repo: str,
    *,
    number: int,
    body: str,
) -> dict[str, Any]:
    """Post a comment on a GitHub issue/PR from inside the sandbox.

    This is the SOR-128 review surface: a machine-readable *comment*, never
    a formal ``/reviews`` approval — every sandbox shares one GitHub
    identity, so an approval would read as the PR author approving their own
    work. Same opt-in + token-safety rules as :func:`create_pull_request`.
    """
    if not github.injection_enabled():
        raise WorkspaceError(
            REPO_UNAVAILABLE,
            "GitHub injection is off — export SBX_GITHUB_EPHEMERAL=1 with a "
            "GH_TOKEN/GITHUB_TOKEN to comment on pull requests from sandboxes",
        )
    slug = github.repo_slug(repo)
    if slug is None:
        raise WorkspaceError(
            WORKSPACE_INVALID,
            f"not a github.com repo URL: {github.redact_url_credentials(repo)!r}",
        )
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise WorkspaceError(WORKSPACE_INVALID, f"invalid PR/issue number: {number!r}")
    payload = json.dumps({"body": body})
    script = (
        "curl -sS -X POST "
        f"https://api.github.com/repos/{slug}/issues/{number}/comments "
        '-H "Accept: application/vnd.github+json" '
        '-H "Authorization: Bearer $GH_TOKEN" '
        f"--data {shlex.quote(payload)} "
        "-w '\\n%{http_code}'"
    )
    proc = backend.exec(handle, ["bash", "-c", script], env=sandbox_env(handle, github_repo=repo))
    lines = list(proc.stdout)
    code = proc.wait()
    http_code = lines[-1].strip() if lines else ""
    text = "\n".join(lines[:-1])
    if code != 0 or not http_code.isdigit() or not (200 <= int(http_code) < 300):
        detail = clip_message(text)[:200]
        raise WorkspaceError(
            REPO_UNAVAILABLE,
            f"GitHub comment on {slug}#{number} failed "
            f"(exit {code}, http {http_code or '?'})" + (f": {detail}" if detail else ""),
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkspaceError(
            REPO_UNAVAILABLE, f"GitHub comment on {slug}#{number} returned no JSON"
        ) from exc
    return data if isinstance(data, dict) else {"result": data}


def merge_pull_request(
    backend: SandboxBackend,
    handle: SandboxHandle,
    repo: str,
    *,
    number: int,
    sha: str,
) -> dict[str, Any]:
    """Merge a GitHub pull request from inside the sandbox (SOR-178).

    The ``sha`` argument is sent as GitHub's required head-sha pin: GitHub
    itself refuses the merge when the PR head moved since ``sha``, making
    the server-side check a second fail-closed gate behind the control
    plane's own reviewed-head comparison. Same opt-in + token-safety rules
    as :func:`create_pull_request`.
    """
    if not github.injection_enabled():
        raise WorkspaceError(
            REPO_UNAVAILABLE,
            "GitHub injection is off — export SBX_GITHUB_EPHEMERAL=1 with a "
            "GH_TOKEN/GITHUB_TOKEN to merge pull requests from sandboxes",
        )
    slug = github.repo_slug(repo)
    if slug is None:
        raise WorkspaceError(
            WORKSPACE_INVALID,
            f"not a github.com repo URL: {github.redact_url_credentials(repo)!r}",
        )
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise WorkspaceError(WORKSPACE_INVALID, f"invalid PR number: {number!r}")
    if not is_commit_sha(sha):
        raise WorkspaceError(WORKSPACE_INVALID, f"merge sha must be a commit sha: {sha!r}")
    payload = json.dumps({"sha": sha, "merge_method": "merge"})
    script = (
        "curl -sS -X PUT "
        f"https://api.github.com/repos/{slug}/pulls/{number}/merge "
        '-H "Accept: application/vnd.github+json" '
        '-H "Authorization: Bearer $GH_TOKEN" '
        f"--data {shlex.quote(payload)} "
        "-w '\\n%{http_code}'"
    )
    proc = backend.exec(handle, ["bash", "-c", script], env=sandbox_env(handle, github_repo=repo))
    lines = list(proc.stdout)
    code = proc.wait()
    http_code = lines[-1].strip() if lines else ""
    text = "\n".join(lines[:-1])
    if code != 0 or not http_code.isdigit() or not (200 <= int(http_code) < 300):
        detail = clip_message(text)[:200]
        raise WorkspaceError(
            REPO_UNAVAILABLE,
            f"GitHub merge of {slug}#{number} failed "
            f"(exit {code}, http {http_code or '?'})" + (f": {detail}" if detail else ""),
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkspaceError(
            REPO_UNAVAILABLE, f"GitHub merge of {slug}#{number} returned no JSON"
        ) from exc
    return data if isinstance(data, dict) else {"result": data}


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
            if not path.is_file() or path.is_symlink():
                return None
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

    def list_records(self) -> Iterable[tuple[str, dict[str, Any]]]:
        """Index-backed raw-record listing — delegates to the store so a
        caller holding only the service still gets the bounded read."""
        return self._store.list_records()

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
        git: dict[str, Any] | None = None,
    ) -> WorkspaceRecord:
        """Clone ``spec.repo`` into ``workdir``, verify the declared base,
        check it out, and record the actual ``checkout_sha``/``head_sha``.

        The declaration is persisted before any git work runs, so a failed
        prepare still leaves an inspectable (unprepared) record.

        SOR-128: ``git`` is the create-time collaboration policy. It is
        resolved (``branch`` defaults to ``sbx/<agent_id>``, ``target`` to
        ``base_ref``) and persisted on the record; the checkout lands on the
        work branch so a later ``publish`` pushes where the policy declared.
        """
        workdir = require_relpath(workdir, code=WORKSPACE_INVALID, what="workdir")
        policy = normalize_git_policy(git, agent_id=agent_id, base_ref=spec.base_ref)
        if self._get_fresh(agent_id) is not None:
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
            git=policy,
            branch=(policy or {}).get("branch"),
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
        git_checkout(self._backend, handle, workdir, spec.base_sha, branch=record.branch)
        actual = git_head(self._backend, handle, workdir)
        if actual != spec.base_sha:
            raise WorkspaceError(
                BASE_SHA_MISMATCH,
                f"checkout drifted: HEAD is {actual}, expected {spec.base_sha}",
            )
        record.checkout_sha = actual
        record.head_sha = actual
        return self.save(record)

    def prepare_restored(
        self,
        handle: SandboxHandle,
        agent_id: str,
        spec: WorkspaceSpec,
        *,
        workdir: str = DEFAULT_WORKDIR,
        git: dict[str, Any] | None = None,
    ) -> WorkspaceRecord:
        """Record + verify a workspace restored from a prepared environment
        snapshot (SOR-127) instead of freshly cloned.

        No clone runs — the snapshot carries the repo — but the declared
        ``base_sha`` gate is identical: the restored workdir HEAD must
        resolve to exactly ``base_sha``, or the prepare fails closed with
        ``base_sha_mismatch`` and the record stays unprepared. The SOR-128
        git policy still resolves/persists and the work branch is created
        at the pinned sha, so a restored workspace is indistinguishable
        from a fresh ``prepare`` to downstream consumers.
        """
        workdir = require_relpath(workdir, code=WORKSPACE_INVALID, what="workdir")
        policy = normalize_git_policy(git, agent_id=agent_id, base_ref=spec.base_ref)
        if self._get_fresh(agent_id) is not None:
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
            git=policy,
            branch=(policy or {}).get("branch"),
            created_at=now,
            updated_at=now,
        )
        self._store.put(record)
        actual = git_head(self._backend, handle, workdir)
        if actual != spec.base_sha:
            raise WorkspaceError(
                BASE_SHA_MISMATCH,
                f"restored environment HEAD is {actual}, expected {spec.base_sha}",
            )
        if record.branch is not None:
            git_checkout(self._backend, handle, workdir, spec.base_sha, branch=record.branch)
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

    def publish(self, handle: SandboxHandle, agent_id: str) -> WorkspaceRecord:
        """Execute the recorded git policy: push the work branch, then open
        the declared pull request.

        Fails closed at every step: no push-enabled policy is
        ``workspace_invalid``; a failed push/ls-remote/PR call is
        ``repo_unavailable``; a remote head that disagrees with what was
        pushed is an explicit ``repo_unavailable`` rather than a silently
        recorded drift. ``pushed_head_sha`` is only recorded after the
        remote verifies it — it is the sha a reviewer can pin.
        """
        record = self._require(agent_id)
        if not record.prepared:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"workspace for agent {agent_id} is not prepared"
            )
        policy = record.git
        if not policy or not policy.get("push"):
            raise WorkspaceError(
                WORKSPACE_INVALID,
                f"agent {agent_id} declared no push-enabled git policy",
            )
        branch = record.branch or policy.get("branch") or f"sbx/{agent_id}"
        if not is_safe_ref(branch):
            raise WorkspaceError(WORKSPACE_INVALID, f"unsafe branch name: {branch!r}")
        try:
            head = git_head(self._backend, handle, record.workdir)
            if head is None:
                raise WorkspaceError(
                    CHECKOUT_FAILED, f"no HEAD in workdir {record.workdir} for agent {agent_id}"
                )
            git_push(
                self._backend,
                handle,
                record.workdir,
                f"HEAD:refs/heads/{branch}",
                github_repo=record.repo,
            )
            remote_sha = git_ls_remote(
                self._backend,
                handle,
                record.workdir,
                f"refs/heads/{branch}",
                github_repo=record.repo,
            )
            if remote_sha != head:
                raise WorkspaceError(
                    REPO_UNAVAILABLE,
                    f"pushed {branch} but remote resolves to {remote_sha}, expected {head}",
                )
            record.head_sha = head
            record.branch = branch
            record.pushed_head_sha = head
            # The verified push is a durable fact — persist it before the PR
            # step so a PR failure never masks where the head actually landed.
            self.save(record)
            if policy.get("auto_create_pr"):
                pr = record.pull_request
                if (
                    pr is None
                    or pr.get("state") in ("merged", "closed")
                    # A recorded PR only applies to the branch it was
                    # published under — a branch switch must not graft the
                    # old PR's number onto this push.
                    or (pr.get("head_branch") or branch) != branch
                ):
                    # A terminal PR cannot track new work — open a fresh one.
                    data = create_pull_request(
                        self._backend,
                        handle,
                        record.repo,
                        head=branch,
                        base=str(policy.get("target") or record.base_ref),
                        title=str(policy.get("title") or f"sbx {agent_id}"),
                        body=str(policy.get("body") or ""),
                        draft=bool(policy.get("draft")),
                    )
                    number = data.get("number")
                    record.pull_request = {
                        "number": number if isinstance(number, int) else None,
                        "url": data.get("html_url"),
                        "state": data.get("state") or "open",
                        "ref": (f"refs/pull/{number}/head" if isinstance(number, int) else None),
                        "head_sha": head,
                        "head_branch": branch,
                        "base": str(policy.get("target") or record.base_ref),
                        "draft": bool(policy.get("draft")),
                    }
                else:
                    # The PR tracks the branch — a fresh push moved its head.
                    # Record the new pinned head rather than recreating.
                    pr = dict(pr)
                    pr["head_sha"] = head
                    pr["head_branch"] = branch
                    record.pull_request = pr
        except WorkspaceError as exc:
            # SOR-178: a publish failure is a durable fact on the record —
            # an automatic publish must never be silent, and an explicit one
            # leaves the same trail. Clipped like run errors.
            record.publish_error = f"{exc.code}: {clip_message(exc.message)}"
            self.save(record)
            raise
        record.publish_error = None
        return self.save(record)

    def merge(self, handle: SandboxHandle, agent_id: str) -> WorkspaceRecord:
        """Merge the recorded pull request — gated on the independent
        exact-SHA review pin (SOR-178).

        Fail-closed chain, every link required: the policy must declare
        ``merge``; a pull request must be recorded; an independent review
        must have pinned ``reviewed_head_sha`` (missing →
        ``review_required``); the recorded PR head and the remote PR ref
        must still equal that pin (either drift → ``head_sha_mismatch``,
        requiring a fresh review). Only then is the GitHub merge issued —
        with the pinned sha as GitHub's own required-head check, so the
        merge also fails closed server-side.
        """
        record = self._require(agent_id)
        if not record.prepared:
            raise WorkspaceError(
                WORKSPACE_INVALID, f"workspace for agent {agent_id} is not prepared"
            )
        policy = record.git
        if not policy or not policy.get("merge"):
            raise WorkspaceError(
                WORKSPACE_INVALID,
                f"agent {agent_id} declared no merge-enabled git policy",
            )
        pr = record.pull_request
        number = (pr or {}).get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise WorkspaceError(
                WORKSPACE_INVALID,
                f"agent {agent_id} has no recorded pull request to merge",
            )
        if (pr or {}).get("state") == "merged" and record.merge is not None:
            return record  # already merged — idempotent
        reviewed = record.reviewed_head_sha
        if reviewed is None:
            raise WorkspaceError(
                REVIEW_REQUIRED,
                f"agent {agent_id} PR #{number} has no reviewed head — "
                "an independent review must pin the exact head sha first",
            )
        if pr.get("head_sha") != reviewed:
            raise WorkspaceError(
                HEAD_SHA_MISMATCH,
                f"recorded PR head {pr.get('head_sha')} does not match "
                f"reviewed head {reviewed} — re-review required",
            )
        pr_ref = pr.get("ref")
        if not is_safe_ref(pr_ref):
            pr_ref = f"refs/pull/{number}/head"
        remote_sha = git_ls_remote(
            self._backend, handle, record.workdir, pr_ref, github_repo=record.repo
        )
        if remote_sha != reviewed:
            raise WorkspaceError(
                HEAD_SHA_MISMATCH,
                f"remote {pr_ref} resolves to {remote_sha}, not the reviewed "
                f"head {reviewed} — re-review required",
            )
        data = merge_pull_request(self._backend, handle, record.repo, number=number, sha=reviewed)
        if not data.get("merged"):
            detail = clip_message(str(data.get("message") or ""))[:200]
            raise WorkspaceError(
                REPO_UNAVAILABLE,
                f"GitHub refused merge of PR #{number}" + (f": {detail}" if detail else ""),
            )
        merge_commit_sha = data.get("sha")
        record.merge = {
            "merged": True,
            "merge_commit_sha": merge_commit_sha if is_commit_sha(merge_commit_sha) else None,
            "head_sha": reviewed,
            "merged_at": self._now(),
        }
        pr = dict(pr)
        pr["state"] = "merged"
        record.pull_request = pr
        return self.save(record)

    def post_review_comment(
        self, handle: SandboxHandle, agent_id: str, body: str
    ) -> WorkspaceRecord:
        """Post a machine-readable review comment on the recorded PR.

        Deliberately an *issue comment* — never a formal GitHub review
        approval: every sandbox shares one GitHub identity, so an approval
        event would fake a review by the PR's own author. The comment URL is
        stamped on ``pull_request.review_comment_url``.
        """
        record = self._require(agent_id)
        pr = record.pull_request
        number = (pr or {}).get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise WorkspaceError(
                WORKSPACE_INVALID,
                f"agent {agent_id} has no recorded pull request to comment on",
            )
        if not isinstance(body, str) or not body:
            raise WorkspaceError(WORKSPACE_INVALID, "review comment body must be non-empty")
        data = create_issue_comment(self._backend, handle, record.repo, number=number, body=body)
        url = data.get("html_url")
        pr = dict(record.pull_request or {})
        if isinstance(url, str) and url:
            pr["review_comment_url"] = url
        record.pull_request = pr
        return self.save(record)

    def _get_fresh(self, agent_id: str) -> WorkspaceRecord | None:
        """Authoritative read for a mutation: a store that caches ``get``
        (``ModalDictWorkspaceStore``) exposes ``get_fresh``; other stores
        serve the live row anyway."""
        fresh = getattr(self._store, "get_fresh", None)
        if callable(fresh):
            return fresh(agent_id)
        return self._store.get(agent_id)

    def _require(self, agent_id: str) -> WorkspaceRecord:
        record = self._get_fresh(agent_id)
        if record is None:
            raise WorkspaceError(WORKSPACE_NOT_FOUND, f"no workspace for agent {agent_id}")
        return record

    def _clone(self, handle: SandboxHandle, repo: str, workdir: str) -> None:
        # ``repo`` may carry userinfo (https://user:TOKEN@…); the credential
        # portion never belongs in an error that lands on a run record.
        safe_repo = github.redact_url_credentials(repo)
        res = run_git(
            self._backend,
            handle,
            ["clone", "--", repo, workdir],
            github_repo=repo,
        )
        if res.code != 0:
            raise WorkspaceError(
                REPO_UNAVAILABLE, f"git clone {safe_repo!r} failed (exit {res.code})"
            )

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
    "GIT_POLICY_KEYS",
    "GitResult",
    "HEAD_SHA_MISMATCH",
    "InMemoryWorkspaceStore",
    "ModalDictWorkspaceStore",
    "REPO_UNAVAILABLE",
    "REVIEW_REQUIRED",
    "WORKSPACE_ERROR_CODES",
    "WORKSPACE_INVALID",
    "WORKSPACE_NOT_FOUND",
    "WORKSPACES_DICT_NAME",
    "WorkspaceError",
    "WorkspaceRecord",
    "WorkspaceService",
    "WorkspaceSpec",
    "WorkspaceStore",
    "create_issue_comment",
    "create_pull_request",
    "merge_pull_request",
    "git_checkout",
    "git_fetch_ref",
    "git_head",
    "git_is_ancestor",
    "git_ls_remote",
    "git_push",
    "git_rev_parse",
    "is_commit_sha",
    "is_safe_ref",
    "is_safe_relpath",
    "is_sha256",
    "normalize_git_policy",
    "record_from_dict",
    "record_to_dict",
    "require_relpath",
    "run_git",
    "sha256_file",
    "validate_git_policy",
    "write_payload",
]
