"""Validation evidence artifacts (SOR-131).

An *evidence* item is a durable, content-addressed validation artifact
(logs, screenshots, videos, reports) produced by a run and bound to the
workspace head that produced it. Evidence is stored separately from
workspace handoff artifacts so handoff semantics never see evidence
entries as patch/bundle payloads.

Security boundary: evidence content is secret-scanned against the same
forbidden values as workspace artifacts; source paths are denied before
reading; and a size bound prevents unbounded storage. Checksums/sha256
and size are stored in the manifest and verified on every read.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from control.artifacts import WorkspacePolicy, sha256_hex
from control.workspace import WorkspaceRecord

EVIDENCE_SCHEMA_VERSION = 1
CONTENT_MEMBER = "content"
EVIDENCE_MANIFEST = "evidence.json"

# 100 MiB cap keeps an accidentally-huge video/screenshot from monopolizing
# the durable store while still covering realistic validation payloads.
MAX_EVIDENCE_SIZE_BYTES = 100 * 1024 * 1024

_ALLOWED_LOGICAL_TYPES = frozenset({"log", "screenshot", "video", "report"})
_DEFAULT_MEDIA_TYPES: dict[str, str] = {
    "log": "text/plain",
    "screenshot": "image/png",
    "video": "video/mp4",
    "report": "application/json",
}

_MEDIA_TYPE_RE = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9._+]*/[a-zA-Z0-9][-a-zA-Z0-9._+]+$")
_EVIDENCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class EvidenceError(Exception):
    """Base class for evidence package/store failures."""


class EvidenceNotFoundError(EvidenceError):
    """Evidence id does not exist."""


class EvidenceCorruptError(EvidenceError):
    """Stored manifest or content fails decode or checksum verification."""


class EvidenceSecretError(EvidenceError):
    """A forbidden value was found inside evidence content."""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = sorted(paths)
        super().__init__(f"forbidden content found in evidence: {', '.join(self.paths)}")


def _to_bytes(value: bytes | str) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


def _validate_evidence_id(evidence_id: Any) -> str:
    if not isinstance(evidence_id, str) or not _EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise EvidenceError(f"invalid evidence id {evidence_id!r}")
    return evidence_id


def _validate_media_type(media_type: Any) -> str:
    if not isinstance(media_type, str) or not _MEDIA_TYPE_RE.fullmatch(media_type):
        raise EvidenceError(f"invalid evidence media_type {media_type!r}")
    return media_type


def _validate_sha256(value: Any) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise EvidenceCorruptError(f"invalid sha256 {value!r}")
    return value


@dataclass
class Evidence:
    """``evidence.json`` content."""

    evidence_id: str
    logical_type: str
    media_type: str
    size: int
    sha256: str
    producer_agent_id: str
    producer_run_id: str | None = None
    workspace_id: str = ""
    workspace_repo: str = ""
    workspace_base_sha: str = ""
    workspace_head_sha: str = ""
    created_at: str = ""
    schema_version: int = EVIDENCE_SCHEMA_VERSION


def evidence_to_dict(evidence: Evidence) -> dict[str, Any]:
    return {
        "schema_version": evidence.schema_version,
        "evidence_id": evidence.evidence_id,
        "logical_type": evidence.logical_type,
        "media_type": evidence.media_type,
        "size": evidence.size,
        "sha256": evidence.sha256,
        "producer": {
            "agent_id": evidence.producer_agent_id,
            "run_id": evidence.producer_run_id,
        },
        "workspace": {
            "id": evidence.workspace_id,
            "repo": evidence.workspace_repo,
            "base_sha": evidence.workspace_base_sha,
            "head_sha": evidence.workspace_head_sha,
        },
        "created_at": evidence.created_at,
    }


def evidence_from_dict(data: Any) -> Evidence:
    """Strict decode; raises ``EvidenceCorruptError`` on unusable payloads."""
    if not isinstance(data, dict):
        raise EvidenceCorruptError("evidence manifest is not a dict")
    version = data.get("schema_version")
    bad_version = (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != EVIDENCE_SCHEMA_VERSION
    )
    if bad_version:
        raise EvidenceCorruptError(f"unsupported evidence schema_version {version!r}")
    evidence_id = _validate_evidence_id(data.get("evidence_id"))
    logical_type = data.get("logical_type")
    if logical_type not in _ALLOWED_LOGICAL_TYPES:
        raise EvidenceCorruptError(f"unknown evidence logical_type {logical_type!r}")
    media_type = _validate_media_type(data.get("media_type"))
    size = data.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise EvidenceCorruptError("evidence size must be a non-negative int")
    sha = _validate_sha256(data.get("sha256"))
    producer = data.get("producer")
    if not isinstance(producer, dict) or not isinstance(producer.get("agent_id"), str):
        raise EvidenceCorruptError("evidence producer must be a dict with agent_id")
    producer_agent_id = producer["agent_id"]
    producer_run_id = producer.get("run_id")
    if producer_run_id is not None and not isinstance(producer_run_id, str):
        raise EvidenceCorruptError("evidence producer.run_id must be a string or null")
    workspace = data.get("workspace") or {}
    if not isinstance(workspace, dict):
        raise EvidenceCorruptError("evidence workspace must be a dict")
    for key in ("id", "repo", "base_sha", "head_sha"):
        value = workspace.get(key)
        if value is not None and not isinstance(value, str):
            raise EvidenceCorruptError(f"evidence workspace {key} must be a string")
    created_at = data.get("created_at", "")
    if not isinstance(created_at, str):
        raise EvidenceCorruptError("evidence created_at must be a string")
    return Evidence(
        evidence_id=evidence_id,
        logical_type=logical_type,
        media_type=media_type,
        size=size,
        sha256=sha,
        producer_agent_id=producer_agent_id,
        producer_run_id=producer_run_id,
        workspace_id=str(workspace.get("id") or ""),
        workspace_repo=str(workspace.get("repo") or ""),
        workspace_base_sha=str(workspace.get("base_sha") or ""),
        workspace_head_sha=str(workspace.get("head_sha") or ""),
        created_at=created_at,
    )


def evidence_dumps(evidence: Evidence) -> bytes:
    """Canonical manifest bytes (sorted keys, stable across processes)."""
    text = json.dumps(evidence_to_dict(evidence), sort_keys=True, indent=2)
    return (text + "\n").encode("utf-8")


def evidence_loads(data: bytes | str) -> Evidence:
    try:
        raw = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceCorruptError(f"evidence manifest is not valid json: {exc}") from None
    return evidence_from_dict(raw)


def _verify_content(evidence: Evidence, content: bytes) -> None:
    if len(content) != evidence.size:
        raise EvidenceCorruptError(f"evidence {evidence.evidence_id} content size mismatch")
    if sha256_hex(content) != evidence.sha256:
        raise EvidenceCorruptError(f"evidence {evidence.evidence_id} content checksum mismatch")


@runtime_checkable
class EvidenceStore(Protocol):
    """Persistence for evidence packages, keyed by evidence_id."""

    def put(self, evidence: Evidence, content: bytes) -> Evidence:
        """Validate + persist. Returns the evidence."""

    def get(self, evidence_id: str) -> Evidence:
        """Decoded manifest. Raises ``EvidenceNotFoundError``/``EvidenceCorruptError``."""

    def read(self, evidence_id: str) -> bytes:
        """Content bytes, checksum-verified."""

    def list(
        self,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> list[Evidence]:
        """Decodable manifests, optionally filtered by producer."""

    def delete(self, evidence_id: str) -> None:
        """Remove the evidence; missing ids are ignored."""


class InMemoryEvidenceStore:
    """Dict-backed store for tests and ephemeral deployments."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, bytes]] = {}
        self._lock = threading.Lock()

    def put(self, evidence: Evidence, content: bytes) -> Evidence:
        _validate_evidence_id(evidence.evidence_id)
        _verify_content(evidence, content)
        with self._lock:
            self._items[evidence.evidence_id] = (evidence_dumps(evidence), content)
        return evidence

    def _entry(self, evidence_id: str) -> tuple[bytes, bytes]:
        _validate_evidence_id(evidence_id)
        with self._lock:
            entry = self._items.get(evidence_id)
        if entry is None:
            raise EvidenceNotFoundError(f"unknown evidence {evidence_id!r}")
        return entry

    def get(self, evidence_id: str) -> Evidence:
        raw, _ = self._entry(evidence_id)
        decoded = evidence_loads(raw)
        if decoded.evidence_id != evidence_id:
            raise EvidenceCorruptError(
                f"stored evidence id {decoded.evidence_id!r} != {evidence_id!r}"
            )
        return decoded

    def read(self, evidence_id: str) -> bytes:
        evidence = self.get(evidence_id)
        _, content = self._entry(evidence_id)
        _verify_content(evidence, content)
        return content

    def list(
        self,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> list[Evidence]:
        with self._lock:
            keys = list(self._items)
        out: list[Evidence] = []
        for key in keys:
            try:
                evidence = self.get(key)
            except EvidenceError:
                continue
            if agent_id is not None and evidence.producer_agent_id != agent_id:
                continue
            if run_id is not None and evidence.producer_run_id != run_id:
                continue
            out.append(evidence)
        return sorted(out, key=lambda e: (e.created_at, e.evidence_id))

    def delete(self, evidence_id: str) -> None:
        with self._lock:
            self._items.pop(evidence_id, None)


class FileEvidenceStore:
    """Local durable store: ``<root>/<evidence_id>/evidence.json`` plus ``content``.

    Writes are staged in a temp dir and atomically renamed so a crash mid-write
    cannot leave a half-written package.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def _dir(self, evidence_id: str) -> Path:
        return self._root / _validate_evidence_id(evidence_id)

    def put(self, evidence: Evidence, content: bytes) -> Evidence:
        _validate_evidence_id(evidence.evidence_id)
        _verify_content(evidence, content)
        target = self._dir(evidence.evidence_id)
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(dir=self._root, prefix=".staging-"))
            try:
                (staging / CONTENT_MEMBER).write_bytes(content)
                (staging / EVIDENCE_MANIFEST).write_bytes(evidence_dumps(evidence))
                if target.exists():
                    trash = self._root / f".trash-{uuid.uuid4().hex}"
                    os.replace(target, trash)
                    try:
                        os.replace(staging, target)
                    except Exception:
                        os.replace(trash, target)
                        raise
                    else:
                        shutil.rmtree(trash, ignore_errors=True)
                else:
                    os.replace(staging, target)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return evidence

    def get(self, evidence_id: str) -> Evidence:
        path = self._dir(evidence_id) / EVIDENCE_MANIFEST
        try:
            raw = path.read_bytes()
        except OSError:
            raise EvidenceNotFoundError(f"unknown evidence {evidence_id!r}") from None
        decoded = evidence_loads(raw)
        if decoded.evidence_id != evidence_id:
            raise EvidenceCorruptError(
                f"stored evidence id {decoded.evidence_id!r} != {evidence_id!r}"
            )
        return decoded

    def read(self, evidence_id: str) -> bytes:
        evidence = self.get(evidence_id)
        path = self._dir(evidence_id) / CONTENT_MEMBER
        try:
            content = path.read_bytes()
        except OSError:
            raise EvidenceCorruptError(
                f"evidence {evidence_id} content missing from store"
            ) from None
        _verify_content(evidence, content)
        return content

    def list(
        self,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> list[Evidence]:
        try:
            entries = sorted(self._root.iterdir())
        except OSError:
            return []
        out: list[Evidence] = []
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                evidence = self.get(entry.name)
            except EvidenceError:
                continue
            if agent_id is not None and evidence.producer_agent_id != agent_id:
                continue
            if run_id is not None and evidence.producer_run_id != run_id:
                continue
            out.append(evidence)
        return sorted(out, key=lambda e: (e.created_at, e.evidence_id))

    def delete(self, evidence_id: str) -> None:
        _validate_evidence_id(evidence_id)
        with self._lock:
            shutil.rmtree(self._dir(evidence_id), ignore_errors=True)


def _workspace_fields(workspace: WorkspaceRecord | None) -> dict[str, str]:
    if workspace is None:
        return {
            "workspace_id": "",
            "workspace_repo": "",
            "workspace_base_sha": "",
            "workspace_head_sha": "",
        }
    return {
        "workspace_id": workspace.agent_id,
        "workspace_repo": workspace.repo,
        "workspace_base_sha": workspace.base_sha,
        "workspace_head_sha": workspace.head_sha or "",
    }


def create_evidence(
    store: EvidenceStore,
    *,
    logical_type: str,
    producer_agent_id: str,
    content: bytes | str,
    producer_run_id: str | None = None,
    source_path: str | None = None,
    workspace: WorkspaceRecord | None = None,
    media_type: str | None = None,
    forbidden_values: Sequence[bytes | str] = (),
    max_size: int | None = None,
    policy: WorkspacePolicy | None = None,
    evidence_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
    ledger: Any = None,
    run_n: int | None = None,
) -> Evidence:
    """Create and persist a validation evidence item.

    ``content`` is the evidence bytes. ``source_path`` is the optional
    provenance path used for denied-path gating and secret-scan reporting.
    ``workspace`` binds the evidence to the workspace head that produced it.
    If ``ledger`` and ``run_n`` are provided, ``evidence://<id>`` is appended
    to the run's artifact references.
    """
    if logical_type not in _ALLOWED_LOGICAL_TYPES:
        raise EvidenceError(f"invalid logical_type {logical_type!r}")
    resolved_media = media_type if media_type is not None else _DEFAULT_MEDIA_TYPES[logical_type]
    resolved_media = _validate_media_type(resolved_media)

    data = _to_bytes(content)
    bound = max_size if max_size is not None else MAX_EVIDENCE_SIZE_BYTES
    if len(data) > bound:
        raise EvidenceError(f"evidence size {len(data)} exceeds max {bound}")

    if source_path is not None:
        path_policy = policy or WorkspacePolicy()
        if not path_policy.is_allowed(source_path):
            reason = path_policy.denial(source_path) or "not included by policy"
            raise EvidenceError(f"evidence source_path denied: {reason}")

    forbidden = tuple(_to_bytes(v) for v in forbidden_values)
    if any(value and value in data for value in forbidden):
        raise EvidenceSecretError([source_path or "content"])

    eid = evidence_id or f"ev-{uuid.uuid4().hex[:16]}"
    _validate_evidence_id(eid)

    ws = _workspace_fields(workspace)
    now = (clock or (lambda: datetime.now(UTC)))().isoformat()
    evidence = Evidence(
        evidence_id=eid,
        logical_type=logical_type,
        media_type=resolved_media,
        size=len(data),
        sha256=sha256_hex(data),
        producer_agent_id=producer_agent_id,
        producer_run_id=producer_run_id,
        **ws,
        created_at=now,
    )
    persisted = store.put(evidence, data)

    if ledger is not None and run_n is not None:
        attach = getattr(ledger, "attach_artifacts", None)
        if callable(attach):
            attach(producer_agent_id, run_n, [f"evidence://{persisted.evidence_id}"])

    return persisted


def get_evidence(store: EvidenceStore, evidence_id: str) -> Evidence:
    """Return the decoded evidence manifest."""
    return store.get(evidence_id)


def list_evidence(
    store: EvidenceStore,
    *,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> list[Evidence]:
    """List evidence, optionally filtered by producer."""
    return store.list(agent_id=agent_id, run_id=run_id)


def download_evidence(store: EvidenceStore, evidence_id: str) -> bytes:
    """Return the checksum-verified evidence content bytes."""
    return store.read(evidence_id)


__all__ = [
    "CONTENT_MEMBER",
    "EVIDENCE_MANIFEST",
    "EVIDENCE_SCHEMA_VERSION",
    "Evidence",
    "EvidenceCorruptError",
    "EvidenceError",
    "EvidenceNotFoundError",
    "EvidenceSecretError",
    "EvidenceStore",
    "FileEvidenceStore",
    "InMemoryEvidenceStore",
    "MAX_EVIDENCE_SIZE_BYTES",
    "create_evidence",
    "download_evidence",
    "evidence_dumps",
    "evidence_from_dict",
    "evidence_loads",
    "evidence_to_dict",
    "get_evidence",
    "list_evidence",
]
