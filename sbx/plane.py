"""Resource plane: the Modal-side operations deploy/doctor/uninstall need.

``Plane`` is the seam that keeps the CLI testable without cloud credentials —
tests drive a ``FakePlane``; production uses :class:`ModalPlane`, which lazy
imports ``modal`` and shells out to ``python -m modal`` exactly like
``control.deploy``. Nothing in this module touches Modal at import time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from sbx.errors import BootstrapError

_WEB_URL_RE = re.compile(r"https://[^\s'\"]+\.modal\.run")


@dataclass(frozen=True)
class SandboxInfo:
    id: str
    tags: dict[str, str] = field(default_factory=dict)


class Plane(Protocol):
    """Cloud resources the bootstrap lifecycle manages."""

    def workspace(self) -> str | None:
        """Active Modal workspace name, or None when unauthenticated."""

    def list_secret_names(self) -> set[str]:
        """Names of deployed Secrets (values are never readable)."""

    def ensure_secret(self, name: str, env: dict[str, str]) -> bool:
        """Create Secret ``name`` with env vars if absent; True when created."""

    def delete_secret(self, name: str) -> bool:
        """Delete Secret ``name``; True when it existed."""

    def ensure_dict(self, name: str) -> bool:
        """Create durable Dict ``name`` if absent; True when created."""

    def has_dict(self, name: str) -> bool:
        """Dict ``name`` exists (no creation side effect)."""

    def dict_len(self, name: str) -> int:
        """Readable key count of Dict ``name`` (durable-state probe)."""

    def delete_dict(self, name: str) -> bool:
        """Delete Dict ``name``; True when it existed."""

    def ensure_image(self, provider: str) -> None:
        """Build + publish the provider's named runtime image."""

    def deploy_app(self, app_name: str) -> str:
        """Deploy the control app idempotently; return its web base URL."""

    def app_url(self, app_name: str) -> str | None:
        """Deployed app base URL, or None when not deployed."""

    def stop_app(self, app_name: str) -> bool:
        """Stop the deployed app; True when it was running."""

    def list_sandboxes(self, app_name: str) -> list[SandboxInfo]:
        """Live sandboxes owned by the app."""

    def terminate_sandbox(self, sandbox_id: str) -> None:
        """Terminate one sandbox; absent is a no-op."""


