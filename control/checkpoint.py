"""SOR-180: same-Agent filesystem + native-session recovery checkpoint.

Distinct from the SOR-127 prepared-environment cache. That cache stores a
reusable, credential-free build keyed by workspace inputs; an agent
checkpoint captures ONE live agent's full sandbox filesystem —
``session.json`` (native provider session id), ``turns/`` results,
``events.jsonl``, the workspace checkout — at suspend time, so a later
follow-up restores the same Agent id + filesystem + native provider
session.

Lifecycle: ``idle`` live → checkpoint → release → ``suspended``
(recoverable, non-terminal) → the next follow-up restores a fresh
sandbox from the checkpoint and re-attaches credentials/resources.

Credentials never become durable product state: the filesystem is
scrubbed of credential material *before* snapshotting, and the restored
sandbox re-attaches them in-sandbox from the Secret mounts / env bridge
(``SBX_ACCOUNT_CREDENTIAL`` / ``CODEX_AUTH_JSON``) — the same channels
``runner init`` consumes. A platform loss with no checkpoint is
diagnosed explicitly: the reaper emits ``platform_loss`` and this
service persists a ``failed`` record carrying ``last_error``.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from control.backend import SandboxBackend, SandboxHandle, SandboxSpec
from control.compute import compute_for_record
from control.config import CHECKPOINTS_DICT_NAME
from control.sandbox_io import drain, is_local_root, read_json, sandbox_env
from control.store import SessionRecord
from control.workspace import git_head, is_commit_sha

CHECKPOINT_CHECKPOINTED = "checkpointed"
CHECKPOINT_RESTORED = "restored"
CHECKPOINT_FAILED = "failed"
CHECKPOINT_STATUSES = frozenset({CHECKPOINT_CHECKPOINTED, CHECKPOINT_RESTORED, CHECKPOINT_FAILED})

# Internal-only session status for a checkpointed + released agent. It is
# never terminal (the agent can still be recovered) and never crosses the
# wire — ``ControlPlane.public`` maps it to ``idle`` because the frozen
# ``AgentStatus`` enum cannot grow.
SESSION_SUSPENDED = "suspended"

_AGENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SNAPSHOT_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}")

# Credential-shaped paths removed from the sandbox root before the
# filesystem snapshot. Surgical by design — unlike the env-cache build
# scrub, the agent's native provider session state (``.codex/sessions``,
# ``~/.grok`` history, ``turns/``, ``events.jsonl``, the workspace
# checkout) MUST survive for a same-agent restore; only credential files
# are removed. ``.ssh`` is whole-dir (arbitrary key filenames).
_SCRUB_CREDENTIAL_RELPATHS = (
    # v1 codex auth input + both CODEX_HOME layouts (``$SBX_WORK/.codex``
    # in production, ``$HOME/.codex`` for local).
    "auth.json",
    ".codex/auth.json",
    "home/.codex/auth.json",
    ".git-credentials",
    "home/.git-credentials",
    ".netrc",
    "home/.netrc",
    ".gitconfig",
    "home/.gitconfig",
    ".config/gh/hosts.yml",
    "home/.config/gh/hosts.yml",
    ".ssh",
    "home/.ssh",
    # Provider credential blob restore targets (``SBX_ACCOUNT_CREDENTIAL``
    # ``files`` relpaths, relative to ``$HOME``) — file-level only so the
    # surrounding native session state is preserved.
    "home/.claude/.credentials.json",
    "home/.gemini/antigravity-cli/antigravity-oauth-token",
    "home/.grok/auth.json",
    "home/.local/share/devin/credentials.toml",
    "home/.local/share/opencode/auth.json",
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

# In-sandbox credential reattach for a restored checkpoint. Mirrors
# ``runtime.runner.credentials.restore_credential_blob`` +
# ``bootstrap._write_auth_json`` (same blob shape, same relpath safety,
# same mode-600 writes) as a self-contained ``python3 -c`` so the restore
# never depends on the runner version baked into the snapshotted image.
# Reads ``session.json`` (survives in the snapshot) for the provider, then
# consumes ``SBX_ACCOUNT_CREDENTIAL`` / ``CODEX_AUTH_JSON`` — both arrive
# through Secret mounts or the scoped ``sandbox_env`` bridge, exactly as
# ``runner init`` sees them.
_REATTACH_SCRIPT = (
    "import base64, binascii, json, os, pathlib\n"
    "root = pathlib.Path(os.environ['SBX_WORK'])\n"
    "provider = 'codex'\n"
    "session_path = root / 'session.json'\n"
    "if session_path.is_file():\n"
    "    try:\n"
    "        provider = json.loads(session_path.read_text()).get('provider') or 'codex'\n"
    "    except Exception:\n"
    "        pass\n"
    "codex_home = pathlib.Path(os.environ.get('CODEX_HOME') or (root / '.codex'))\n"
    "# ``$HOME`` inside the sandbox is always ``$SBX_WORK/home`` (filesystem.md).\n"
    "home = root / 'home'\n"
    "def check(rel):\n"
    "    if not isinstance(rel, str) or not rel.strip() or '\\\\' in rel or '\\x00' in rel:\n"
    "        raise SystemExit(3)\n"
    "    p = pathlib.PurePosixPath(rel)\n"
    "    if p.is_absolute() or not p.parts or any(x in ('..', '.') for x in p.parts):\n"
    "        raise SystemExit(3)\n"
    "def target(rel):\n"
    "    parts = pathlib.PurePosixPath(rel).parts\n"
    "    if provider == 'codex' and parts and parts[0] == '.codex':\n"
    "        base = codex_home.resolve()\n"
    "        dest = base.joinpath(*parts[1:]).resolve()\n"
    "    else:\n"
    "        base = home.resolve()\n"
    "        dest = base.joinpath(*parts).resolve()\n"
    "    if not str(dest).startswith(str(base) + os.sep):\n"
    "        raise SystemExit(4)\n"
    "    return dest\n"
    "def decode(value):\n"
    "    if isinstance(value, str):\n"
    "        return value.encode('utf-8')\n"
    "    if isinstance(value, dict):\n"
    "        if 'content_b64' in value:\n"
    "            try:\n"
    "                return base64.b64decode(str(value['content_b64']), validate=True)\n"
    "            except (binascii.Error, ValueError):\n"
    "                raise SystemExit(3)\n"
    "        if 'content' in value:\n"
    "            return str(value['content']).encode('utf-8')\n"
    "    raise SystemExit(3)\n"
    "def write_secret(dest, content):\n"
    "    dest.parent.mkdir(parents=True, exist_ok=True)\n"
    "    try:\n"
    "        dest.parent.chmod(0o700)\n"
    "    except OSError:\n"
    "        pass\n"
    "    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
    "    with os.fdopen(fd, 'wb') as fh:\n"
    "        fh.write(content)\n"
    "    dest.chmod(0o600)\n"
    "raw = os.environ.get('SBX_ACCOUNT_CREDENTIAL') or ''\n"
    "if raw:\n"
    "    blob = json.loads(raw)\n"
    "    if blob.get('provider') != provider:\n"
    "        raise SystemExit(5)\n"
    "    pending = []\n"
    "    for rel, value in sorted((blob.get('files') or {}).items()):\n"
    "        check(rel)\n"
    "        pending.append((target(rel), decode(value)))\n"
    "    for dest, content in pending:\n"
    "        write_secret(dest, content)\n"
    "if provider == 'codex':\n"
    "    auth = os.environ.get('CODEX_AUTH_JSON') or ''\n"
    "    work_auth = root / 'auth.json'\n"
    "    if auth:\n"
    "        text = auth if auth.endswith('\\n') else auth + '\\n'\n"
    "        write_secret(codex_home / 'auth.json', text.encode('utf-8'))\n"
    "    elif work_auth.is_file():\n"
    "        write_secret(codex_home / 'auth.json', work_auth.read_bytes())\n"
)


class CheckpointUnavailable(Exception):
    """A checkpoint record exists but cannot produce a restored sandbox.

    ``retryable`` marks a transient failure of one restore attempt — the
    record stays ``checkpointed`` and the session ``suspended`` so the
    next recovery re-attempts (SOR-180: checkpoint/restore must be
    idempotent and retryable). Terminal ``lost`` is reserved for a
    missing or proven-invalid checkpoint.
    """

    def __init__(self, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class AgentCheckpoint:
    """Durable per-agent checkpoint record, keyed by Agent (session) id.

    ``snapshot_ref`` is the servable artifact — an opaque provider ref
    (Modal image id, local snapshot id) written only by a successful
    suspend. ``secrets``/``resource_secrets`` are the Secret *names* the
    sandbox was provisioned with (refs, never values) so a restore can
    re-declare them on the fresh sandbox spec. A ``failed`` status with
    ``last_error`` is the explicit diagnosis for uncheckpointed platform
    loss and failed suspend/restore attempts.
    """

    agent_id: str
    status: str = CHECKPOINT_CHECKPOINTED
    snapshot_ref: str | None = None
    provider: str = "codex"
    account_id: str = "auto"
    sandbox_id: str | None = None
    native_session_id: str | None = None
    # Post-restore identity evidence: the workspace workdir (relative to
    # the sandbox root) and its ``git rev-parse HEAD`` at checkpoint
    # time. A restore that cannot reproduce them fails closed.
    workspace_workdir: str | None = None
    workspace_head_sha: str | None = None
    turns: int = 0
    secrets: list[str] = field(default_factory=list)
    resource_secrets: list[str] = field(default_factory=list)
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    checkpointed_at: str | None = None
    restored_at: str | None = None


def checkpoint_to_dict(record: AgentCheckpoint) -> dict[str, Any]:
    return asdict(record)


def checkpoint_from_dict(data: Any) -> AgentCheckpoint:
    """Strict-ish decode; raises ``ValueError`` on unusable payloads."""
    if not isinstance(data, dict):
        raise ValueError("checkpoint record is not a dict")
    agent_id = data.get("agent_id")
    if not isinstance(agent_id, str) or not _AGENT_ID_RE.fullmatch(agent_id):
        raise ValueError("checkpoint record missing agent_id")
    record = AgentCheckpoint(agent_id=agent_id)
    status = data.get("status", CHECKPOINT_CHECKPOINTED)
    if status not in CHECKPOINT_STATUSES:
        raise ValueError(f"checkpoint record field status must be one of {CHECKPOINT_STATUSES}")
    record.status = status
    snapshot_ref = data.get("snapshot_ref")
    if snapshot_ref is not None and (
        not isinstance(snapshot_ref, str) or not _SNAPSHOT_REF_RE.fullmatch(snapshot_ref)
    ):
        raise ValueError("checkpoint record field snapshot_ref must be a safe ref string")
    record.snapshot_ref = snapshot_ref
    for key in ("provider", "account_id"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"checkpoint record field {key} must be a string")
        setattr(record, key, value or ("codex" if key == "provider" else "auto"))
    workspace_workdir = data.get("workspace_workdir")
    if workspace_workdir is not None and not _is_safe_relpath(workspace_workdir):
        raise ValueError("checkpoint record field workspace_workdir must be a safe relpath")
    record.workspace_workdir = (
        str(PurePosixPath(workspace_workdir)) if workspace_workdir is not None else None
    )
    workspace_head_sha = data.get("workspace_head_sha")
    if workspace_head_sha is not None and not is_commit_sha(workspace_head_sha):
        raise ValueError("checkpoint record field workspace_head_sha must be a commit sha")
    record.workspace_head_sha = workspace_head_sha
    for key in ("sandbox_id", "native_session_id", "last_error", "checkpointed_at", "restored_at"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"checkpoint record field {key} must be a string")
        setattr(record, key, value)
    turns = data.get("turns", 0)
    if isinstance(turns, bool) or not isinstance(turns, int) or turns < 0:
        raise ValueError("checkpoint record field turns must be a non-negative int")
    record.turns = turns
    for key in ("secrets", "resource_secrets"):
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError(f"checkpoint record field {key} must be a list of strings")
        setattr(record, key, list(value))
    for key in ("created_at", "updated_at"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"checkpoint record field {key} must be a string")
        setattr(record, key, value or "")
    return record


@runtime_checkable
class CheckpointStore(Protocol):
    """Persistence for ``AgentCheckpoint``s, keyed by agent id."""

    def get(self, agent_id: str) -> AgentCheckpoint | None:
        """Return the record, or None when absent."""

    def put(self, record: AgentCheckpoint) -> None:
        """Insert or replace a record."""

    def delete(self, agent_id: str) -> None:
        """Remove a record (agent closed / explicitly unrecoverable)."""

    def list(self) -> list[AgentCheckpoint]:
        """All records (inspection / operator diagnosis)."""


class InMemoryCheckpointStore:
    """Thread-safe dict store for tests and ephemeral deployments."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str) -> AgentCheckpoint | None:
        with self._lock:
            raw = self._items.get(agent_id)
            if raw is None:
                return None
            raw = dict(raw)
        return checkpoint_from_dict(raw)

    def put(self, record: AgentCheckpoint) -> None:
        with self._lock:
            self._items[record.agent_id] = checkpoint_to_dict(record)

    def delete(self, agent_id: str) -> None:
        with self._lock:
            self._items.pop(agent_id, None)

    def list(self) -> list[AgentCheckpoint]:
        with self._lock:
            raws = list(self._items.values())
        return [checkpoint_from_dict(raw) for raw in raws]


