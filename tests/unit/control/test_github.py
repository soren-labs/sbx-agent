"""SOR-117: optional GitHub auth bridge — detection, opt-in gate, env wiring,
and repo-native collaboration helpers (push / PR create).

Everything here is deterministic and cloud-free: ``RecordingBackend`` captures
argv/env instead of spawning, and the git-level tests use local file remotes
plus ``git credential fill`` against the generated ``GIT_CONFIG_*`` env.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from control import github
from control.backend import (
    LocalProcessBackend,
    Process,
    SandboxHandle,
    SandboxPoll,
    SandboxSpec,
)
from control.sandbox_io import sandbox_env
from control.workspace import (
    REPO_UNAVAILABLE,
    WORKSPACE_INVALID,
    InMemoryWorkspaceStore,
    WorkspaceError,
    WorkspaceService,
    WorkspaceSpec,
    create_pull_request,
    git_push,
)

GH_VALUE = "REDACTED_GITHUB"


# --------------------------------------------------------------------------
# fakes


class _FakeProc:
    def __init__(self, lines: list[str], code: int) -> None:
        self.stdout: Iterator[str] = iter(lines)
        self._code = code

    def wait(self) -> int:
        return self._code

    def kill(self) -> None:
        return None


class RecordingBackend:
    """``SandboxBackend`` that records (argv, env) and replays canned stdout."""

    def __init__(self, results: list[tuple[list[str], int]] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._results = list(results or [])

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        return SandboxHandle(id="sb-rec", root=Path("/work"), tags=dict(spec.tags))

    def exec(
        self,
        handle: SandboxHandle,
        argv: list[str],
        env: Mapping[str, str] | None = None,
    ) -> Process:
        self.calls.append((list(argv), dict(env or {})))
        lines, code = self._results.pop(0) if self._results else ([], 0)
        return _FakeProc(lines, code)

    def terminate(self, handle: SandboxHandle) -> None:
        return None

    def poll(self, handle: SandboxHandle) -> SandboxPoll:
        return SandboxPoll(alive=True, active_processes=0)

    def list(self, tags: Mapping[str, str] | None = None) -> list[SandboxHandle]:
        return []


HANDLE = SandboxHandle(id="sb-gh", root=Path("/work"), tags={})
GIT_ENV_BASE = {
    "GIT_AUTHOR_NAME": "sbx-test",
    "GIT_AUTHOR_EMAIL": "sbx-test@localhost",
    "GIT_COMMITTER_NAME": "sbx-test",
    "GIT_COMMITTER_EMAIL": "sbx-test@localhost",
}


def host_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env={**os.environ, **GIT_ENV_BASE},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def make_repo(root: Path, name: str = "origin") -> tuple[Path, str]:
    repo = root / name
    repo.mkdir()
    host_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    host_git(repo, "add", "-A")
    host_git(repo, "commit", "-qm", "A")
    return repo, host_git(repo, "rev-parse", "HEAD")


# --------------------------------------------------------------------------
# token resolution + opt-in gate


class TestTokenResolution:
    def test_gh_token_wins(self) -> None:
        env = {"GH_TOKEN": "a", "GITHUB_TOKEN": "b"}
        assert github.token_source(env) == "GH_TOKEN"
        assert github.resolve_token(env) == "a"

    def test_github_token_fallback(self) -> None:
        env = {"GITHUB_TOKEN": "b"}
        assert github.token_source(env) == "GITHUB_TOKEN"
        assert github.resolve_token(env) == "b"

    @pytest.mark.parametrize("env", [{}, {"GH_TOKEN": ""}, {"GH_TOKEN": "  \n"}])
    def test_empty_is_no_token(self, env: dict[str, str]) -> None:
        assert github.token_source(env) is None
        assert github.resolve_token(env) is None


class TestOptInGate:
    def test_flag_alone_injects_nothing(self) -> None:
        env = {"SBX_GITHUB_EPHEMERAL": "1"}
        assert github.opted_in(env)
        assert not github.injection_enabled(env)
        assert github.secret_env(env) == {}
        assert github.exec_env(env) == {}

    def test_token_alone_injects_nothing(self) -> None:
        env = {"GH_TOKEN": GH_VALUE}
        assert not github.opted_in(env)
        assert not github.injection_enabled(env)
        assert github.secret_env(env) == {}
        assert github.exec_env(env) == {}

    @pytest.mark.parametrize("flag", ["0", "true", "yes", ""])
    def test_gate_requires_exact_1(self, flag: str) -> None:
        env = {"SBX_GITHUB_EPHEMERAL": flag, "GH_TOKEN": GH_VALUE}
        assert not github.injection_enabled(env)
        assert github.exec_env(env) == {}

    def test_armed_secret_env_populates_both_names(self) -> None:
        env = {"SBX_GITHUB_EPHEMERAL": "1", "GITHUB_TOKEN": GH_VALUE}
        out = github.secret_env(env)
        assert out == {"GH_TOKEN": GH_VALUE, "GITHUB_TOKEN": GH_VALUE}


class TestExecEnv:
    def test_exec_env_shape(self) -> None:
        env = {"SBX_GITHUB_EPHEMERAL": "1", "GH_TOKEN": GH_VALUE}
        out = github.exec_env(env)
        assert out["GH_TOKEN"] == GH_VALUE
        assert out["GITHUB_TOKEN"] == GH_VALUE
        assert out["GIT_TERMINAL_PROMPT"] == "0"
        assert out["GIT_CONFIG_COUNT"] == "2"
        # The helper value carries the literal $GH_TOKEN reference — the
        # token itself appears only under the token var names, never inside
        # the git wiring.
        helper_pairs = {
            out[f"GIT_CONFIG_KEY_{i}"]: out[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(out["GIT_CONFIG_COUNT"]))
        }
        assert helper_pairs["credential.helper"] == ""
        helper = helper_pairs["credential.https://github.com.helper"]
        assert "$GH_TOKEN" in helper
        assert GH_VALUE not in helper

    def test_exec_env_empty_without_gate(self) -> None:
        assert github.exec_env({"GH_TOKEN": GH_VALUE}) == {}

    def test_owns_env_key(self) -> None:
        for key in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GIT_TERMINAL_PROMPT",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_7",
            "GIT_CONFIG_PARAMETERS",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "GIT_SSH_COMMAND",
        ):
            assert github.owns_env_key(key)
        for key in ("PATH", "GH_TOKENX", "GIT_CONFIG", "GIT_CONFIG_KEYS"):
            assert not github.owns_env_key(key)


# --------------------------------------------------------------------------
# URL redaction + slug parsing


class TestUrlHelpers:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/o/r", "https://github.com/o/r"),
            (
                "https://user:SECRET@github.com/o/r.git",
                "https://<redacted>@github.com/o/r.git",
            ),
            ("https://x-access-token:TOK@github.com/o/r", "https://<redacted>@github.com/o/r"),
            # '@' past the authority is a path char, not userinfo.
            ("https://github.com/o/r@rev", "https://github.com/o/r@rev"),
            ("git@github.com:o/r.git", "git@github.com:o/r.git"),
            ("/plain/path", "/plain/path"),
            ("not a url", "not a url"),
        ],
    )
    def test_redact_url_credentials(self, url: str, expected: str) -> None:
        assert github.redact_url_credentials(url) == expected

    def test_redact_non_str_passthrough(self) -> None:
        assert github.redact_url_credentials(None) is None
        assert github.redact_url_credentials(3) == 3

    @pytest.mark.parametrize(
        ("url", "slug"),
        [
            ("https://github.com/owner/repo", "owner/repo"),
            ("https://github.com/owner/repo.git", "owner/repo"),
            ("https://github.com/owner/repo/", "owner/repo"),
            ("https://user:tok@github.com/owner/repo", "owner/repo"),
            ("git@github.com:owner/repo", "owner/repo"),
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("ssh://git@github.com/owner/repo", "owner/repo"),
            ("ssh://git@github.com:22/owner/repo.git", "owner/repo"),
        ],
    )
    def test_repo_slug_accepts_github_forms(self, url: str, slug: str) -> None:
        assert github.repo_slug(url) == slug

    @pytest.mark.parametrize(
        "url",
        [
            "https://gitlab.com/owner/repo",
            "https://evilgithub.com/owner/repo",
            "https://github.com.evil.com/owner/repo",
            "https://github.com/owner",
            "https://github.com/owner/repo/issues",
            "git@gitlab.com:owner/repo",
            "ssh://git@github.com:evil@x/owner/repo",
            "",
            "/local/path",
            None,
            42,
        ],
    )
    def test_repo_slug_rejects_non_github(self, url: Any) -> None:
        assert github.repo_slug(url) is None


# --------------------------------------------------------------------------
# detection — metadata only, never token material


class _FakeProcResult:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class TestDetection:
    def test_env_token_detected_by_name_only(self) -> None:
        det = github.detect({"GH_TOKEN": GH_VALUE})
        assert det.detected
        assert det.token_env == "GH_TOKEN"
        assert GH_VALUE not in repr(det)

    def test_nothing_detected(self) -> None:
        det = github.detect({}, which=lambda name: None)
        assert not det.detected
        assert det.token_env is None
        assert not det.gh_on_path
        assert det.gh_authenticated is None

    def test_gh_probe_gated_on_flag(self) -> None:
        calls: list[Any] = []

        def runner(*args: Any, **kwargs: Any) -> _FakeProcResult:
            calls.append((args, kwargs))
            return _FakeProcResult(0)

        det = github.detect({}, probe_gh=False, runner=runner, which=lambda name: "/usr/bin/gh")
        assert det.gh_on_path
        assert det.gh_authenticated is None
        assert calls == []  # no probe without the flag

        det = github.detect({}, probe_gh=True, runner=runner, which=lambda name: "/usr/bin/gh")
        assert det.gh_authenticated is True
        assert det.detected
        assert calls and calls[0][0][0][:3] == ["/usr/bin/gh", "auth", "status"]

    def test_gh_probe_env_is_scrubbed(self) -> None:
        """The probe child must not inherit GH_TOKEN/GITHUB_TOKEN — gh's own
        stored auth is what is being measured, and ambient tokens must not
        leak into the subprocess."""
        seen: dict[str, Any] = {}

        def runner(*args: Any, **kwargs: Any) -> _FakeProcResult:
            seen.update(kwargs)
            return _FakeProcResult(1)

        det = github.detect(
            {"GH_TOKEN": GH_VALUE, "HOME": "/home/x", "PATH": "/usr/bin"},
            probe_gh=True,
            runner=runner,
            which=lambda name: "/usr/bin/gh",
        )
        child_env = seen["env"]
        assert "GH_TOKEN" not in child_env
        assert "GITHUB_TOKEN" not in child_env
        assert child_env["HOME"] == "/home/x"
        # gh's own auth says no → token_env still reports the env source.
        assert det.gh_authenticated is False
        assert det.token_env == "GH_TOKEN"

    def test_probe_failure_is_inconclusive_not_false(self) -> None:
        def runner(*args: Any, **kwargs: Any) -> _FakeProcResult:
            raise OSError("spawn failed")

        det = github.detect({}, probe_gh=True, runner=runner, which=lambda name: "/usr/bin/gh")
        assert det.gh_authenticated is None

    def test_opted_in_reflected_in_detection(self) -> None:
        det = github.detect(
            {"SBX_GITHUB_EPHEMERAL": "1", "GH_TOKEN": GH_VALUE},
            which=lambda name: None,
        )
        assert det.opted_in
        assert det.token_env == "GH_TOKEN"


# --------------------------------------------------------------------------
# sandbox_env integration: injection + isolation


class TestSandboxEnvIntegration:
    def test_opted_in_env_reaches_exec(self, monkeypatch) -> None:
        monkeypatch.setenv("SBX_GITHUB_EPHEMERAL", "1")
        monkeypatch.setenv("GH_TOKEN", GH_VALUE)
        env = sandbox_env(HANDLE)
        assert env["GH_TOKEN"] == GH_VALUE
        assert env["GITHUB_TOKEN"] == GH_VALUE
        assert env["GIT_CONFIG_COUNT"] == "2"

    def test_ambient_token_without_gate_never_leaks(self, monkeypatch) -> None:
        """The isolation guarantee: an ambient GH_TOKEN with no opt-in must
        not reach a sandbox exec env — for ANY provider."""
        monkeypatch.delenv("SBX_GITHUB_EPHEMERAL", raising=False)
        monkeypatch.setenv("GH_TOKEN", GH_VALUE)
        for provider in ("codex", "devin", "grok"):
            handle = SandboxHandle(
                id=f"sb-{provider}", root=Path("/work"), tags={"provider": provider}
            )
            env = sandbox_env(handle)
            assert "GH_TOKEN" not in env
            assert "GITHUB_TOKEN" not in env
            assert not any(k.startswith("GIT_CONFIG_") for k in env)
            assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_extra_cannot_smuggle_or_override_github_keys(self, monkeypatch) -> None:
        monkeypatch.setenv("SBX_GITHUB_EPHEMERAL", "1")
        monkeypatch.setenv("GH_TOKEN", GH_VALUE)
        env = sandbox_env(
            HANDLE,
            {
                "GH_TOKEN": "smuggled",
                "GITHUB_TOKEN": "smuggled",
                "GIT_TERMINAL_PROMPT": "1",
                "GIT_CONFIG_COUNT": "9",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "!echo hacked",
                "GIT_CONFIG_PARAMETERS": "'credential.helper=!echo pwned'",
                "GIT_ASKPASS": "/tmp/evil",
                "GIT_SSH_COMMAND": "/tmp/evil-ssh",
            },
        )
        assert env["GH_TOKEN"] == GH_VALUE
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert "GIT_ASKPASS" not in env
        assert "GIT_SSH_COMMAND" not in env
        assert "GIT_CONFIG_PARAMETERS" not in env
        # And with the gate off, the same extra still cannot inject.
        monkeypatch.delenv("SBX_GITHUB_EPHEMERAL")
        env = sandbox_env(HANDLE, {"GH_TOKEN": "smuggled", "GIT_CONFIG_COUNT": "9"})
        assert "GH_TOKEN" not in env
        assert "GIT_CONFIG_COUNT" not in env


# --------------------------------------------------------------------------
# real-git verification of the credential-helper env


class TestCredentialHelperEndToEnd:
    """Run real ``git credential fill`` under the generated env — the exact
    mechanism a sandboxed ``git clone https://github.com/...`` uses."""

    def _fill(
        self, overlay: dict[str, str], home: Path, host: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(home),
            **overlay,
        }
        return subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost={host}\n\n",
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_helper_answers_github_with_token(self, tmp_path: Path) -> None:
        overlay = github.exec_env({"SBX_GITHUB_EPHEMERAL": "1", "GH_TOKEN": GH_VALUE})
        res = self._fill(overlay, tmp_path, "github.com")
        assert res.returncode == 0, res.stderr
        assert f"password={GH_VALUE}" in res.stdout
        assert "username=x-access-token" in res.stdout

    def test_helper_scoped_to_github_only(self, tmp_path: Path) -> None:
        """A non-github host must NOT receive the credential — the helper is
        URL-scoped, and GIT_TERMINAL_PROMPT=0 turns the would-be prompt into
        a fast failure."""
        overlay = github.exec_env({"SBX_GITHUB_EPHEMERAL": "1", "GH_TOKEN": GH_VALUE})
        res = self._fill(overlay, tmp_path, "gitlab.example.com")
        assert res.returncode != 0
        assert GH_VALUE not in res.stdout


