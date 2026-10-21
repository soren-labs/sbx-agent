"""`sbx server` end to end: a private PostgreSQL, a real background control plane, first-run
identity, a Session created and read, stop/restart with durable state, and categorised
diagnostics. Local only: no cloud account, no credentials (fixtures use REDACTED)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from control.selfhost.config import ServerSettings
from control.selfhost.postgres import ManagedPostgres, find_bindir
from control.selfhost.process import ServerProcess

from sbx.cli import main as cli_main
from sbx.sdk import SBXClient

pytestmark = pytest.mark.skipif(
    find_bindir() is None or os.geteuid() == 0,
    reason="needs PostgreSQL server binaries and a non-root user",
)

PASSWORD = "local test password 1234"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    capsys.readouterr()
    code = cli_main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip().startswith("{") else {"raw": out}


@pytest.fixture
def instance(capsys) -> Iterator[dict[str, object]]:
    # A short base path: the PostgreSQL Unix socket path must stay under ~104 bytes.
    base = Path(tempfile.mkdtemp(prefix="sbxsh-", dir="/tmp"))
    config, data = base / "c" / "server.env", base / "d"
    port = _free_port()
    yield {"config": config, "data": data, "port": port, "base": base}
    if config.exists():
        ServerProcess(ServerSettings.load(config)).stop(timeout=10)
    shutil.rmtree(base, ignore_errors=True)


def test_first_start_session_and_restart_keep_durable_state(instance, capsys) -> None:
    config, data, port = str(instance["config"]), str(instance["data"]), instance["port"]
    code, init = _cli(
        capsys,
        "server",
        "init",
        "--config",
        config,
        "--data-dir",
        data,
        "--port",
        str(port),
        "--executors",
        "local",
    )
    assert code == 0 and init["database"]["managed"] is True
    text = Path(config).read_text()
    assert oct(Path(config).stat().st_mode & 0o777) == "0o600"
    assert "SBX_VAULT_KEYS=" in text and "SBX_MANAGED_POSTGRES=1" in text
    for line in text.splitlines():
        if line.startswith(("SBX_VAULT_KEYS=", "SBX_RUNTIME_MASTER_KEY=")):
            assert line.split("=", 1)[1] not in json.dumps(init), "keys stay in the file"

    # A second init never replaces the keys.
    code, again = _cli(capsys, "server", "init", "--config", config, "--data-dir", data)
    assert code == 3 and "already exists" in again["problem"]["message"]
    assert Path(config).read_text() == text

    code, checked = _cli(capsys, "server", "check", "--config", config)
    assert code == 0, checked["problem"]
    names = {c["name"]: c for c in checked["checks"]}
    assert names["managed_postgres"]["status"] == "warn"  # stopped until start

    code, started = _cli(capsys, "server", "start", "--config", config)
    assert code == 0, started
    assert started["state"] == "running" and started["action_taken"] == "started"
    first_pid = started["pid"]
    code, again = _cli(capsys, "server", "start", "--config", config)
    assert code == 0 and again["action_taken"] == "already_running" and again["pid"] == first_pid

    url = f"http://127.0.0.1:{port}"
    client = SBXClient(url)
    unauthenticated = client.diagnose()
    assert unauthenticated["problem"]["category"] == "authentication"

    client.register("owner@example.test", PASSWORD)
    code, mail = _cli(capsys, "server", "outbox", "--config", config, "--to", "owner@example.test")
    assert code == 0 and "token=" in mail["body"]
    client.verify_email(mail["body"].split("token=")[1].strip())
    client.login("owner@example.test", PASSWORD)
    key = client.create_api_key("b1", ["write"])["key"]
    client = SBXClient(url, key)
    report = client.diagnose()
    assert report["ok"] is True and report["executor_backends"] == ["local"]

    connection = client.connections.add(
        "inference_api",
        {
            "api_key": "REDACTED",
            "endpoints": {"openai_chat": "https://inference.sbx-test.invalid/v1"},
            "model": "stub-model",
        },
        "local-stub",
    )
    session = client.sessions.create(
        executor={"backend": "local"}, harness={"provider_id": "opencode", "model": "stub-model"}
    )
    session_id = session["session_id"]
    before = client.sessions.get(session_id)["session"]
    assert before["connections"]["inference"] == connection["id"]

    code, stopped = _cli(capsys, "server", "stop", "--config", config)
    assert code == 0 and stopped["action_taken"] == "stopped" and stopped["postgres"] == "stopped"
    assert not ManagedPostgres(Path(data)).running()
    code, status = _cli(capsys, "server", "status", "--config", config)
    assert code == 4 and status["state"] == "stopped"
    down = SBXClient(url, key).diagnose()
    assert down["problem"]["category"] == "reachability" and down["problem"]["name"] == "server"

    code, started = _cli(capsys, "server", "start", "--config", config)
    assert code == 0 and started["pid"] != first_pid
    after = client.sessions.get(session_id)["session"]
    assert after["id"] == before["id"] and after["version"] == before["version"]
    assert after["created_at"] == before["created_at"]
    assert [s["id"] for s in client.sessions.list()["items"]] == [session_id]

    code, restarted = _cli(capsys, "server", "restart", "--config", config)
    assert code == 0 and restarted["previous_pid"] == started["pid"]
    assert client.sessions.get(session_id)["session"]["id"] == session_id
    code, status = _cli(capsys, "server", "status", "--config", config)
    assert code == 0 and status["state"] == "running" and status["problem"] is None


def test_separate_instance_uses_only_its_own_config(instance, capsys) -> None:
    """Instance B (--config) must not inherit the default config A's optional settings."""
    default_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "sbx"
    default_dir.mkdir(parents=True)
    default = default_dir / "server.env"
    config, data, port = str(instance["config"]), str(instance["data"]), instance["port"]
    # Same data dir on purpose: only the config path tells the instances apart.
    default.write_text(
        "SBX_RESEND_API_KEY=REDACTED\nSBX_MAIL_FROM=SBX <no-reply@a.example>\n"
        f"SBX_EXECUTORS=local,modal\nSBX_DATA_DIR={data}\n"
    )
    os.chmod(default, 0o600)
    code, _ = _cli(
        capsys,
        "server",
        "init",
        "--config",
        config,
        "--data-dir",
        data,
        "--port",
        str(port),
        "--executors",
        "local",
    )
    assert code == 0
    code, started = _cli(capsys, "server", "start", "--config", config)
    assert code == 0, started
    environ = dict(
        item.split("=", 1)
        for item in Path(f"/proc/{started['pid']}/environ").read_text().split("\0")
        if "=" in item
    )
    assert environ["SBX_SERVER_CONFIG"] == str(Path(config).absolute())
    assert environ["SBX_EXECUTORS"] == "local"
    assert "SBX_RESEND_API_KEY" not in environ and "SBX_MAIL_FROM" not in environ
    # A's config, even over the same data dir and PID file, does not claim B's process.
    other = ServerSettings.load(default, environ={})
    assert other.data_dir == Path(data) and ServerProcess(other).pid() is None
    code, untouched = _cli(capsys, "server", "stop", "--config", str(default))
    assert code == 0 and untouched["action_taken"] == "not_running"
    assert untouched["data_dir_in_use_by"] == started["pid"]
    code, refused = _cli(capsys, "server", "start", "--config", str(default))
    assert code == 3 and refused["action_taken"] == "refused"
    assert "data_dir_in_use" in {c["name"] for c in refused["checks"] if c["status"] == "fail"}
    assert ServerProcess(ServerSettings.load(Path(config))).pid() == started["pid"]
    code, stopped = _cli(capsys, "server", "stop", "--config", config)
    assert code == 0 and stopped["pid"] == started["pid"]