class ModalPlane:
    """Production :class:`Plane` over the Modal SDK + CLI.

    ``profile`` maps to ``MODAL_PROFILE`` for subprocess calls and the SDK's
    default profile resolution. ``runner`` exists so tests can observe argv.
    """

    def __init__(self, *, profile: str = "", env: Mapping[str, str] | None = None) -> None:
        self._profile = profile
        self._base_env = dict(os.environ if env is None else env)

    # ------------------------------------------------------------- helpers

    def _env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(self._base_env)
        if self._profile:
            env["MODAL_PROFILE"] = self._profile
        env.update(extra or {})
        return env

    @staticmethod
    def _modal() -> Any:
        import modal

        return modal

    def _cli(self, argv: list[str], *, extra_env: Mapping[str, str] | None = None) -> str:
        """Run ``python -m modal <argv>``; return combined output."""
        proc = subprocess.run(
            [sys.executable, "-m", "modal", *argv],
            capture_output=True,
            text=True,
            env=self._env(extra_env),
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise BootstrapError(
                f"modal {' '.join(argv)} failed (rc={proc.returncode}): {output.strip()[-400:]}",
                hint="check Modal auth (`modal token new`) and the MODAL_PROFILE setting",
                code="modal_cli_failed",
            )
        return output

    def _modal_secret(self) -> Any:
        return self._modal().Secret.objects

    def _modal_dict(self) -> Any:
        return self._modal().Dict.objects

    # ------------------------------------------------------------ plane API

    def workspace(self) -> str | None:
        """Active workspace name — only when the token actually works.

        ``modal profile current`` prints a profile name even with no
        credentials, so auth is proven with a real API call.
        """
        proc = subprocess.run(
            [sys.executable, "-m", "modal", "profile", "current"],
            capture_output=True,
            text=True,
            env=self._env(),
        )
        name = (proc.stdout or "").strip()
        if proc.returncode != 0 or not name:
            return None
        probe = subprocess.run(
            [sys.executable, "-m", "modal", "app", "list", "--json"],
            capture_output=True,
            text=True,
            env=self._env(),
        )
        if probe.returncode != 0:
            return None
        return name

    def list_secret_names(self) -> set[str]:
        try:
            return {s.name for s in self._modal_secret().list() if getattr(s, "name", None)}
        except Exception as exc:
            raise BootstrapError(
                f"cannot list Modal secrets: {exc}",
                hint="run `modal token new` to authenticate, then `sbx doctor`",
                code="modal_auth_missing",
            ) from exc

    def ensure_secret(self, name: str, env: dict[str, str]) -> bool:
        if name in self.list_secret_names():
            return False
        self._modal_secret().create(name, env_dict=env)
        return True

    def delete_secret(self, name: str) -> bool:
        existed = name in self.list_secret_names()
        if existed:
            self._modal_secret().delete(name, allow_missing=True)
        return existed

    def ensure_dict(self, name: str) -> bool:
        try:
            self._modal_dict().create(name, allow_existing=True)
        except Exception as exc:
            raise BootstrapError(
                f"cannot ensure Modal Dict {name!r}: {exc}",
                hint="check Modal auth and workspace permissions, then rerun `sbx deploy`",
                code="modal_dict_failed",
            ) from exc
        return True

    def has_dict(self, name: str) -> bool:
        try:
            return any(getattr(d, "name", None) == name for d in self._modal_dict().list())
        except Exception:
            return False

    def dict_len(self, name: str) -> int:
        return int(self._modal().Dict.from_name(name).len())

    def delete_dict(self, name: str) -> bool:
        try:
            self._modal_dict().delete(name)
        except Exception:
            return False
        return True

    def ensure_image(self, provider: str) -> None:
        from runtime.image import build_named_image

        try:
            build_named_image(provider=provider)
        except SystemExit as exc:
            raise BootstrapError(
                f"image build for provider {provider!r} failed: {exc}",
                hint="check runtime/packages.txt pins and provider CLI availability",
                code="image_build_failed",
            ) from exc

    def deploy_app(self, app_name: str) -> str:
        output = self._cli(
            ["deploy", "-m", "control.modal_app"],
            extra_env={"SBX_MODAL_APP_NAME": app_name},
        )
        match = _WEB_URL_RE.search(output)
        if match:
            return match.group(0).rstrip("/")
        url = self.app_url(app_name)
        if url is None:
            raise BootstrapError(
                f"modal deploy succeeded but no web URL was found for app {app_name!r}",
                hint="check `modal app list` and control/modal_app.py web endpoints",
                code="deploy_url_missing",
            )
        return url

    def app_url(self, app_name: str) -> str | None:
        apps = self._apps()
        if not any(a.get("description") == app_name or a.get("name") == app_name for a in apps):
            return None
        workspace = self.workspace()
        if not workspace:
            return None
        return f"https://{workspace}--{app_name}-fastapi-app.modal.run"

    def _apps(self) -> list[dict[str, Any]]:
        try:
            raw = self._cli(["app", "list", "--json"])
        except BootstrapError:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def stop_app(self, app_name: str) -> bool:
        for entry in self._apps():
            if entry.get("description") == app_name or entry.get("name") == app_name:
                app_id = entry.get("app_id") or entry.get("id")
                if app_id:
                    self._cli(["app", "stop", str(app_id)])
                    return True
        return False

    def list_sandboxes(self, app_name: str) -> list[SandboxInfo]:
        modal = self._modal()
        try:
            app = modal.App.lookup(app_name)
        except Exception:
            return []
        app_id = getattr(app, "app_id", None)
        found: list[SandboxInfo] = []
        for sb in modal.Sandbox.list(app_id=app_id):
            try:
                tags = dict(sb.get_tags())
            except Exception:
                tags = {}
            found.append(SandboxInfo(id=sb.object_id, tags=tags))
        return found

    def terminate_sandbox(self, sandbox_id: str) -> None:
        try:
            self._modal().Sandbox.from_id(sandbox_id).terminate()
        except Exception:
            return