# --------------------------------------------------------------------------
# git_push / create_pull_request — argv safety + error contract


class TestGitPush:
    def test_push_argv_and_env(self, monkeypatch) -> None:
        monkeypatch.setenv("SBX_GITHUB_EPHEMERAL", "1")
        monkeypatch.setenv("GH_TOKEN", GH_VALUE)
        backend = RecordingBackend()
        git_push(backend, HANDLE, "repo", "HEAD:refs/heads/sbx/review")
        argv, env = backend.calls[0]
        assert argv[:3] == ["git", "-C", "/work/repo"]
        assert argv[3:] == ["push", "origin", "HEAD:refs/heads/sbx/review"]
        assert GH_VALUE not in " ".join(argv)  # token never in argv
        assert env["GH_TOKEN"] == GH_VALUE
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    @pytest.mark.parametrize(
        ("remote", "refspec"),
        [("-o", "HEAD:refs/heads/x"), ("origin", "--delete"), ("--force", "x:y")],
    )
    def test_option_like_args_rejected(self, remote: str, refspec: str) -> None:
        backend = RecordingBackend()
        with pytest.raises(WorkspaceError) as exc:
            git_push(backend, HANDLE, "repo", refspec, remote=remote)
        assert exc.value.code == WORKSPACE_INVALID
        assert backend.calls == []

    def test_push_failure_is_repo_unavailable(self) -> None:
        backend = RecordingBackend(results=[(["remote: denied"], 1)])
        with pytest.raises(WorkspaceError) as exc:
            git_push(backend, HANDLE, "repo", "HEAD:refs/heads/x")
        assert exc.value.code == REPO_UNAVAILABLE


