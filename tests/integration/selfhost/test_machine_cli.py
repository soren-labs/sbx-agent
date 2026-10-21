"""``sbx server machine``: operator registration with strict host keys and a live probe."""

from __future__ import annotations

import json

import pytest
from control.selfhost.config import ServerSettings
from tests.support import sshd as sshd_support
from tests.support.factories import make_principal

from sbx.cli import build_parser
from sbx.server_cli import run_operator

pytestmark = pytest.mark.skipif(
    not sshd_support.available(), reason="OpenSSH client/server binaries are not installed"
)


@pytest.fixture
def sshd(tmp_path):
    server = sshd_support.PrivateSshd(tmp_path / "sshd")
    server.start()
    yield server
    server.stop()


def _run(capsys, monkeypatch, db, *argv: str) -> tuple[int, dict]:
    monkeypatch.setenv("SBX_DATABASE_URL", db.dsn)
    args = build_parser().parse_args(["server", "machine", *argv])
    code = run_operator(args)
    return code, json.loads(capsys.readouterr().out)


def _add_args(server, tmp_path, workspace, *extra):
    target = server.target(tmp_path / "remote")
    return [
        "add",
        "ubuntu-dev",
        "--workspace",
        workspace,
        "--host",
        target["host"],
        "--port",
        str(target["port"]),
        "--user",
        target["user"],
        "--identity-file",
        target["identity_file"],
        "--known-hosts-file",
        target["known_hosts_file"],
        "--remote-python",
        target["remote_python"],
        "--remote-root",
        target["remote_root"],
        "--state-root",
        target["state_root"],
        *extra,
    ]


def test_add_probe_list_remove(capsys, monkeypatch, db, sshd, tmp_path) -> None:
    owner = make_principal(db)
    code, out = _run(
        capsys, monkeypatch, db, *_add_args(sshd, tmp_path, owner.default_workspace_id)
    )
    assert code == 0, out
    assert out["reachability"]["status"] == "reachable"
    assert out["reachability"]["runtime"]["digest_match"] is True
    machine_id = out["machine"]["id"]
    code, out = _run(capsys, monkeypatch, db, "probe", "ubuntu-dev")
    assert code == 0 and out["reachability"]["status"] == "reachable"
    code, out = _run(capsys, monkeypatch, db, "list")
    assert [m["id"] for m in out["machines"]] == [machine_id]
    code, out = _run(
        capsys, monkeypatch, db, *_add_args(sshd, tmp_path, owner.default_workspace_id)
    )
    assert code == 3 and "already exists" in out["problem"]["message"]
    code, out = _run(capsys, monkeypatch, db, "remove", machine_id)
    assert code == 0 and out["machine"]["state"] == "removed"
    assert db.read(lambda u: u.get("resident_machines", machine_id))["state"] == "removed"


def test_add_refuses_unknown_host_key_and_unreachable_machines(
    capsys, monkeypatch, db, sshd, tmp_path
) -> None:
    owner = make_principal(db)
    empty = tmp_path / "empty_known_hosts"
    empty.write_text("")
    args = _add_args(sshd, tmp_path, owner.default_workspace_id)
    args[args.index("--known-hosts-file") + 1] = str(empty)
    code, out = _run(capsys, monkeypatch, db, *args)
    assert code == 5 and out["problem"]["category"] == "authentication"
    assert "never accepts an unknown host key" in out["problem"]["action"]
    sshd.stop()
    code, out = _run(
        capsys, monkeypatch, db, *_add_args(sshd, tmp_path, owner.default_workspace_id)
    )
    assert code == 4 and out["reachability"]["status"] == "unreachable"
    assert db.read(lambda u: u.count("resident_machines")) == 0


def test_settings_accept_the_resident_executor(tmp_path) -> None:
    settings = ServerSettings.load(tmp_path / "none.env", environ={"SBX_EXECUTORS": "resident"})
    assert settings.executors == ["resident"]
