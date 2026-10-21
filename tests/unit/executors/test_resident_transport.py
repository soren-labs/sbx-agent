"""Resident executor: strict SSH invocation, operator-only target, failure categories."""

from __future__ import annotations

import io
import json
import shlex
import sys

import pytest
from control.domain.errors import DomainError
from control.executors.resident import (
    AgentRefused,
    ResidentExecutor,
    Target,
    Unreachable,
    classify,
    remote_command,
    runtime_source_digest,
    ssh_argv,
    ssh_env,
)
from runtime.resident import agent

TARGET = {
    "host": "203.0.113.7",
    "port": 2222,
    "user": "dev",
    "identity_file": "/keys/id_ed25519",
    "known_hosts_file": "/keys/known_hosts",
    "remote_python": "/home/dev/sbx/.venv/bin/python",
    "remote_root": "/home/dev/sbx checkout",
    "state_root": "~/.local/state/sbx-resident",
}


def test_ssh_is_strict_explicit_and_never_reads_ssh_config() -> None:
    argv = ssh_argv(Target.from_dict(TARGET))
    text = " ".join(argv)
    assert argv[1:3] == ["-F", "none"]
    for option in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=/keys/known_hosts",
        "GlobalKnownHostsFile=/dev/null",
        "IdentitiesOnly=yes",
        "ForwardAgent=no",
        "UpdateHostKeys=no",
    ):
        assert option in argv, option
    assert "StrictHostKeyChecking=no" not in text and "accept-new" not in text
    assert argv[-3:] == ["-l", "dev", "203.0.113.7"]


def test_remote_command_runs_only_the_agent_with_quoted_operator_paths() -> None:
    command = remote_command(Target.from_dict(TARGET), "probe")
    assert command.startswith("cd -- '/home/dev/sbx checkout' && exec ")
    tokens = shlex.split(command.split("&& exec ", 1)[1])
    assert tokens[:5] == [
        "/home/dev/sbx/.venv/bin/python",
        "-E",
        "-s",
        "-m",
        "runtime.resident.agent",
    ]
    assert tokens[-1] == "probe"
    assert "~/" in command  # the remote shell expands the state root under the SSH user's HOME


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "-oProxyCommand=sh"),
        ("host", "evil host"),
        ("user", "root;id"),
        ("identity_file", "relative/key"),
        ("remote_root", "~/sbx"),
        ("remote_python", "python3"),
        ("port", 0),
        ("remote_path", "bin:/usr/bin"),
        ("cli_overrides", {"LD_PRELOAD": "/x.so"}),
        ("cli_overrides", {"CLAUDE_BIN": "claude"}),
    ],
)
def test_target_rejects_anything_but_explicit_safe_values(field, value) -> None:
    with pytest.raises(DomainError) as exc:
        Target.from_dict({**TARGET, field: value})
    assert exc.value.code == "validation_failed"


def test_ssh_failures_are_categorised_and_never_absence() -> None:
    assert classify(255, "Host key verification failed.")[0] == "host_key_unverified"
    assert classify(255, "dev@h: Permission denied (publickey).")[0] == "auth_failed"
    assert classify(255, "ssh: connect to host h port 22: Connection refused")[0] == "unreachable"
    assert classify(127, "bash: /x/python: No such file or directory")[0] == "runtime_missing"

    class Down:
        def call(self, *a, **k):
            raise Unreachable("unreachable", "Connection timed out")

    executor = ResidentExecutor(transport=Down(), expected_digest="d" * 32)
    compute = {"target": TARGET}
    for call in (
        lambda: executor.lookup("op_1", compute),
        lambda: executor.describe({"operation_id": "op_1"}, compute),
        lambda: executor.terminate({"operation_id": "op_1", "lease_id": "l"}, "t", compute),
    ):
        with pytest.raises(DomainError) as exc:
            call()
        assert exc.value.code == "executor_unavailable" and exc.value.retryable


