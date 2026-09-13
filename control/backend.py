"""Sandbox backend abstraction.

Frozen after WP0 (SOR-39). Control-plane code should wrap these synchronous
APIs in a thread pool if it needs async. Tests must not instantiate
``ModalBackend`` or trigger any Modal network connection.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

_SIGTERM_GRACE_S = 5.0


@dataclass(frozen=True)
class SandboxSpec:
    """How to create a sandbox."""

    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxHandle:
    """Opaque handle plus local metadata (root is the sandbox filesystem root)."""

    id: str
    root: Path
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxPoll:
    """Result of ``poll(handle)``."""

    alive: bool
    active_processes: int


@runtime_checkable
class Process(Protocol):
    """A process started by ``exec``. ``stdout`` yields lines without trailing newlines."""

    stdout: Iterator[str]

    def wait(self) -> int:
        """Block until the process exits; return the exit code."""

    def kill(self) -> None:
        """SIGTERM the process group, then SIGKILL after 5 seconds."""


class SandboxBackend(Protocol):
    """Create / exec / terminate sandboxes."""

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        """Provision a sandbox root and return a handle."""

    def exec(
        self,
        handle: SandboxHandle,
        argv: list[str],
        env: Mapping[str, str] | None = None,
    ) -> Process:
        """Start ``argv`` in the sandbox. Returns a streaming ``Process``."""

    def terminate(self, handle: SandboxHandle) -> None:
        """Kill all child processes for this handle and delete the sandbox root."""

    def poll(self, handle: SandboxHandle) -> SandboxPoll:
        """Whether the sandbox still exists and how many execs are live."""

    def list(self, tags: Mapping[str, str] | None = None) -> list[SandboxHandle]:
        """List live sandboxes. ``tags`` is an exact-match subset filter."""


class LocalProcess:
    """``subprocess.Popen`` wrapper: line-wise stdout, SIGTERM→5s→SIGKILL."""

    def __init__(self, popen: subprocess.Popen[str]) -> None:
        self._popen = popen
        stdout = popen.stdout
        if stdout is None:
            raise RuntimeError("LocalProcess requires stdout=PIPE")
        self.stdout = _LineIterator(stdout)

    def wait(self) -> int:
        code = self._popen.wait()
        return int(code)

    def kill(self) -> None:
        if self._popen.poll() is not None:
            return
        pid = self._popen.pid
        _kill_process_group(pid, signal.SIGTERM)
        try:
            self._popen.wait(timeout=_SIGTERM_GRACE_S)
        except subprocess.TimeoutExpired:
            _kill_process_group(pid, signal.SIGKILL)
            self._popen.wait()


class _LineIterator:
    """Iterates a text stream one stripped line at a time."""

    def __init__(self, stream: Iterator[str]) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        line = next(self._stream)
        return line.rstrip("\n")


@dataclass
class _Record:
    handle: SandboxHandle
    procs: list[LocalProcess] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class LocalProcessBackend:
    """Local tempdir sandbox: ``create`` makes a directory used as the sandbox root."""

    def __init__(self) -> None:
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        root = Path(tempfile.mkdtemp(prefix="sbx-"))
        handle = SandboxHandle(id=uuid.uuid4().hex, root=root, tags=dict(spec.tags))
        with self._lock:
            self._records[handle.id] = _Record(handle=handle)
        return handle

    def exec(
        self,
        handle: SandboxHandle,
        argv: list[str],
        env: Mapping[str, str] | None = None,
    ) -> Process:
        rec = self._require(handle)
        merged = dict(os.environ)
        if env:
            merged.update(env)
        merged.setdefault("SBX_WORK", str(handle.root))
        popen = subprocess.Popen(
            argv,
            cwd=handle.root,
            env=merged,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        proc = LocalProcess(popen)
        with rec.lock:
            rec.procs.append(proc)
        return proc

    def terminate(self, handle: SandboxHandle) -> None:
        rec = self._records.get(handle.id)
        if rec is None:
            return
        with rec.lock:
            for proc in rec.procs:
                proc.kill()
        shutil.rmtree(rec.handle.root, ignore_errors=True)
        with self._lock:
            self._records.pop(handle.id, None)

    def poll(self, handle: SandboxHandle) -> SandboxPoll:
        rec = self._records.get(handle.id)
        if rec is None or not rec.handle.root.exists():
            return SandboxPoll(alive=False, active_processes=0)
        with rec.lock:
            active = sum(1 for p in rec.procs if p._popen.poll() is None)
        return SandboxPoll(alive=True, active_processes=active)

    def list(self, tags: Mapping[str, str] | None = None) -> list[SandboxHandle]:
        with self._lock:
            records = list(self._records.values())
        out: list[SandboxHandle] = []
        for rec in records:
            if not rec.handle.root.exists():
                continue
            if tags and any(rec.handle.tags.get(k) != v for k, v in tags.items()):
                continue
            out.append(rec.handle)
        return out

    def _require(self, handle: SandboxHandle) -> _Record:
        rec = self._records.get(handle.id)
        if rec is None:
            raise KeyError(f"unknown sandbox handle {handle.id}")
        return rec


class ModalBackend:
    """Modal Sandbox backend skeleton.

    Methods raise ``NotImplementedError``. Corresponding Modal SDK calls are
    documented in each method; WP0 must not open a Modal connection.
    ``import modal`` is reserved for a future implementation of these methods
    (``modal.Sandbox.create``, ``sb.exec``, ``sb.terminate``,
    ``Sandbox.from_id``, ``Sandbox.list``) and is not executed at import time.
    """

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        """Create a Modal sandbox.

        Modal SDK: ``modal.Sandbox.create(...)``.
        """
        raise NotImplementedError("ModalBackend.create -> modal.Sandbox.create")

    def exec(
        self,
        handle: SandboxHandle,
        argv: list[str],
        env: Mapping[str, str] | None = None,
    ) -> Process:
        """Exec a command in an existing sandbox.

        Modal SDK: ``Sandbox.from_id(handle.id)`` then ``sb.exec(...)``.
        """
        raise NotImplementedError("ModalBackend.exec -> sb.exec / Sandbox.from_id")

    def terminate(self, handle: SandboxHandle) -> None:
        """Terminate a sandbox.

        Modal SDK: ``sb.terminate()``.
        """
        raise NotImplementedError("ModalBackend.terminate -> sb.terminate")

    def poll(self, handle: SandboxHandle) -> SandboxPoll:
        """Poll sandbox liveness.

        Modal SDK: ``Sandbox.from_id(handle.id)`` (and inspect returned object).
        """
        raise NotImplementedError("ModalBackend.poll -> Sandbox.from_id")

    def list(self, tags: Mapping[str, str] | None = None) -> list[SandboxHandle]:
        """List sandboxes, optionally filtered by tags.

        Modal SDK: ``modal.Sandbox.list(...)``.
        """
        raise NotImplementedError("ModalBackend.list -> modal.Sandbox.list")


def _kill_process_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return


# Re-export grace constant for tests that assert the SIGTERM window without waiting it.
SIGTERM_GRACE_S = _SIGTERM_GRACE_S
