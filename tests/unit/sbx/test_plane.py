"""``ModalPlane`` local logic: app state filtering + honest ensure_dict.

The Modal SDK is stubbed out — these tests pin the check-then-act and
``modal app list`` parsing semantics without cloud credentials.
"""

from __future__ import annotations

import pytest
from sbx.errors import BootstrapError
from sbx.plane import ModalPlane


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
    assert calls == [["app", "stop", "ap-2"]]


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