def test_agent_refusals_map_to_public_codes() -> None:
    class Refusing:
        def __init__(self, code):
            self.code = code

        def call(self, *a, **k):
            raise AgentRefused(self.code, "no", {"runtime_digest": "a", "expected_digest": "b"})

    compute = {"target": TARGET}
    spec = {"lease_id": "l", "generation": 1, "enrollment_key": "ab" * 32, "compute": compute}
    with pytest.raises(DomainError) as exc:
        ResidentExecutor(transport=Refusing("runtime_mismatch")).allocate(spec, "op")
    assert exc.value.code == "runtime_incompatible" and not exc.value.retryable
    assert exc.value.details == {"runtime_digest": "a", "expected_digest": "b"}
    with pytest.raises(DomainError) as exc:
        ResidentExecutor(transport=Refusing("busy")).allocate(spec, "op")
    assert exc.value.code == "executor_unavailable" and exc.value.retryable


def test_ssh_process_gets_no_server_secrets(monkeypatch) -> None:
    monkeypatch.setenv("SBX_VAULT_KEYS", "k1:REDACTED")
    monkeypatch.setenv("SBX_RUNTIME_MASTER_KEY", "REDACTED")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    env = ssh_env()
    assert "SBX_VAULT_KEYS" not in env and "SBX_RUNTIME_MASTER_KEY" not in env
    assert env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"


def test_control_and_agent_compute_the_same_runtime_digest() -> None:
    assert runtime_source_digest() == agent.source_digest()
    assert agent.DIGEST_PACKAGES == tuple(
        __import__("control.executors.resident", fromlist=["x"]).DIGEST_PACKAGES
    )


def _run_agent(monkeypatch, capsys, argv, request, env=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    assert agent.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_agent_forced_command_accepts_only_its_verbs(monkeypatch, capsys, tmp_path) -> None:
    root = ["--state-root", str(tmp_path), "--forced"]
    reply = _run_agent(
        monkeypatch, capsys, root, {}, {"SSH_ORIGINAL_COMMAND": "cd /x && exec py -m a probe"}
    )
    assert reply["ok"] and reply["runtime_digest"] == agent.source_digest()
    reply = _run_agent(monkeypatch, capsys, root, {}, {"SSH_ORIGINAL_COMMAND": "rm -rf / ; sh"})
    assert reply == {
        "ok": False,
        "error": {"code": "validation_failed", "message": "unknown verb", "details": {}},
    }


def test_agent_rejects_path_like_identifiers_and_never_echoes_the_key(
    monkeypatch, capsys, tmp_path
) -> None:
    reply = _run_agent(
        monkeypatch,
        capsys,
        ["--state-root", str(tmp_path), "allocate"],
        {
            "operation_id": "../../etc",
            "lease_id": "l",
            "generation": 1,
            "enrollment_key": "ab" * 32,
        },
    )
    assert reply["ok"] is False and reply["error"]["code"] == "validation_failed"
    assert "ab" * 32 not in json.dumps(reply)
    assert not (tmp_path / "leases").exists()


@pytest.mark.parametrize(
    "code,stderr",
    [
        (255, "ssh: connect to host 203.0.113.7 port 2222: Connection refused"),
        (255, "ssh: Could not resolve hostname secret-host.internal: Name or service not known"),
        (255, "dev@203.0.113.7: Permission denied (publickey)."),
        (255, "Host key for [203.0.113.7]:2222 has changed\nHost key verification failed."),
        (127, "bash: line 1: /home/dev/private/.venv/bin/python: No such file or directory"),
        (255, "kex_exchange_identification: read: weird failure at 203.0.113.7"),
    ],
)
def test_reasons_are_fixed_phrases_without_host_or_paths(code, stderr) -> None:
    status, reason = classify(code, stderr)
    assert status in ("unreachable", "auth_failed", "host_key_unverified", "runtime_missing")
    for detail in ("203.0.113.7", "2222", "secret-host", "/home/dev"):
        assert detail not in reason