def test_database_failures_are_categorised(instance, capsys, monkeypatch) -> None:
    config, data = instance["config"], instance["data"]
    code, _ = _cli(
        capsys,
        "server",
        "init",
        "--config",
        str(config),
        "--data-dir",
        str(data),
        "--port",
        str(instance["port"]),
    )
    assert code == 0
    managed = ManagedPostgres(data)
    managed.start()
    try:
        socket_dir = managed.socket_dir
        cases = {
            # Peer authentication as a role that does not match the OS user.
            f"host={socket_dir} port=5432 user=sbx_nobody dbname=sbx": ("authentication", 5),
            f"host={socket_dir} port=5432 dbname=sbx_missing": ("config", 3),
            "host=127.0.0.1 port=9 dbname=sbx connect_timeout=2": ("reachability", 4),
        }
        monkeypatch.setenv("SBX_MANAGED_POSTGRES", "0")
        for dsn, (category, exit_status) in cases.items():
            monkeypatch.setenv("SBX_DATABASE_URL", dsn)
            code, report = _cli(capsys, "server", "check", "--config", str(config))
            assert report["problem"]["name"] == "database", report["problem"]
            assert report["problem"]["category"] == category and code == exit_status, dsn
            assert dsn not in json.dumps(report)
            if category == "authentication":
                assert "Peer authentication failed" in report["problem"]["message"]
        # Start refuses with the same category instead of spawning a failing server.
        monkeypatch.setenv("SBX_DATABASE_URL", next(iter(cases)))
        code, refused = _cli(capsys, "server", "start", "--config", str(config))
        assert code == 5 and refused["action_taken"] == "refused"
        assert not (Path(data) / "run" / "server.pid").exists()
    finally:
        managed.stop()


def test_unreachable_server_and_rejected_key_are_distinct(capsys) -> None:
    port = _free_port()
    code, report = _cli(capsys, "--base-url", f"http://127.0.0.1:{port}", "status")
    assert code == 4 and report["problem"]["category"] == "reachability"
    assert "sbx server start" in report["problem"]["action"]
    capsys.readouterr()
    code = cli_main(["--base-url", f"http://127.0.0.1:{port}", "sessions", "list"])
    error = json.loads(capsys.readouterr().err)["error"]
    assert code == 1 and error["code"] == "transport_error"
    assert f"127.0.0.1:{port}" in error["message"] and "sbx status" in error["action"]
