"""Protocol surface for control.backend — do not construct ModalBackend."""

from __future__ import annotations

import inspect
from pathlib import Path

from control.backend import (
    LocalProcessBackend,
    ModalBackend,
    SandboxBackend,
    SandboxHandle,
    SandboxSpec,
)


def test_protocol_methods() -> None:
    for name in ("create", "exec", "terminate", "poll", "list"):
        assert callable(getattr(LocalProcessBackend, name))
        assert callable(getattr(ModalBackend, name))
        assert name in SandboxBackend.__dict__ or hasattr(SandboxBackend, name)


def test_modal_methods_document_sdk_and_are_unimplemented() -> None:
    docs = {
        "create": "modal.Sandbox.create",
        "exec": "sb.exec",
        "terminate": "sb.terminate",
        "poll": "Sandbox.from_id",
        "list": "Sandbox.list",
    }
    for method, needle in docs.items():
        func = getattr(ModalBackend, method)
        assert needle in (func.__doc__ or "")
        source = inspect.getsource(func)
        assert "raise NotImplementedError" in source
        # Tests must not call these methods (would be the place `import modal` could run).
    exec_doc = ModalBackend.exec.__doc__ or ""
    assert "bufsize=1" in exec_doc
    assert "write_eof" in exec_doc
    backend_exec_doc = inspect.getdoc(SandboxBackend.exec) or ""
    assert "/dev/null" in backend_exec_doc
    local_src = inspect.getsource(LocalProcessBackend.exec)
    assert "stdin=subprocess.DEVNULL" in local_src
    assert "bufsize=1" in local_src


def test_spec_and_handle_types() -> None:
    spec = SandboxSpec(tags={"role": "test"})
    assert spec.tags["role"] == "test"
    handle = SandboxHandle(id="abc", root=Path("/tmp"), tags={})
    assert handle.id == "abc"