class TestCreatePullRequest:
    REPO = "https://github.com/octo/hello"

    def test_requires_opt_in(self) -> None:
        backend = RecordingBackend()
        with pytest.raises(WorkspaceError) as exc:
            create_pull_request(backend, HANDLE, self.REPO, head="b", base="main", title="t")
        assert exc.value.code == REPO_UNAVAILABLE
        assert backend.calls == []  # fails before any sandbox exec

    def test_non_github_repo_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("SBX_GITHUB_EPHEMERAL", "1")
        monkeypatch.setenv("GH_TOKEN", GH_VALUE)
        backend = RecordingBackend()
        with pytest.raises(WorkspaceError) as exc:
            create_pull_request(
                backend,
                HANDLE,
                "https://user:SECRET@gitlab.com/o/r",
                head="b",
                base="main",
                title="t",
            )
        assert exc.value.code == WORKSPACE_INVALID
        assert "SECRET" not in str(exc.value)
        assert "<redacted>" in str(exc.value)
        assert backend.calls == []

    def test_request_shape_and_token_safety(self, monkeypatch) -> None:
        monkeypatch.setenv("SBX_GITHUB_EPHEMERAL", "1")
        monkeypatch.setenv("GH_TOKEN", GH_VALUE)
        pr = {"number": 7, "html_url": "https://github.com/octo/hello/pull/7"}
        backend = RecordingBackend(results=[([json.dumps(pr), "201"], 0)])
        out = create_pull_request(
            backend,
            HANDLE,
            "git@github.com:octo/hello.git",
            head="sbx/review",
            base="main",
            title="Add feature",
            body="by the agent",
            draft=True,
        )
        assert out == pr
        argv, env = backend.calls[0]
        assert argv[:2] == ["bash", "-c"]
        script = argv[2]
        assert "https://api.github.com/repos/octo/hello/pulls" in script
        assert '"Authorization: Bearer $GH_TOKEN"' in script
        assert GH_VALUE not in script  # the token expands in-sandbox only
        parts = shlex.split(script)
        payload = json.loads(parts[parts.index("--data") + 1])
        assert payload == {
            "title": "Add feature",
            "head": "sbx/review",
            "base": "main",
            "body": "by the agent",
            "draft": True,
        }
        assert env["GH_TOKEN"] == GH_VALUE

    def test_http_failure_is_sanitized_repo_unavailable(self, monkeypatch) -> None:
        monkeypatch.setenv("SBX_GITHUB_EPHEMERAL", "1")
        monkeypatch.setenv("GH_TOKEN", GH_VALUE)
        body = json.dumps({"message": "token ghp_0123456789abcdefXYZ denied"})
        backend = RecordingBackend(results=[([body, "403"], 0)])
        with pytest.raises(WorkspaceError) as exc:
            create_pull_request(backend, HANDLE, self.REPO, head="b", base="main", title="t")
        assert exc.value.code == REPO_UNAVAILABLE
        assert "ghp_0123456789abcdefXYZ" not in str(exc.value)
        assert "REDACTED" in str(exc.value)

    def test_exec_failure_is_repo_unavailable(self, monkeypatch) -> None:
        monkeypatch.setenv("SBX_GITHUB_EPHEMERAL", "1")
        monkeypatch.setenv("GH_TOKEN", GH_VALUE)
        backend = RecordingBackend(results=[([], 7)])
        with pytest.raises(WorkspaceError) as exc:
            create_pull_request(backend, HANDLE, self.REPO, head="b", base="main", title="t")
        assert exc.value.code == REPO_UNAVAILABLE

    def test_non_json_success_body(self, monkeypatch) -> None:
        monkeypatch.setenv("SBX_GITHUB_EPHEMERAL", "1")
        monkeypatch.setenv("GH_TOKEN", GH_VALUE)
        backend = RecordingBackend(results=[(["not json", "201"], 0)])
        with pytest.raises(WorkspaceError) as exc:
            create_pull_request(backend, HANDLE, self.REPO, head="b", base="main", title="t")
        assert exc.value.code == REPO_UNAVAILABLE


