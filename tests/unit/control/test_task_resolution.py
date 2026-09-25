"""SOR-222/223 domain tests: canonicalization, resolve, eligibility, store."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from control import tasks
from control.capabilities import CapabilitySnapshot, ModelCapability
from control.ports import Account
from tests.fakes.fake_ports import InMemoryAccountRegistry, InMemoryScheduler

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git binary unavailable")


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(UTC)).isoformat()


def _account(
    account_id: str,
    provider: str = "codex",
    *,
    status: str = "active",
    max_concurrent: int = 1,
    models: tuple[str, ...] = ("gpt-5.6-luna",),
    secret_name: str = "sec",
    last_used_at: str | None = None,
    cooldown_until: str | None = None,
) -> Account:
    return Account(
        id=account_id,
        provider=provider,
        label=account_id,
        status=status,
        max_concurrent=max_concurrent,
        secret_name=secret_name,
        models=models,
        created_at=_iso(),
        last_used_at=last_used_at,
        cooldown_until=cooldown_until,
    )


@pytest.fixture
def registry() -> InMemoryAccountRegistry:
    return InMemoryAccountRegistry()


@pytest.fixture
def scheduler(registry: InMemoryAccountRegistry) -> InMemoryScheduler:
    return InMemoryScheduler(registry)


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[Path, str]:
    """A real local repo: (path, HEAD sha) — exercises ls-remote offline."""
    if GIT is None:
        pytest.skip("git binary unavailable")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([GIT, "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("hi")
    subprocess.run([GIT, "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [GIT, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        [GIT, "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


# ---------------------------------------------------------------------------
# canonicalize_repo
# ---------------------------------------------------------------------------


def test_canonicalize_github_https() -> None:
    repo = tasks.canonicalize_repo("https://github.com/Owner/Repo.git")
    assert repo.canonical == "https://github.com/owner/repo"
    assert repo.kind == "github"
    assert repo.slug == "owner/repo"


def test_canonicalize_github_scp_and_userinfo() -> None:
    repo = tasks.canonicalize_repo("git@github.com:Owner/Repo.git")
    assert repo.canonical == "https://github.com/owner/repo"
    repo2 = tasks.canonicalize_repo("https://user:pass@github.com/owner/repo")
    assert repo2.canonical == "https://github.com/owner/repo"
    assert "pass" not in repo2.canonical


def test_canonicalize_remote_and_local() -> None:
    remote = tasks.canonicalize_repo("https://gitlab.com/owner/repo.git")
    assert remote.kind == "remote"
    local = tasks.canonicalize_repo("file:///srv/repo")
    assert local.kind == "local"
    assert tasks.canonicalize_repo("/srv/repo").kind == "local"


def test_canonicalize_rejects_empty_and_secret_leaks() -> None:
    with pytest.raises(tasks.TaskRefusal):
        tasks.canonicalize_repo("   ")
    with pytest.raises(tasks.TaskRefusal):
        tasks.canonicalize_repo(None)


# ---------------------------------------------------------------------------
# GitLsRemoteResolver + resolve_source (real local repo, offline)
# ---------------------------------------------------------------------------


@needs_git
def test_ls_remote_resolves_default_branch_and_sha(git_repo: tuple[Path, str]) -> None:
    repo, sha = git_repo
    resolver = tasks.GitLsRemoteResolver()
    url = f"file://{repo}"
    canon = tasks.canonicalize_repo(url)
    assert resolver.default_branch(canon) == "main"
    assert resolver.resolve_ref(canon, "main") == sha
    assert resolver.resolve_ref(canon, sha) == sha
    assert resolver.resolve_ref(canon, "no-such-ref") is None


@needs_git
def test_resolve_source_defaults_to_default_branch(git_repo: tuple[Path, str]) -> None:
    repo, sha = git_repo
    res, checks, warnings = tasks.resolve_source(
        {"repo": f"file://{repo}"}, resolver=tasks.GitLsRemoteResolver(), env={}
    )
    assert res.base_ref == "main"
    assert res.base_sha == sha
    assert res.kind == "local"
    assert res.workspace() == {
        "repo": res.repo,
        "base_ref": "main",
        "base_sha": sha,
    }
    assert all(c.status == "pass" for c in checks)
    assert warnings == []


@needs_git
def test_resolve_source_named_ref_and_exact_sha(git_repo: tuple[Path, str]) -> None:
    repo, sha = git_repo
    subprocess.run([GIT, "branch", "feature"], cwd=repo, check=True, capture_output=True)
    resolver = tasks.GitLsRemoteResolver()
    res, _, _ = tasks.resolve_source(
        {"repo": f"file://{repo}", "ref": "feature"}, resolver=resolver, env={}
    )
    assert res.base_ref == "feature"
    assert res.base_sha == sha
    # Exact-sha input pins the commit verbatim.
    res2, checks2, _ = tasks.resolve_source(
        {"repo": f"file://{repo}", "ref": sha}, resolver=resolver, env={}
    )
    assert res2.base_ref == sha
    assert res2.base_sha == sha
    assert any(c.name == "source.ref" and "pinned" in c.detail for c in checks2)


@needs_git
def test_resolve_source_bad_ref_fails(git_repo: tuple[Path, str]) -> None:
    repo, _ = git_repo
    with pytest.raises(tasks.TaskRefusal) as err:
        tasks.resolve_source(
            {"repo": f"file://{repo}", "ref": "no-such-branch"},
            resolver=tasks.GitLsRemoteResolver(),
            env={},
        )
    assert err.value.code == "repo_unavailable"
    with pytest.raises(tasks.TaskRefusal) as err2:
        tasks.resolve_source(
            {"repo": f"file://{repo}", "ref": "..bad.."},
            resolver=tasks.GitLsRemoteResolver(),
            env={},
        )
    assert err2.value.code == "workspace_invalid"


@needs_git
def test_resolve_source_unreachable_repo_fails(tmp_path: Path) -> None:
    with pytest.raises(tasks.TaskRefusal) as err:
        tasks.resolve_source(
            {"repo": f"file://{tmp_path / 'missing'}"},
            resolver=tasks.GitLsRemoteResolver(),
            env={},
        )
    assert err.value.code == "repo_unavailable"


# ---------------------------------------------------------------------------
# GitHub authorization / permission preflight (stub resolver)
# ---------------------------------------------------------------------------


class _FakeResolver:
    def __init__(
        self,
        *,
        branch: str | None = "main",
        sha: str = "a" * 40,
        access: dict[str, str] | None = None,
    ) -> None:
        self._branch = branch
        self._sha = sha
        self._access = access or {"read": "yes", "push": "yes", "source": "github_api"}

    def default_branch(self, repo: tasks.CanonicalRepo) -> str | None:
        return self._branch

    def resolve_ref(self, repo: tasks.CanonicalRepo, ref: str) -> str | None:
        if ref == "missing":
            return None
        return ref if len(ref) == 40 else self._sha

    def access(self, repo: tasks.CanonicalRepo) -> dict[str, str]:
        return self._access


def test_github_read_denied_fails() -> None:
    resolver = _FakeResolver(access={"read": "no", "push": "unknown", "source": "github_api"})
    with pytest.raises(tasks.TaskRefusal) as err:
        tasks.resolve_source({"repo": "https://github.com/o/r"}, resolver=resolver, env={})
    assert err.value.code == "repo_unavailable"
    assert any(c.name == "github.read" and c.status == "fail" for c in err.value.checks)


def test_github_push_denied_fails_when_delivery_needs_push() -> None:
    resolver = _FakeResolver(access={"read": "yes", "push": "no", "source": "github_api"})
    with pytest.raises(tasks.TaskRefusal) as err:
        tasks.resolve_source(
            {"repo": "https://github.com/o/r"},
            resolver=resolver,
            env={},
            needs_push=True,
        )
    assert err.value.code == "repo_unavailable"
    assert any(c.name == "github.push" and c.status == "fail" for c in err.value.checks)


def test_github_unknown_access_warns_not_fails() -> None:
    resolver = _FakeResolver(access={"read": "unknown", "push": "unknown", "source": "github_app"})
    res, checks, warnings = tasks.resolve_source(
        {"repo": "https://github.com/o/r"},
        resolver=resolver,
        env={},
        needs_push=True,
    )
    assert res.base_sha == "a" * 40
    assert any(c.status == "warn" for c in checks)
    assert warnings


# ---------------------------------------------------------------------------
# delivery → git policy
# ---------------------------------------------------------------------------


def test_delivery_to_git_variants() -> None:
    assert tasks.delivery_to_git(None) is None
    assert tasks.delivery_to_git({}) is None
    assert tasks.delivery_to_git({"branch": "feat/x"}) == {"branch": "feat/x"}
    assert tasks.delivery_to_git({"auto_publish": True}) == {
        "push": True,
        "auto_publish": True,
    }
    assert tasks.delivery_to_git(
        {
            "branch": "feat/x",
            "pull_request": {"title": "T", "body": "B", "draft": True, "target": "main"},
        }
    ) == {
        "push": True,
        "auto_create_pr": True,
        "auto_publish": False,
        "draft": True,
        "title": "T",
        "body": "B",
        "target": "main",
        "branch": "feat/x",
    }


def test_delivery_without_source_refused(registry, scheduler) -> None:
    registry.put(_account("a1"))
    with pytest.raises(tasks.TaskRefusal) as err:
        tasks.resolve_task(
            {"prompt": {"text": "x"}, "delivery": {"branch": "b"}},
            registry=registry,
            scheduler=scheduler,
            capabilities=None,
            resolver=_FakeResolver(),
            env={},
        )
    assert err.value.code == "workspace_invalid"


# ---------------------------------------------------------------------------
# evaluate_account — the eligibility filter chain
# ---------------------------------------------------------------------------


def _eval(
    account: Account,
    registry: InMemoryAccountRegistry,
    *,
    model_req: str | None = None,
    effort_req: str | None = None,
    capabilities: object = None,
    enabled: tuple[str, ...] = ("codex",),
) -> tasks.AccountCandidate:
    registry.put(account)
    return tasks.evaluate_account(
        account,
        model_req=model_req,
        effort_req=effort_req,
        registry=registry,
        capabilities=capabilities,
        running_count=registry.running_count,
        env={},
        enabled_providers=enabled,
    )


def test_evaluate_baseline_eligible(registry) -> None:
    c = _eval(_account("a1"), registry)
    assert c.eligible
    assert c.reasons == []
    assert c.model == "gpt-5.6-luna"


def test_evaluate_provider_disabled_and_unsupported(registry) -> None:
    c = _eval(_account("a1", "grok"), registry)
    assert not c.eligible and "provider_disabled" in c.reasons
    c2 = _eval(_account("a2", "nope"), registry)
    assert "provider_unsupported" in c2.reasons


def test_evaluate_auth_health_capacity(registry) -> None:
    no_cred = _eval(_account("a1", secret_name=""), registry)
    assert not no_cred.eligible and "no_credential" in no_cred.reasons

    registry.put_credential_blob("a2", {"provider": "codex", "files": {".codex/auth.json": "{}"}})
    blobbed = _eval(_account("a2", secret_name=""), registry)
    assert blobbed.eligible

    busy = _eval(_account("a3", max_concurrent=1), registry)
    registry.set_running("a3", 1)
    busy = _eval(registry.get("a3"), registry)
    assert "at_capacity" in busy.reasons

    future = _iso(datetime.now(UTC) + timedelta(hours=1))
    cooling = _eval(
        _account("a4", status="cooling", cooldown_until=future),
        registry,
    )
    assert "status_cooling" in cooling.reasons

    # Cooling that already expired normalizes back to active.
    past = _iso(datetime.now(UTC) - timedelta(hours=1))
    recovered = _eval(
        _account("a5", status="cooling", cooldown_until=past),
        registry,
    )
    assert recovered.eligible and recovered.status == "active"


def test_evaluate_model_and_effort_capability(registry) -> None:
    row = ModelCapability(
        model="m1",
        display_name="m1",
        family="f",
        aliases=("alias1",),
        reasoning_efforts=("low", "high"),
        effort_native={},
        default_effort="low",
    )
    snap = CapabilitySnapshot(
        provider="codex",
        account_id="a1",
        models=(row,),
        source="discovered",
        refreshed_at=_iso(),
        stale=False,
        default_model="m1",
    )
    caps = type("Caps", (), {"get": lambda self, account, ensure=False: snap})()

    ok = _eval(_account("a1"), registry, model_req="alias1", effort_req="high", capabilities=caps)
    assert ok.eligible

    bad_model = _eval(_account("a2"), registry, model_req="other", capabilities=caps)
    assert "model_not_advertised" in bad_model.reasons

    bad_effort = _eval(
        _account("a3"), registry, model_req="m1", effort_req="max", capabilities=caps
    )
    assert "effort_unsupported" in bad_effort.reasons

    # No effort surface on the advertised model → effort_no_surface.
    row2 = ModelCapability(
        model="m2",
        display_name="m2",
        family="f",
        aliases=(),
        reasoning_efforts=(),
        effort_native={},
        default_effort=None,
    )
    snap2 = CapabilitySnapshot(
        provider="codex",
        account_id="a4",
        models=(row2,),
        source="discovered",
        refreshed_at=_iso(),
        stale=False,
        default_model="m2",
    )
    caps2 = type("Caps", (), {"get": lambda self, account, ensure=False: snap2})()
    no_surface = _eval(
        _account("a4"), registry, model_req="m2", effort_req="low", capabilities=caps2
    )
    assert "effort_no_surface" in no_surface.reasons


# ---------------------------------------------------------------------------
# resolve_execution — auto provider/model/effort/account + evidence
# ---------------------------------------------------------------------------


def test_resolve_execution_auto_lru(registry, scheduler) -> None:
    registry.put(_account("old", last_used_at="2024-01-01T00:00:00+00:00"))
    registry.put(_account("fresh", last_used_at="2026-01-01T00:00:00+00:00"))
    registry.put(_account("never"))
    res = tasks.resolve_execution(
        None, registry=registry, scheduler=scheduler, capabilities=None, env={}
    )
    assert res.account_id == "never"  # never-used wins the LRU key
    assert res.evidence["account_id"]["source"] == "lru"
    assert res.evidence["provider"]["resolved"] == "codex"
    assert res.evidence["model"]["resolved"] == "gpt-5.6-luna"
    assert len(res.candidates) == 3


def test_resolve_execution_named_account(registry, scheduler) -> None:
    registry.put(_account("a1"))
    res = tasks.resolve_execution(
        {"account_id": "a1", "provider": "codex"},
        registry=registry,
        scheduler=scheduler,
        capabilities=None,
        env={},
    )
    assert res.account_id == "a1"
    assert res.evidence["account_id"]["source"] == "requested"

    registry.put(_account("g1", provider="grok"))
    with pytest.raises(tasks.TaskRefusal) as err:
        tasks.resolve_execution(
            {"account_id": "g1", "provider": "codex"},
            registry=registry,
            scheduler=scheduler,
            capabilities=None,
            env={},
        )
    assert err.value.code == "account_unavailable"

    with pytest.raises(tasks.TaskRefusal) as err2:
        tasks.resolve_execution(
            {"account_id": "missing"},
            registry=registry,
            scheduler=scheduler,
            capabilities=None,
            env={},
        )
    assert err2.value.code == "account_unavailable"


def test_resolve_execution_provider_validation(registry, scheduler) -> None:
    registry.put(_account("a1"))
    with pytest.raises(tasks.TaskRefusal) as err:
        tasks.resolve_execution(
            {"provider": "notaprovider"},
            registry=registry,
            scheduler=scheduler,
            capabilities=None,
            env={},
        )
    assert err.value.code == "invalid_provider" and err.value.status_code == 400
    with pytest.raises(tasks.TaskRefusal) as err2:
        tasks.resolve_execution(
            {"provider": "grok"},  # canonical but not enabled (env default: codex)
            registry=registry,
            scheduler=scheduler,
            capabilities=None,
            env={},
        )
    assert err2.value.code == "invalid_provider"


def test_resolve_execution_exhaustion_vs_unsupported(registry, scheduler) -> None:
    # Capability-only refusals → 400 unsupported (retrying won't help).
    registry.put(_account("a1", models=("x1",)))
    caps_snap = CapabilitySnapshot(
        provider="codex",
        account_id="a1",
        models=(
            ModelCapability(
                model="x1",
                display_name="x1",
                family="f",
                aliases=(),
                reasoning_efforts=("low",),
                effort_native={},
                default_effort="low",
            ),
        ),
        source="discovered",
        refreshed_at=_iso(),
        stale=False,
        default_model="x1",
    )
    caps = type("Caps", (), {"get": lambda self, account, ensure=False: caps_snap})()
    with pytest.raises(tasks.TaskRefusal) as err:
        tasks.resolve_execution(
            {"model": "ghost-model"},
            registry=registry,
            scheduler=scheduler,
            capabilities=caps,
            env={},
        )
    assert err.value.code == "unsupported" and err.value.status_code == 400

    # Capacity refusals → 429 provider_exhausted.
    registry2 = InMemoryAccountRegistry()
    registry2.put(_account("b1", max_concurrent=1))
    registry2.set_running("b1", 1)
    with pytest.raises(tasks.TaskRefusal) as err2:
        tasks.resolve_execution(
            None,
            registry=registry2,
            scheduler=InMemoryScheduler(registry2),
            capabilities=None,
            env={},
        )
    assert err2.value.code == "provider_exhausted" and err2.value.status_code == 429


# ---------------------------------------------------------------------------
# resolve_task (end-to-end domain) + store round trips
# ---------------------------------------------------------------------------


@needs_git
def test_resolve_task_full(git_repo, registry, scheduler) -> None:
    repo, sha = git_repo
    registry.put(_account("a1"))
    res = tasks.resolve_task(
        {
            "prompt": {"text": "do it"},
            "source": {"repo": f"file://{repo}"},
            "delivery": {"pull_request": {"title": "T"}},
        },
        registry=registry,
        scheduler=scheduler,
        capabilities=None,
        resolver=tasks.GitLsRemoteResolver(),
        env={},
    )
    assert res.source.base_sha == sha
    assert res.git["auto_create_pr"] is True
    assert res.git["push"] is True
    payload = res.resolved_payload()
    assert payload["source"]["base_sha"] == sha
    assert payload["execution"]["provider"] == "codex"
    pub = res.public(ok=True)
    assert pub["ok"] is True
    assert all(c["status"] == "pass" for c in pub["checks"])


def test_task_store_roundtrips(tmp_path: Path) -> None:
    for store in (
        tasks.InMemoryTaskStore(),
        tasks.FileTaskStore(tmp_path / "tasks"),
    ):
        rec = tasks.TaskRecord(
            id=tasks.new_task_id(),
            owner="key_1",
            status="queued",
            request={"prompt": {"text": "x"}},
            resolved={"execution": {"provider": "codex"}},
            agent_id="agent-1",
            run_id="run-1",
            created_at=_iso(),
            updated_at=_iso(),
            response={"task": {"id": "t"}},
            idempotency={"key_id": "key_1", "key": "idem-1", "fingerprint": "fp"},
        )
        store.put(rec)
        got = store.get(rec.id)
        assert got is not None and got.agent_id == "agent-1" and got.owner == "key_1"
        assert got.idempotency["key"] == "idem-1"
        assert [t.id for t in store.list("key_1")] == [rec.id]
        assert store.list("key_2") == []
        bound = store.find_by_idempotency("key_1", "idem-1")
        assert bound is not None and bound.id == rec.id
        assert store.find_by_idempotency("key_1", "nope") is None
        assert store.get("nope") is None
