"""``ModalPlane`` local logic: app state filtering + honest ensure_dict.

The Modal SDK is stubbed out — these tests pin the check-then-act and
``modal app list`` parsing semantics without cloud credentials.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sbx.errors import BootstrapError
from sbx.plane import ModalPlane


class _NotFound(Exception):
    """Stands in for ``modal.exception.NotFoundError`` (no modal import)."""


def _fake_modal(*, lookup_exc=None, sandboxes=(), list_exc=None) -> SimpleNamespace:
    """Minimal ``modal`` module stand-in for ``ModalPlane._modal``."""

    def _lookup(name):
        if lookup_exc is not None:
            raise lookup_exc
        return SimpleNamespace(app_id="ap-1")

    def _list(app_id=None):
        if list_exc is not None:
            raise list_exc
        return iter(sandboxes)

    return SimpleNamespace(
        App=SimpleNamespace(lookup=_lookup),
        Sandbox=SimpleNamespace(list=_list),
        exception=SimpleNamespace(NotFoundError=_NotFound),
    )


def _plane(apps: list[dict], workspace: str = "ws-test") -> ModalPlane:
    plane = ModalPlane()
    plane._apps = lambda: apps  # type: ignore[method-assign]
    plane.workspace = lambda: workspace  # type: ignore[method-assign]
    return plane


def test_app_url_ignores_stopped_app() -> None:
    plane = _plane([{"description": "sbx-control", "state": "stopped"}])
    assert plane.app_url("sbx-control") is None


def test_app_url_for_deployed_app() -> None:
    plane = _plane([{"description": "sbx-control", "state": "deployed"}])
    assert plane.app_url("sbx-control") == ("https://ws-test--sbx-control-fastapi-app.modal.run")


def test_app_url_missing_state_field_treated_as_deployed() -> None:
    plane = _plane([{"description": "sbx-control"}])
    assert plane.app_url("sbx-control") is not None


def test_stop_app_only_stops_deployed() -> None:
    plane = _plane(
        [
            {"description": "sbx-control", "state": "stopped", "app_id": "ap-1"},
            {"description": "other-app", "state": "deployed", "app_id": "ap-2"},
        ]
    )
    calls: list[list[str]] = []
    plane._cli = lambda argv, **kw: calls.append(argv) or ""  # type: ignore[method-assign]
    assert plane.stop_app("sbx-control") is False  # already stopped
    assert calls == []
    assert plane.stop_app("other-app") is True
    assert calls == [["app", "stop", "ap-2", "--yes"]]


def test_ensure_dict_reports_existing_honestly() -> None:
    plane = ModalPlane()
    plane.has_dict = lambda name: True  # type: ignore[method-assign]
    assert plane.ensure_dict("sbx-runs") is False


def test_ensure_dict_creates_when_absent() -> None:
    plane = ModalPlane()
    plane.has_dict = lambda name: False  # type: ignore[method-assign]
    created: list[tuple[str, bool]] = []

    class _Objects:
        def create(self, name: str, *, allow_existing: bool = False) -> None:
            created.append((name, allow_existing))

    plane._modal_dict = lambda: _Objects()  # type: ignore[method-assign]
    assert plane.ensure_dict("sbx-runs") is True
    assert created == [("sbx-runs", True)]


def test_ensure_dict_create_failure_is_actionable() -> None:
    plane = ModalPlane()
    plane.has_dict = lambda name: False  # type: ignore[method-assign]

    class _Objects:
        def create(self, name: str, *, allow_existing: bool = False) -> None:
            raise RuntimeError("denied")

    plane._modal_dict = lambda: _Objects()  # type: ignore[method-assign]
    with pytest.raises(BootstrapError) as exc:
        plane.ensure_dict("sbx-runs")
    assert exc.value.code == "modal_dict_failed"


def test_has_dict_list_failure_is_loud() -> None:
    """An unreadable Dict list must not be reported as "absent"."""
    plane = ModalPlane()

    class _Objects:
        def list(self):
            raise RuntimeError("Token missing")

    plane._modal_dict = lambda: _Objects()  # type: ignore[method-assign]
    with pytest.raises(BootstrapError) as exc:
        plane.has_dict("sbx-runs")
    assert exc.value.code == "modal_dict_failed"


def test_delete_dict_absent_returns_false() -> None:
    plane = ModalPlane()
    plane.has_dict = lambda name: False  # type: ignore[method-assign]
    plane._modal_dict = lambda: pytest.fail("delete must not run")  # type: ignore[method-assign]
    assert plane.delete_dict("sbx-runs") is False


def test_delete_dict_failure_is_loud() -> None:
    plane = ModalPlane()
    plane.has_dict = lambda name: True  # type: ignore[method-assign]

    class _Objects:
        def delete(self, name: str) -> None:
            raise RuntimeError("denied")

    plane._modal_dict = lambda: _Objects()  # type: ignore[method-assign]
    with pytest.raises(BootstrapError) as exc:
        plane.delete_dict("sbx-runs")
    assert exc.value.code == "modal_dict_failed"


def test_list_sandboxes_empty_when_app_missing() -> None:
    plane = ModalPlane()
    plane._modal = lambda: _fake_modal(lookup_exc=_NotFound("no app"))  # type: ignore[method-assign]
    assert plane.list_sandboxes("sbx-control") == []


def test_list_sandboxes_lookup_failure_is_loud() -> None:
    """Auth/network failures must not masquerade as "zero sandboxes"."""
    plane = ModalPlane()
    plane._modal = lambda: _fake_modal(lookup_exc=RuntimeError("Token missing"))  # type: ignore[method-assign]
    with pytest.raises(BootstrapError) as exc:
        plane.list_sandboxes("sbx-control")
    assert exc.value.code == "sandbox_list_failed"


def test_list_sandboxes_list_failure_is_loud() -> None:
    plane = ModalPlane()
    plane._modal = lambda: _fake_modal(list_exc=RuntimeError("denied"))  # type: ignore[method-assign]
    with pytest.raises(BootstrapError) as exc:
        plane.list_sandboxes("sbx-control")
    assert exc.value.code == "sandbox_list_failed"


def test_stop_app_list_failure_is_loud() -> None:
    """uninstall must not report "not running" when enumeration failed."""
    plane = ModalPlane()

    def _cli(argv, **kw):
        raise BootstrapError("modal app list failed", code="modal_cli_failed")

    plane._cli = _cli  # type: ignore[method-assign]
    with pytest.raises(BootstrapError) as exc:
        plane.stop_app("sbx-control")
    assert exc.value.code == "modal_cli_failed"


def test_app_url_list_failure_is_loud() -> None:
    plane = ModalPlane()

    def _cli(argv, **kw):
        raise BootstrapError("modal app list failed", code="modal_cli_failed")

    plane._cli = _cli  # type: ignore[method-assign]
    with pytest.raises(BootstrapError):
        plane.app_url("sbx-control")