class FileCheckpointStore:
    """Local durable store: ``root/<agent_id>.json`` (atomic writes)."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _path(self, agent_id: str) -> Path:
        if not _AGENT_ID_RE.fullmatch(agent_id):
            raise ValueError(f"checkpoint agent id must be filename-safe: {agent_id!r}")
        return self._root / f"{agent_id}.json"

    def get(self, agent_id: str) -> AgentCheckpoint | None:
        try:
            raw = json.loads(self._path(agent_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"stored checkpoint record {agent_id} is corrupt: {exc}") from exc
        return checkpoint_from_dict(raw)

    def put(self, record: AgentCheckpoint) -> None:
        path = self._path(record.agent_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(checkpoint_to_dict(record), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)

    def delete(self, agent_id: str) -> None:
        with self._lock:
            self._path(agent_id).unlink(missing_ok=True)

    def list(self) -> list[AgentCheckpoint]:
        try:
            paths = sorted(self._root.glob("*.json"))
        except OSError:
            return []
        out: list[AgentCheckpoint] = []
        for path in paths:
            record = self.get(path.stem)
            if record is not None:
                out.append(record)
        return out


class ModalDictCheckpointStore:
    """Production store backed by ``modal.Dict``. Lazy-imports modal."""

    def __init__(self, name: str = CHECKPOINTS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def get(self, agent_id: str) -> AgentCheckpoint | None:
        raw = self._d().get(agent_id)
        if raw is None:
            return None
        return checkpoint_from_dict(raw)

    def put(self, record: AgentCheckpoint) -> None:
        self._d().put(record.agent_id, checkpoint_to_dict(record))

    def delete(self, agent_id: str) -> None:
        try:
            self._d().pop(agent_id)
        except KeyError:
            return

    def list(self) -> list[AgentCheckpoint]:
        out: list[AgentCheckpoint] = []
        items: Iterator[tuple[Any, Any]] = self._d().items()
        for _key, raw in items:
            if isinstance(raw, dict):
                out.append(checkpoint_from_dict(raw))
        return out


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text[:limit]


class CheckpointService:
    """Suspend / restore agents through filesystem checkpoints.

    ``suspend`` runs on a live sandbox: read the runner session state →
    scrub credential material → filesystem snapshot → persist a
    ``checkpointed`` record. The caller (the reaper) then releases the
    sandbox and moves the session record to ``suspended``. ``restore``
    re-creates the sandbox from the snapshot with the provision-time
    Secret refs re-declared, then reattaches credential files in-sandbox
    so nothing credential-shaped ever lands in durable product state.
    """

    def __init__(
        self,
        backend: SandboxBackend,
        store: CheckpointStore,
        *,
        snapshots: Any = None,
        workspaces: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend
        self._store = store
        self._snapshots = snapshots
        self._workspaces = workspaces
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return self._clock()

    @property
    def store(self) -> CheckpointStore:
        return self._store

    @property
    def snapshots(self) -> Any:
        return self._snapshots

    def get(self, agent_id: str) -> AgentCheckpoint | None:
        return self._store.get(agent_id)

    def has_checkpoint(self, agent_id: str) -> bool:
        """Whether ``agent_id`` holds a restorable checkpoint right now."""
        try:
            record = self._store.get(agent_id)
        except Exception:
            return False
        return (
            record is not None
            and record.status == CHECKPOINT_CHECKPOINTED
            and record.snapshot_ref is not None
        )

    def fail(self, agent_id: str, error: str, *, force: bool = False) -> None:
        """Record an explicit failure diagnosis for ``agent_id``.

        Upserts a ``failed`` record — used both for suspend/restore
        failures and for uncheckpointed platform loss (no snapshot ever
        landed), which is the same durable surface operators inspect.
        A usable checkpoint normally outranks a later failure (a failed
        re-suspend must not wipe a restorable artifact); ``force`` bypasses
        that guard for the restore path's fail-closed case, where the
        restored artifact proved it is not this agent's and the session
        is diagnosed ``lost`` regardless.
        """
        now = self._now().isoformat()
        try:
            record = self._store.get(agent_id)
        except Exception:
            record = None
        if record is None:
            record = AgentCheckpoint(agent_id=agent_id, created_at=now)
        # A usable checkpoint outranks a later failure — never downgrade it.
        if not force and record.status == CHECKPOINT_CHECKPOINTED and record.snapshot_ref:
            return
        record.status = CHECKPOINT_FAILED
        record.last_error = _clip(error)
        record.updated_at = now
        self._store.put(record)

    def note_error(self, agent_id: str, error: str) -> None:
        """Record a transient restore error without downgrading the record.

        Unlike :meth:`fail`, the status is preserved — a ``checkpointed``
        record stays restorable while ``last_error`` still carries the
        latest attempt's diagnosis for operators.
        """
        now = self._now().isoformat()
        try:
            record = self._store.get(agent_id)
        except Exception:
            return
        if record is None:
            return
        record.last_error = _clip(error)
        record.updated_at = now
        try:
            self._store.put(record)
        except Exception:
            pass

    def discard(self, agent_id: str) -> None:
        """Drop the checkpoint record — the agent is gone for good."""
        try:
            self._store.delete(agent_id)
        except Exception:
            pass

    def suspend(self, rec: SessionRecord, handle: SandboxHandle) -> bool:
        """Checkpoint ``rec``'s live filesystem for a later same-agent restore.

        Returns ``True`` only when a durable ``checkpointed`` record with a
        snapshot ref exists; the caller then releases the sandbox and moves
        the record to ``suspended``. On ``False`` the failure is recorded
        on the checkpoint record and the caller falls back to the
        non-recoverable outcome — credentials are never snapshotted, so a
        scrub failure aborts the snapshot rather than shipping them.
        """
        if self._snapshots is None:
            self.fail(rec.id, "no snapshot provider configured")
            return False
        try:
            state = self._session_state(handle)
            self._scrub(rec, handle, state)
            # Pin the workspace identity the snapshot must reproduce:
            # restore verifies ``session.json.native_session_id`` and the
            # workdir's ``git rev-parse HEAD`` before credentials attach.
            workspace_workdir = self._workdir(rec.id)
            workspace_head_sha: str | None = None
            if workspace_workdir is not None:
                try:
                    workspace_head_sha = git_head(self._backend, handle, workspace_workdir)
                except Exception:
                    workspace_head_sha = None
            snapshot_ref = self._snapshots.snapshot(handle)
        except Exception as exc:
            self.fail(rec.id, f"checkpoint failed: {_clip(exc)}")
            return False
        now = self._now().isoformat()
        try:
            existing = self._store.get(rec.id)
        except Exception:
            existing = None
        spec_secrets = rec.spec_secrets or {}
        record = AgentCheckpoint(
            agent_id=rec.id,
            status=CHECKPOINT_CHECKPOINTED,
            snapshot_ref=snapshot_ref,
            # ``session.json`` is the authoritative provider record — sandbox
            # tags may not carry one, and the restore check compares the
            # same field it recorded.
            provider=state.get("provider") or (rec.sandbox_tags or {}).get("provider") or "codex",
            account_id=(rec.sandbox_tags or {}).get("account_id") or "auto",
            sandbox_id=rec.sandbox_id,
            native_session_id=state.get("native_session_id"),
            workspace_workdir=workspace_workdir,
            workspace_head_sha=workspace_head_sha,
            turns=rec.turns,
            secrets=list(spec_secrets.get("secrets") or ()),
            resource_secrets=list(spec_secrets.get("resource_secrets") or ()),
            created_at=(existing.created_at if existing is not None else now) or now,
            updated_at=now,
            checkpointed_at=now,
        )
        try:
            self._store.put(record)
        except Exception as exc:
            # The snapshot exists but cannot be addressed — treat it as
            # uncheckpointed rather than risk a restore pointing at a
            # record that may not persist.
            self.fail(rec.id, f"checkpoint record write failed: {_clip(exc)}")
            return False
        return True

    def restore(self, rec: SessionRecord) -> SandboxHandle:
        """Create a fresh sandbox pre-populated with the agent's checkpoint.

        The provision-time Secret refs are re-declared on the spec so the
        restored sandbox mounts the same credential/resource channels; the
        in-sandbox reattach then rewrites the credential files (scrubbed
        pre-snapshot — they never persist as product state).

        Restore is idempotent and retryable: a transient failure — the
        snapshot provider erroring, or the in-sandbox verify/reattach
        exec failing — raises ``CheckpointUnavailable(retryable=True)``
        with the record left ``checkpointed``, so the caller keeps the
        session ``suspended`` and the next recovery re-attempts. The
        non-retryable raise (terminal ``lost``) is reserved for a
        missing checkpoint or for post-restore identity evidence that
        fails closed — a snapshot that cannot prove it is this agent's
        filesystem.
        """
        record = self.get(rec.id)
        if record is None or record.status != CHECKPOINT_CHECKPOINTED or not record.snapshot_ref:
            raise CheckpointUnavailable(f"no usable checkpoint for agent {rec.id}")
        if self._snapshots is None:
            raise CheckpointUnavailable("no snapshot provider configured")
        tags = dict(rec.sandbox_tags or {})
        tags["session_id"] = rec.id
        # SOR-181: re-declare the session's durable compute sizing — the
        # backend contract applies it on snapshot restore the same as on
        # create, so a recovered agent keeps its declared floor.
        compute = compute_for_record(rec.compute, rec.sandbox_tags)
        spec = SandboxSpec(
            tags=tags,
            secrets=list(record.secrets),
            resource_secrets=list(record.resource_secrets),
            cpu=compute.cpu if compute is not None else None,
            memory_mib=compute.memory_mib if compute is not None else None,
        )
        try:
            handle = self._snapshots.restore(record.snapshot_ref, spec)
        except Exception as exc:
            # Transient: the snapshot may still be perfectly usable — the
            # record stays ``checkpointed`` and the next recovery retries.
            self.note_error(rec.id, f"checkpoint restore failed: {_clip(exc)}")
            raise CheckpointUnavailable(
                f"checkpoint restore failed: {exc}", retryable=True
            ) from exc
        try:
            self._verify_identity(record, handle)
            self._reattach(handle)
        except CheckpointUnavailable as exc:
            self._terminate(handle)
            if exc.retryable:
                self.note_error(rec.id, f"checkpoint restore failed: {_clip(exc)}")
            else:
                # Identity evidence failed closed: this snapshot is not
                # provably the agent's — the checkpoint is invalid, not
                # merely unlucky.
                self.fail(rec.id, f"checkpoint restore failed: {_clip(exc)}", force=True)
            raise
        except Exception as exc:
            self._terminate(handle)
            self.note_error(rec.id, f"checkpoint restore failed: {_clip(exc)}")
            raise CheckpointUnavailable(
                f"checkpoint restore failed: {exc}", retryable=True
            ) from exc
        now = self._now().isoformat()
        record.status = CHECKPOINT_RESTORED
        record.updated_at = now
        record.restored_at = now
        try:
            self._store.put(record)
        except Exception:
            # Bookkeeping must not wedge a restored sandbox — the handle
            # is already live and bound by the caller.
            pass
        return handle

    def _verify_identity(self, record: AgentCheckpoint, handle: SandboxHandle) -> None:
        """Fail-closed post-restore identity check (SOR-180 #9).

        Before credentials reattach, the snapshot must prove it is THIS
        agent's filesystem: the restored ``session.json`` must carry the
        recorded ``native_session_id`` (and provider), and a recorded
        workspace HEAD must match ``git rev-parse HEAD`` in the restored
        workdir. A mismatch raises non-retryable
        ``CheckpointUnavailable``; unreadable evidence that the record
        proves existed (a missing ``session.json``/repo) mismatches the
        same way, while an exec that itself errors propagates as a
        transient failure the caller may retry.
        """
        state = read_json(self._backend, handle, "session.json")
        restored_native = state.get("native_session_id") if isinstance(state, dict) else None
        if restored_native != record.native_session_id:
            raise CheckpointUnavailable(
                "restored session.json native_session_id mismatch: "
                f"expected {record.native_session_id!r}, got {restored_native!r}"
            )
        restored_provider = state.get("provider") if isinstance(state, dict) else None
        if restored_provider and record.provider and restored_provider != record.provider:
            raise CheckpointUnavailable(
                "restored session.json provider mismatch: "
                f"expected {record.provider!r}, got {restored_provider!r}"
            )
        if record.workspace_head_sha is not None:
            workdir = record.workspace_workdir
            head = git_head(self._backend, handle, workdir) if workdir else None
            if head != record.workspace_head_sha:
                raise CheckpointUnavailable(
                    "restored workspace HEAD mismatch: "
                    f"expected {record.workspace_head_sha!r}, got {head!r}"
                )

    def _terminate(self, handle: SandboxHandle) -> None:
        try:
            self._backend.terminate(handle)
        except Exception:
            pass

    def _session_state(self, handle: SandboxHandle) -> dict[str, Any]:
        state = read_json(self._backend, handle, "session.json")
        return state if isinstance(state, dict) else {}

    def _scrub(self, rec: SessionRecord, handle: SandboxHandle, state: dict[str, Any]) -> None:
        """Delete credential material in place before snapshotting."""
        relpaths = self._scrub_relpaths(rec, state)
        if is_local_root(handle):
            root = Path(handle.root)
            for rel in relpaths:
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
            ["python3", "-c", _SCRUB_SCRIPT, str(handle.root), *relpaths],
            env=sandbox_env(handle),
        )
        if drain(proc) != 0:
            raise CheckpointUnavailable("checkpoint: credential scrub failed")

    def _scrub_relpaths(self, rec: SessionRecord, state: dict[str, Any]) -> list[str]:
        relpaths = list(_SCRUB_CREDENTIAL_RELPATHS)
        # ``session.json.credential_files`` is the authoritative list of
        # blob-restored files — cover both the ``$HOME`` layout and a
        # rooted CODEX_HOME (``$SBX_WORK/.codex``).
        for rel in state.get("credential_files") or ():
            rel = str(rel)
            if not _is_safe_relpath(rel):
                continue
            relpaths.append(rel)
            relpaths.append(f"home/{rel}")
        workdir = self._workdir(rec.id)
        if workdir is not None:
            for rel in _SCRUB_WORKDIR_RELPATHS:
                relpaths.append(f"{workdir}/{rel}")
        return relpaths

    def _workdir(self, agent_id: str) -> str | None:
        """Prepared workspace workdir, when a workspace service is wired."""
        if self._workspaces is None:
            return None
        get = getattr(self._workspaces, "get", None)
        if not callable(get):
            return None
        try:
            record = get(agent_id)
        except Exception:
            return None
        workdir = getattr(record, "workdir", None) if record is not None else None
        if not getattr(record, "prepared", False):
            return None
        if isinstance(workdir, str) and _is_safe_relpath(workdir):
            return workdir
        return None

    def _reattach(self, handle: SandboxHandle) -> None:
        """Re-write credential files inside the restored sandbox.

        Same channels ``runner init`` consumes — ``SBX_ACCOUNT_CREDENTIAL``
        (provider blob → ``$HOME``/``CODEX_HOME`` files, mode 600) and
        ``CODEX_AUTH_JSON`` (codex compat input). Secrets re-mounted at
        create reach execs through the backend's per-sandbox resolution;
        local backends forward the scoped ambient vars via
        ``sandbox_env``.
        """
        proc = self._backend.exec(
            handle, ["python3", "-c", _REATTACH_SCRIPT], env=sandbox_env(handle)
        )
        if drain(proc) != 0:
            raise CheckpointUnavailable("credential reattach failed", retryable=True)


def _is_safe_relpath(rel: str) -> bool:
    path = PurePosixPath(rel)
    return (
        bool(path.parts)
        and not path.is_absolute()
        and not any(part in ("..", ".") for part in path.parts)
        and "\\" not in rel
        and "\x00" not in rel
    )


__all__ = [
    "CHECKPOINT_CHECKPOINTED",
    "CHECKPOINT_FAILED",
    "CHECKPOINT_RESTORED",
    "CHECKPOINT_STATUSES",
    "SESSION_SUSPENDED",
    "AgentCheckpoint",
    "CheckpointService",
    "CheckpointStore",
    "CheckpointUnavailable",
    "FileCheckpointStore",
    "InMemoryCheckpointStore",
    "ModalDictCheckpointStore",
    "checkpoint_from_dict",
    "checkpoint_to_dict",
]