# --------------------------------------------------------------------------
# local clone/push roundtrip — the GitHub-less fallback, exercised end to end


class TestLocalRoundtrip:
    def test_clone_commit_push_file_remote(self, tmp_path: Path) -> None:
        """Public-style flow: local file remote, zero GitHub env — clone,
        commit in-sandbox, push a ref back. Proves the collaboration path
        needs no GitHub auth for non-GitHub transports."""
        backend = LocalProcessBackend()
        handle = backend.create(SandboxSpec())
        workspaces = WorkspaceService(backend, InMemoryWorkspaceStore())
        origin, base = make_repo(tmp_path)
        workspaces.prepare(
            handle, "a1", WorkspaceSpec(repo=str(origin), base_ref="main", base_sha=base)
        )
        workdir = handle.root / "repo"
        (workdir / "b.txt").write_text("two\n", encoding="utf-8")
        host_git(workdir, "add", "-A")
        res = subprocess.run(
            [
                "git",
                "-C",
                str(workdir),
                "-c",
                "user.name=sbx-test",
                "-c",
                "user.email=sbx-test@localhost",
                "commit",
                "-qm",
                "B",
            ],
            env={**os.environ, **GIT_ENV_BASE},
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        new_head = host_git(workdir, "rev-parse", "HEAD")
        assert new_head != base

        git_push(backend, handle, "repo", "HEAD:refs/heads/sbx/review")
        assert host_git(origin, "rev-parse", "refs/heads/sbx/review") == new_head

    def test_clone_error_redacts_userinfo(self) -> None:
        """A clone failure must never echo a userinfo-bearing repo URL into
        the error that lands on a run record."""
        backend = RecordingBackend(results=[([], 128)])
        workspaces = WorkspaceService(backend, InMemoryWorkspaceStore())
        url = "https://user:SECRET-TOKEN@github.example.invalid/o/r"
        with pytest.raises(WorkspaceError) as exc:
            workspaces.prepare(
                HANDLE,
                "a1",
                WorkspaceSpec(repo=url, base_ref="main", base_sha="a" * 40),
            )
        assert exc.value.code == REPO_UNAVAILABLE
        assert "SECRET-TOKEN" not in str(exc.value)
        assert "<redacted>" in str(exc.value)
        # git itself still receives the full URL — userinfo is how a caller
        # may legitimately pass a credential; only the error text is redacted.
        argv, _ = backend.calls[0]
        assert url in argv
