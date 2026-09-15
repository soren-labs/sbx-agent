"""SOR-83 integration glue: sandbox-side collection + B1/B2 bridging.

B1 (``control.artifacts``) owns the durable package format and stores; B2
(``control.handoff``) owns apply/checkout validation against its own narrow
manifest/store surface. This module is the thin seam between them:

* ``collect_sandbox_workspace`` mirrors ``collect_workspace`` over
  ``SandboxBackend.exec``, so a remote (Modal) sandbox is collected with the
  same ``WorkspacePolicy`` boundary as a local root — denied paths are
  excluded before content is ever read and symlinks are never followed;
* ``snapshot_workspace_artifact`` turns a live agent sandbox into a durable
  B1 package — ``files/`` members, a ``patch.diff`` payload, and a
  ``repo.bundle`` payload when the worktree is clean and HEAD moved — while
  the sandbox still exists, so the artifact outlives teardown;
* ``HandoffStoreView`` adapts a B1 ``ArtifactStore`` to the B2 handoff
  ``ArtifactStore`` protocol so ``prepare_from_artifact`` validates the same
  bytes the ``/v1`` download endpoint serves.

Snapshot base semantics: an artifact's ``base_sha`` is the producer's
``checkout_sha`` — the head its workspace was at when the last
prepare/handoff completed — so chained handoffs validate: applying artifact
B requires a workspace standing exactly on artifact A's head.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from control.artifacts import (
    ArtifactFile,
    ArtifactManifest,
    ArtifactNotFoundError,
    ArtifactSecretError,
    ArtifactStore,
    TestResult,
    WorkspacePolicy,
    sha256_hex,
)
from control.backend import SandboxBackend, SandboxHandle
from control.handoff import ARTIFACT_KIND_BUNDLE, ARTIFACT_KIND_PATCH
from control.sandbox_io import is_local_root, sandbox_env
from control.workspace import (
    CHECKOUT_FAILED,
    WORKSPACE_INVALID,
    WORKSPACE_NOT_FOUND,
    WorkspaceError,
    WorkspaceService,
    git_head,
    run_git,
)

# Conventional payload member carrying an exact-commit git bundle. Chained
# handoffs and sha-pinned reviews ride this member (kind="bundle"); the
# always-present ``patch.diff`` member keeps content-only handoffs working.
BUNDLE_MEMBER = "repo.bundle"

# Sandbox-root-relative staging area (shared with control.handoff): kept
# OUTSIDE the workdir so ``git add -A`` can never sweep a payload in.
_STAGING_DIR = ".sbx-handoff"

# Remote listing: kind TAB relpath per line (f=file, l=symlink, o=other).
_LIST_SCRIPT = (
    "import os,sys\n"
    "root=sys.argv[1]\n"
    "for dp,dn,fn in os.walk(root,followlinks=False):\n"
    "    rel=os.path.relpath(dp,root)\n"
    "    pfx='' if rel=='.' else rel+'/'\n"
    "    for d in dn:\n"
    "        if os.path.islink(os.path.join(dp,d)): print('l\\t'+pfx+d)\n"
    "    for f in fn:\n"
    "        p=os.path.join(dp,f)\n"
    "        k='l' if os.path.islink(p) else ('f' if os.path.isfile(p) else 'o')\n"
    "        print(k+'\\t'+pfx+f)\n"
)

# Remote read: base64 of the file on stdout; exit 3 when not a regular file.
_READ_SCRIPT = (
    "import base64,pathlib,sys\n"
    "p=pathlib.Path(sys.argv[1])\n"
    "sys.exit(3) if not p.is_file() or p.is_symlink() else "
    "sys.stdout.write(base64.b64encode(p.read_bytes()).decode())\n"
)


def _iter_local(root: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else f"{rel_dir}/"
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                entries.append((f"{prefix}{name}", "l"))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                kind = "l"
            elif path.is_file():
                kind = "f"
            else:
                kind = "o"
            entries.append((f"{prefix}{name}", kind))
    return entries


def _iter_workspace(
    backend: SandboxBackend, handle: SandboxHandle, workdir: str
) -> list[tuple[str, str]]:
    """``(relpath, kind)`` for every entry under ``workdir``; kind f/l/o."""
    root = Path(handle.root) / workdir
    if is_local_root(handle):
        if not root.is_dir():
            raise WorkspaceError(
                WORKSPACE_INVALID, f"workspace {workdir} is not a directory in sandbox"
            )
        return _iter_local(root)
    proc = backend.exec(handle, ["python3", "-c", _LIST_SCRIPT, str(root)], env=sandbox_env(handle))
    lines = list(proc.stdout)
    if proc.wait() != 0:
        raise WorkspaceError(
            CHECKOUT_FAILED, f"cannot list workspace {workdir} in sandbox {handle.id}"
        )
    entries: list[tuple[str, str]] = []
    for line in lines:
        kind, _, rel = line.partition("\t")
        if rel and kind in ("f", "l", "o"):
            entries.append((rel, kind))
    return entries


def read_sandbox_bytes(
    backend: SandboxBackend, handle: SandboxHandle, relative: str
) -> bytes | None:
    """Raw bytes of a sandbox-root-relative file; None when absent/unreadable."""
    path = Path(handle.root) / relative
    if is_local_root(handle):
        try:
            return path.read_bytes() if path.is_file() and not path.is_symlink() else None
        except OSError:
            return None
    proc = backend.exec(handle, ["python3", "-c", _READ_SCRIPT, str(path)], env=sandbox_env(handle))
    chunks = list(proc.stdout)
    if proc.wait() != 0:
        return None
    try:
        return base64.b64decode("".join(chunks))
    except Exception:
        return None


def collect_sandbox_workspace(
    backend: SandboxBackend,
    handle: SandboxHandle,
    workdir: str,
    *,
    policy: WorkspacePolicy | None = None,
) -> tuple[list[ArtifactFile], dict[str, bytes], list[str], list[str]]:
    """Collect allowed files under ``workdir`` in a live sandbox.

    Returns ``(files, members, warnings, excluded)`` where ``excluded`` lists
    every workspace file the policy denied or the allowlist skipped — the
    diff pathspec that keeps denied content out of ``patch.diff`` too.
    """
    policy = policy or WorkspacePolicy()
    files: list[ArtifactFile] = []
    members: dict[str, bytes] = {}
    warnings: list[str] = []
    excluded: list[str] = []
    leaks: list[str] = []
    for relpath, kind in _iter_workspace(backend, handle, workdir):
        if kind == "l":
            warnings.append(f"skipped symlink {relpath}")
            continue
        if kind != "f":
            continue
        if policy.denial(relpath) is not None or not policy.is_included(relpath):
            excluded.append(relpath)
            continue
        data = read_sandbox_bytes(backend, handle, f"{workdir}/{relpath}")
        if data is None:
            warnings.append(f"unreadable file {relpath}")
            continue
        if any(value and value in data for value in policy.forbidden_values):
            leaks.append(relpath)
            continue
        files.append(ArtifactFile(relpath, sha256_hex(data), len(data)))
        members[f"files/{relpath}"] = data
    if leaks:
        raise ArtifactSecretError(leaks)
    files.sort(key=lambda f: f.path)
    return files, members, warnings, excluded


def credential_forbidden_values(
    blob: Mapping[str, Any] | None,
    env: Mapping[str, str] | None = None,
) -> tuple[bytes, ...]:
    """Byte strings that must never appear in an artifact for this account.

    The account credential blob's file contents (provider auth stores) plus
    the ambient credential env values that could have leaked into the
    workdir. A file (or payload) containing any of these fails the snapshot
    closed — ``ArtifactSecretError`` — never redact-and-ship.
    """
    env = os.environ if env is None else env
    values: list[bytes] = []
    if isinstance(blob, Mapping):
        files = blob.get("files")
        if isinstance(files, Mapping):
            for content in files.values():
                values.append(str(content).encode("utf-8"))
    for name in ("SBX_ACCOUNT_CREDENTIAL", "CODEX_AUTH_JSON"):
        raw = env.get(name)
        if raw:
            values.append(raw.encode("utf-8"))
    return tuple(values)


def _bundle_payload(
    backend: SandboxBackend,
    handle: SandboxHandle,
    workdir: str,
    base: str,
    head: str,
) -> bytes | None:
    """``git bundle create`` for ``base..head`` staged outside the workdir."""
    staging = f"{_STAGING_DIR}/snapshot.bundle"
    target = str(Path(handle.root) / staging)
    if is_local_root(handle):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    else:
        mkdir = "import pathlib,sys;pathlib.Path(sys.argv[1]).mkdir(parents=True,exist_ok=True)"
        proc = backend.exec(
            handle,
            ["python3", "-c", mkdir, str(Path(handle.root) / _STAGING_DIR)],
            env=sandbox_env(handle),
        )
        for _ in proc.stdout:
            pass
        if proc.wait() != 0:
            return None
    # Positive ref + negative base: a bare ``base..head`` range names no ref
    # and git refuses an empty bundle; ``HEAD ^base`` pins head instead.
    res = run_git(backend, handle, ["bundle", "create", target, "HEAD", f"^{base}"], cwd=workdir)
    if res.code != 0:
        return None
    return read_sandbox_bytes(backend, handle, staging)


def _run_test_command(
    backend: SandboxBackend, handle: SandboxHandle, workdir: str, command: str
) -> int:
    """Run ``command`` inside the workdir; returns its exit code."""
    proc = backend.exec(
        handle,
        ["bash", "-lc", f"cd {workdir} && {command}"],
        env=sandbox_env(handle),
    )
    for _ in proc.stdout:
        pass
    return proc.wait()


def snapshot_workspace_artifact(
    *,
    backend: SandboxBackend,
    handle: SandboxHandle,
    workspaces: WorkspaceService,
    store: ArtifactStore,
    agent_id: str,
    run_id: str | None = None,
    test_command: str | None = None,
    forbidden_values: Sequence[bytes | str] = (),
    artifact_id: str | None = None,
    ledger: Any = None,
    run_n: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ArtifactManifest:
    """Snapshot a live agent's declared workspace into the durable store.

    The package's ``base_sha`` is the workspace ``checkout_sha`` (the head it
    stood on before this agent's own work) so downstream handoff validation
    composes; ``head_sha`` is the current workdir HEAD. ``patch.diff`` is the
    complete working-tree delta vs ``checkout_sha`` with every denied/
    excluded path filtered out via pathspec. When the worktree is clean and
    HEAD moved, a ``repo.bundle`` member additionally pins the exact commit
    for chained handoffs and sha-pinned review.

    ``ledger`` + ``run_n`` attach ``artifact://<id>`` to the durable run
    record so the reference survives sandbox teardown.
    """
    record = workspaces.get(agent_id)
    if record is None:
        raise WorkspaceError(WORKSPACE_NOT_FOUND, f"no workspace for agent {agent_id}")
    if not record.prepared or record.checkout_sha is None:
        raise WorkspaceError(WORKSPACE_INVALID, f"workspace for agent {agent_id} is not prepared")
    workdir = record.workdir
    base = record.checkout_sha
    status = run_git(backend, handle, ["status", "--porcelain"], cwd=workdir)
    if status.code != 0:
        raise WorkspaceError(
            CHECKOUT_FAILED, f"git status failed in workdir {workdir} (exit {status.code})"
        )
    dirty = any(line.strip() for line in status.lines)
    head = git_head(backend, handle, workdir)
    if head is None:
        raise WorkspaceError(CHECKOUT_FAILED, f"no HEAD in workdir {workdir} for agent {agent_id}")
    record.head_sha = head
    workspaces.save(record)

    forbidden = tuple(
        value if isinstance(value, bytes) else str(value).encode("utf-8")
        for value in forbidden_values
    )
    policy = WorkspacePolicy(forbidden_values=forbidden)
    files, members, warnings, excluded = collect_sandbox_workspace(
        backend, handle, workdir, policy=policy
    )

    # Intent-to-add makes untracked files visible to `git diff` without
    # staging content; the excluded pathspec keeps denied/allowlist-skipped
    # files out of the payload exactly as it keeps them out of members.
    res = run_git(backend, handle, ["add", "-N", "-A"], cwd=workdir)
    if res.code != 0:
        raise WorkspaceError(
            CHECKOUT_FAILED, f"git add -N failed in workdir {workdir} (exit {res.code})"
        )
    pathspec = [".", *(f":(exclude){path}" for path in excluded)]
    diff = run_git(backend, handle, ["diff", "--binary", base, "--", *pathspec], cwd=workdir)
    if diff.code != 0:
        raise WorkspaceError(
            CHECKOUT_FAILED, f"git diff failed in workdir {workdir} (exit {diff.code})"
        )
    patch = "\n".join(diff.lines)
    if patch:
        patch += "\n"

    payloads: dict[str, bytes] = {"patch.diff": patch.encode("utf-8")}
    # A bundle can only reproduce a clean committed head — a dirty tree has
    # content no commit holds, so patch is the honest payload then.
    if not dirty and head != base:
        bundle = _bundle_payload(backend, handle, workdir, base, head)
        if bundle is not None:
            payloads[BUNDLE_MEMBER] = bundle

    for name, data in payloads.items():
        if any(value and value in data for value in forbidden):
            raise ArtifactSecretError([name])

    tests: list[TestResult] = []
    if test_command:
        code = _run_test_command(backend, handle, workdir, test_command)
        tests.append(TestResult(test_command, code))

    now = (clock or (lambda: datetime.now(UTC)))().isoformat()
    manifest = ArtifactManifest(
        artifact_id=artifact_id or f"art-{uuid.uuid4().hex[:16]}",
        base_sha=base,
        head_sha=head,
        repo=record.repo,
        created_at=now,
        producer_agent_id=agent_id,
        producer_run_id=run_id,
        files=files,
        tests=tests,
        payloads={name: sha256_hex(data) for name, data in payloads.items()},
        warnings=warnings,
    )
    members.update(payloads)
    # store.put re-verifies every member against the manifest — a snapshot is
    # only durable once integrity checks pass.
    persisted = store.put(manifest, members)
    if ledger is not None and run_n is not None:
        attach = getattr(ledger, "attach_artifacts", None)
        if callable(attach):
            attach(agent_id, run_n, [f"artifact://{persisted.artifact_id}"])
    return persisted


def _payload_member(manifest: ArtifactManifest) -> str | None:
    """The member a handoff consumes: the exact-commit bundle when present."""
    if BUNDLE_MEMBER in manifest.payloads:
        return BUNDLE_MEMBER
    if "patch.diff" in manifest.payloads:
        return "patch.diff"
    return None


class HandoffStoreView:
    """Adapt a B1 ``ArtifactStore`` to the B2 handoff ``ArtifactStore`` protocol.

    ``get_manifest`` projects the package manifest into the flat handoff
    shape (``kind``/``payload_sha256``/path→sha256 ``files``); a corrupt or
    payload-less package decodes as malformed and fails ``artifact_invalid``
    on the consume side — never silently applied.
    """

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    def get_manifest(self, artifact_id: str) -> dict[str, Any] | None:
        try:
            manifest = self._store.manifest(artifact_id)
        except ArtifactNotFoundError:
            return None
        except Exception:
            # Corrupt manifest: hand the consumer a dict that fails strict
            # decode so it fails closed as artifact_invalid.
            return {"artifact_id": artifact_id}
        member = _payload_member(manifest)
        test = manifest.tests[0] if manifest.tests else None
        return {
            "artifact_id": manifest.artifact_id,
            "kind": ARTIFACT_KIND_BUNDLE if member == BUNDLE_MEMBER else ARTIFACT_KIND_PATCH,
            "repo": manifest.repo,
            "base_sha": manifest.base_sha,
            "head_sha": manifest.head_sha,
            "payload_sha256": manifest.payloads.get(member, "") if member else "",
            "files": {f.path: f.sha256 for f in manifest.files},
            "test_command": test.command if test is not None else None,
            "test_exit_code": test.exit_code if test is not None else None,
            "producer_agent_id": manifest.producer_agent_id or None,
            "producer_run_id": manifest.producer_run_id,
            "created_at": manifest.created_at,
        }

    def read_payload(self, artifact_id: str) -> bytes | None:
        try:
            manifest = self._store.manifest(artifact_id)
            member = _payload_member(manifest)
            if member is None:
                return None
            return self._store.read(artifact_id, member)
        except Exception:
            return None
