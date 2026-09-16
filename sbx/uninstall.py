"""``sbx uninstall`` — scoped teardown (SOR-98).

Default scope: terminate every sandbox the app owns and stop the deployed
app. Durable state (Dicts) and user credentials (Modal Secrets, the local
key file) are preserved unless the caller explicitly opts in with
``--purge-data`` / ``--purge-credentials``. After teardown the command
re-lists sandboxes — leftovers fail loudly instead of leaking compute.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from control.config import ACCOUNT_SECRET_PREFIX

from sbx.config import ResolvedConfig, basic_auth_path, deploy_state_path, key_path
from sbx.errors import BootstrapError
from sbx.plane import Plane


@dataclass(frozen=True)
class UninstallReport:
    terminated_sandboxes: tuple[str, ...]
    app_stopped: bool
    deleted_dicts: tuple[str, ...] = ()
    deleted_secrets: tuple[str, ...] = ()
    removed_local_files: tuple[str, ...] = ()
    preserved: tuple[str, ...] = field(default_factory=tuple)


def _remove(path, removed: list[str]) -> None:
    try:
        path.unlink()
        removed.append(str(path))
    except OSError:
        return


def uninstall(
    cfg: ResolvedConfig,
    plane: Plane,
    *,
    env: Mapping[str, str] | None = None,
    purge_data: bool = False,
    purge_credentials: bool = False,
) -> UninstallReport:
    env = os.environ if env is None else env
    config = cfg.config

    sandboxes = plane.list_sandboxes(config.modal_app_name)
    for sb in sandboxes:
        plane.terminate_sandbox(sb.id)
    remaining = plane.list_sandboxes(config.modal_app_name)
    if remaining:
        raise BootstrapError(
            f"{len(remaining)} sandbox(es) still live after terminate: "
            + ", ".join(sb.id for sb in remaining[:5]),
            hint="terminate them in the Modal dashboard or rerun `sbx uninstall`",
            code="uninstall_leftover",
        )

    app_stopped = plane.stop_app(config.modal_app_name)

    deleted_dicts: list[str] = []
    deleted_secrets: list[str] = []
    removed: list[str] = []
    preserved: list[str] = []

    if purge_data:
        for name in config.dict_names():
            if plane.delete_dict(name):
                deleted_dicts.append(name)
    else:
        preserved.extend(config.dict_names())

    if purge_credentials:
        targets = set(config.secret_names())
        # Scope the account-secret sweep to this deployment's prefix so a
        # parallel deploy's ``sbx-acct-*`` Secrets are never swept.
        prefix = config.account_secret_prefix or ACCOUNT_SECRET_PREFIX
        targets.update(n for n in plane.list_secret_names() if n.startswith(prefix))
        for name in sorted(targets):
            if plane.delete_secret(name):
                deleted_secrets.append(name)
        _remove(key_path(env), removed)
        _remove(basic_auth_path(env), removed)
        _remove(deploy_state_path(env), removed)
    else:
        preserved.extend(config.secret_names())
        preserved.append(str(key_path(env)))

    return UninstallReport(
        terminated_sandboxes=tuple(sb.id for sb in sandboxes),
        app_stopped=app_stopped,
        deleted_dicts=tuple(deleted_dicts),
        deleted_secrets=tuple(deleted_secrets),
        removed_local_files=tuple(removed),
        preserved=tuple(preserved),
    )
