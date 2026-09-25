"""SOR-222/223: public Task API — resolution, preflight and persistence.

The task layer is the caller-facing resource on top of the existing agent
machinery: a caller declares ``prompt`` + ``source`` + ``execution`` +
``delivery`` and the control plane resolves that declaration into a concrete
``CreateAgentRequest`` — a canonical repo URL, a ``base_ref``/``base_sha``
pin, and a concrete provider/model/effort/account plan. ``requested`` is
persisted verbatim next to ``resolved`` so the gap between intent and the
authoritative plan stays inspectable.

Resolution is split into two deliberately reusable pieces:

- source resolution (``resolve_source``): canonicalize the repo URL, pick
  the default ref when none is declared, resolve the base ref to an exact
  commit sha, and run the GitHub authorization/permission preflight.
- execution resolution (``resolve_execution``): capability-aware account
  scheduling — every registered account is filtered by provider /
  runtime / model / reasoning-effort / auth material / health / capacity
  before the LRU pick, so ``auto`` is a real selection with recorded
  evidence, not a scheduler coin flip.

``POST /v1/tasks/preflight`` runs this code path advisory-only (no slot is
reserved); ``POST /v1/tasks`` re-runs it and then reserves through the
authoritative ``Scheduler.acquire`` in ``_create_agent_once`` — a stale
advisory answer can never create on a dead account.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from control.accounts import cooldown_expired
from control.ports import Account, AccountRegistry

# Canonical provider set — same tuple the scheduler/adapter registry use.
CANONICAL_PROVIDERS: tuple[str, ...] = ("codex", "antigravity", "grok", "opencode", "devin")

TASKS_DICT_NAME = "sbx-tasks"
TASKS_DICT_ENV = "SBX_TASKS_DICT"
TASK_STORE_DIR_ENV = "SBX_TASK_STORE_DIR"

_LS_REMOTE_TIMEOUT_S = 30.0
_GITHUB_API_TIMEOUT_S = 15.0
_GITHUB_API_URL_ENV = "SBX_GITHUB_API_URL"
_GITHUB_API_DEFAULT = "https://api.github.com"

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Tri-state permission answers: evidence must distinguish "denied" from
# "could not determine" — a caller should see *why* the check is uncertain.
_PERMISSION_TRISTATE = ("yes", "no", "unknown")

# GitHub object permissions that count as push-capable.
_GITHUB_WRITE_PERMISSIONS = ("push", "maintain", "admin")


class TaskRefusal(Exception):
    """Resolution failure carrying a canonical ``{error:{code}}`` mapping."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
        checks: Sequence[Check] = (),
        candidates: Sequence[AccountCandidate] = (),
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.checks = list(checks)
        self.candidates = list(candidates)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Check:
    """One advisory preflight check line (name + pass/warn/fail + detail)."""

    name: str
    status: str  # "pass" | "warn" | "fail"
    detail: str

    def public(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


# --------------------------------------------------------------------------
# repo canonicalization
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalRepo:
    """Normalized repo address + what it implies for resolution."""

    canonical: str
    kind: str  # "github" | "remote" | "local"
    slug: str | None = None  # owner/repo — github kind only


def canonicalize_repo(repo: Any) -> CanonicalRepo:
    """Normalize a caller-supplied repo address for persistence + probing.

    github.com https/ssh forms collapse to the canonical https URL;
    userinfo is always stripped — credential material must never persist
    on the task record. Everything else (other hosts, ``file://``, plain
    paths) is kept verbatim so ``git clone`` semantics are unchanged.
    """
    if not isinstance(repo, str) or not repo.strip():
        raise TaskRefusal(400, "workspace_invalid", "source.repo must be a non-empty string")
    raw = repo.strip()
    # Strip any userinfo segment — it is never persisted.
    scheme_at = raw.find("://")
    candidate = raw
    if scheme_at >= 0:
        at = candidate.find("@", scheme_at + 3)
        slash = candidate.find("/", scheme_at + 3)
        if at >= 0 and (slash < 0 or at < slash):
            candidate = f"{candidate[: scheme_at + 3]}{candidate[at + 1 :]}"
    from control import github

    slug = github.repo_slug(raw) or github.repo_slug(candidate)
    if slug is not None:
        # GitHub owner/repo names are case-insensitive — fold to lower.
        slug = slug.lower()
        return CanonicalRepo(canonical=f"https://github.com/{slug}", kind="github", slug=slug)
    if candidate.startswith("file://"):
        return CanonicalRepo(canonical=candidate, kind="local")
    if candidate.startswith("/") or candidate.startswith("."):
        return CanonicalRepo(canonical=candidate, kind="local")
    if "://" in candidate or ":" in candidate:
        # Other git remotes (gitlab, bitbucket, git://, ssh git@host): kept
        # verbatim — clone semantics and auth are the operator's concern.
        return CanonicalRepo(canonical=candidate, kind="remote")
    return CanonicalRepo(canonical=candidate, kind="local")


# --------------------------------------------------------------------------
# repo probing (default branch / exact sha / access posture)
# --------------------------------------------------------------------------


@runtime_checkable
class RepoResolver(Protocol):
    """Host-side remote probe: resolves refs and access posture.

    Implementations must never surface credential material — tokens exist
    only inside subprocess env / request headers. ``None`` answers mean
    "could not determine", never "defaulted".
    """

    def default_branch(self, repo: CanonicalRepo) -> str | None:
        """The remote's HEAD ref name, or ``None`` when undetermined."""

    def resolve_ref(self, repo: CanonicalRepo, ref: str) -> str | None:
        """Exact commit sha for ``ref`` (branch/tag/sha/HEAD), or ``None``."""

    def access(self, repo: CanonicalRepo) -> dict[str, str]:
        """``{"read": yes|no|unknown, "push": yes|no|unknown, "source": ...}``."""


def _git_env(env: Mapping[str, str]) -> dict[str, str]:
    """Subprocess env for host-side git: no prompts, no credential bleed."""
    out = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": env.get("HOME") or os.environ.get("HOME") or "",
        "PATH": env.get("PATH") or os.environ.get("PATH") or os.defpath,
    }
    # Forward proxy settings only — the git bridge owns credential envs and
    # they are deliberately not copied into the probe's env.
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        if env.get(key):
            out[key] = env[key]  # type: ignore[index]
    return out


class GitLsRemoteResolver:
    """``git ls-remote`` probe — the generic path for any reachable remote.

    Works for github.com too (fallback when the API seam is unavailable)
    and for ``file://``/local-path repos, which keeps it usable in unit
    tests with a bare on-disk repo and no network at all.
    """

    def __init__(
        self,
        *,
        git: str = "git",
        timeout_s: float = _LS_REMOTE_TIMEOUT_S,
        env: Mapping[str, str] | None = None,
        runner: Any = subprocess.run,
    ) -> None:
        self._git = git
        self._timeout_s = timeout_s
        self._env = dict(env or os.environ)
        self._runner = runner

    def _ls_remote(self, url: str, *args: str) -> tuple[int, list[str]]:
        try:
            proc = self._runner(
                [self._git, "ls-remote", *args, url],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                env=_git_env(self._env),
            )
        except (OSError, subprocess.TimeoutExpired):
            return -1, []
        # stderr is deliberately never surfaced — it may echo the URL.
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        return proc.returncode, lines

    @staticmethod
    def _ref_table(lines: list[str]) -> dict[str, str]:
        """``sha<TAB>refname`` lines → refname → sha (peeled ``^{}`` skipped)."""
        table: dict[str, str] = {}
        for line in lines:
            sha, _, name = line.partition("\t")
            sha = sha.strip()
            name = name.strip()
            if not sha or not name or name.endswith("^{}"):
                continue
            table[name] = sha
        return table

    def default_branch(self, repo: CanonicalRepo) -> str | None:
        code, lines = self._ls_remote(repo.canonical, "--symref")
        if code != 0:
            return None
        for line in lines:
            if line.startswith("ref:") and "\t" in line:
                ref = line[4:].split("\t", 1)[0].strip()
                if ref.startswith("refs/heads/"):
                    return ref[len("refs/heads/") :]
        return None

    def resolve_ref(self, repo: CanonicalRepo, ref: str) -> str | None:
        if _COMMIT_SHA_RE.fullmatch(ref):
            # A bare sha needs containment proof: scan the remote's refs.
            code, lines = self._ls_remote(repo.canonical)
            if code != 0:
                return None
            shas = {ln.split("\t", 1)[0].strip() for ln in lines}
            return ref if ref in shas else None
        # Named ref: prefer heads, then tags, then the verbatim name, HEAD.
        code, lines = self._ls_remote(repo.canonical)
        if code != 0:
            return None
        table = self._ref_table(lines)
        for candidate in (
            f"refs/heads/{ref}",
            f"refs/tags/{ref}",
            ref,
            ("HEAD" if ref == "HEAD" else f"refs/remotes/{ref}"),
        ):
            sha = table.get(candidate)
            if sha and _COMMIT_SHA_RE.fullmatch(sha):
                return sha
        return None

    def access(self, repo: CanonicalRepo) -> dict[str, str]:
        code, _ = self._ls_remote(repo.canonical)
        # ls-remote can only prove read reachability; push is never probed
        # (a push probe would mutate the remote).
        if code == 0:
            return {"read": "yes", "push": "unknown", "source": "ls_remote"}
        return {"read": "unknown", "push": "unknown", "source": "ls_remote"}


class GitHubApiResolver:
    """github.com REST probe: repo metadata, ref resolution, permissions.

    Auth uses the same sources as the sandbox injection seam — env
    ``GH_TOKEN``/``GITHUB_TOKEN`` first, else a repo-scoped GitHub App
    installation token (minted lazily and only when an installation
    actually authorizes the repo). Unauthenticated calls still resolve
    public repos; a 404 without a token is reported ``unknown``, not
    ``no`` — a private repo reads as absent without credentials.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: float = _GITHUB_API_TIMEOUT_S,
        client: Any = None,
    ) -> None:
        self._env = dict(env or os.environ)
        self._timeout_s = timeout_s
        self._client = client

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        base = (
            self._env.get(_GITHUB_API_URL_ENV)
            or self._env.get("SBX_GITHUB_APP_API_URL")
            or _GITHUB_API_DEFAULT
        )
        return httpx.Client(base_url=base, timeout=self._timeout_s)

    def _token(self, repo: CanonicalRepo) -> tuple[str | None, str | None]:
        """(token, source-name) — value never leaves this module."""
        from control import github

        token = github.resolve_token(self._env)
        if token is not None:
            return token, "env"
        if repo.slug:
            from control import github_app

            try:
                token = github_app.sandbox_token(self._env, repo=repo.slug)
            except Exception:
                token = None
            if token is not None:
                return token, "github_app"
        return None, None

    def _get(self, path: str, token: str | None) -> tuple[int, Any]:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        client = self._http()
        try:
            resp = client.get(path, headers=headers)
            try:
                body = resp.json()
            except Exception:
                body = None
            return resp.status_code, body
        except Exception:
            return -1, None

    def _repo(self, repo: CanonicalRepo) -> tuple[int, dict[str, Any] | None, str | None]:
        token, _source = self._token(repo)
        status, body = self._get(f"/repos/{repo.slug}", token)
        return status, body if isinstance(body, dict) else None, token

    def default_branch(self, repo: CanonicalRepo) -> str | None:
        if repo.slug is None:
            return None
        status, body, _ = self._repo(repo)
        if status == 200 and body:
            branch = body.get("default_branch")
            return branch if isinstance(branch, str) and branch else None
        return None

    def resolve_ref(self, repo: CanonicalRepo, ref: str) -> str | None:
        if repo.slug is None:
            return None
        token, _ = self._token(repo)
        status, body = self._get(f"/repos/{repo.slug}/commits/{ref}", token)
        if status == 200 and isinstance(body, dict):
            sha = body.get("sha")
            if isinstance(sha, str) and _COMMIT_SHA_RE.fullmatch(sha):
                return sha
        return None

    def access(self, repo: CanonicalRepo) -> dict[str, str]:
        if repo.slug is None:
            return {"read": "unknown", "push": "unknown", "source": "github_api"}
        token, source = self._token(repo)
        status, body = self._get(f"/repos/{repo.slug}", token)
        if status == 200 and isinstance(body, dict):
            perms = body.get("permissions") if isinstance(body.get("permissions"), dict) else {}
            can_push = any(bool(perms.get(p)) for p in _GITHUB_WRITE_PERMISSIONS)
            push = "yes" if can_push else ("no" if perms else "unknown")
            return {
                "read": "yes",
                "push": push,
                "source": f"github_api:{source or 'anonymous'}",
            }
        if status == 404:
            # Without a token a private repo is indistinguishable from a
            # missing one — report unknown, never a false "no".
            return {
                "read": "unknown" if token is None else "no",
                "push": "unknown",
                "source": f"github_api:{source or 'anonymous'}",
            }
        return {
            "read": "unknown",
            "push": "unknown",
            "source": f"github_api:{source or 'anonymous'}",
        }


class ChainRepoResolver:
    """github.com → GitHub API first (permissions), ls-remote fallback;
    every other remote goes straight to ``git ls-remote``."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        api: GitHubApiResolver | None = None,
        git: GitLsRemoteResolver | None = None,
    ) -> None:
        env = env if env is not None else os.environ
        self._api = api or GitHubApiResolver(env=env)
        self._git = git or GitLsRemoteResolver(env=env)

    def default_branch(self, repo: CanonicalRepo) -> str | None:
        if repo.kind == "github":
            branch = self._api.default_branch(repo)
            if branch is not None:
                return branch
        return self._git.default_branch(repo)

    def resolve_ref(self, repo: CanonicalRepo, ref: str) -> str | None:
        if repo.kind == "github":
            sha = self._api.resolve_ref(repo, ref)
            if sha is not None:
                return sha
        return self._git.resolve_ref(repo, ref)

    def access(self, repo: CanonicalRepo) -> dict[str, str]:
        if repo.kind == "github":
            access = self._api.access(repo)
            if access["read"] != "unknown":
                return access
            git_access = self._git.access(repo)
            git_access["source"] = f"{access['source']}+{git_access['source']}"
            return git_access
        return self._git.access(repo)


def default_repo_resolver(env: Mapping[str, str] | None = None) -> ChainRepoResolver:
    """Production default: GitHub API → ls-remote chain."""
    return ChainRepoResolver(env=env)


# --------------------------------------------------------------------------
# source resolution — canonical repo, default ref, exact sha, permissions
# --------------------------------------------------------------------------


@dataclass
class SourceResolution:
    """Resolved ``source`` declaration — inputs for the workspace spec."""

    repo: str  # canonical, userinfo-stripped
    kind: str
    base_ref: str
    base_sha: str
    slug: str | None
    access: dict[str, str]

    def workspace(self) -> dict[str, str]:
        return {"repo": self.repo, "base_ref": self.base_ref, "base_sha": self.base_sha}

    def evidence(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "kind": self.kind,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "slug": self.slug,
            "access": dict(self.access),
        }


def resolve_source(
    source: Mapping[str, Any],
    *,
    resolver: RepoResolver,
    env: Mapping[str, str] | None = None,
    needs_push: bool = False,
) -> tuple[SourceResolution, list[Check], list[str]]:
    """Resolve a ``source`` block to a pinned ``base_ref``/``base_sha``.

    Raises ``TaskRefusal`` on hard failures (unreachable repo, unresolvable
    ref); permission gaps the caller can still proceed past land as
    ``warn`` checks — a read-accessible repo with unknown push rights is a
    valid task, only ``delivery`` escalation would need push.
    """
    env = os.environ if env is None else env
    checks: list[Check] = []
    warnings: list[str] = []
    repo = canonicalize_repo(source.get("repo"))
    checks.append(
        Check("source.repo", "pass", f"repo canonicalized as {repo.canonical!r} ({repo.kind})")
    )
    ref = source.get("ref")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        raise TaskRefusal(
            400, "workspace_invalid", "source.ref must be a non-empty string", checks=checks
        )
    ref = ref.strip() if isinstance(ref, str) else None

    from control import github
    from control.workspace import is_commit_sha, is_safe_ref

    # -- base ref -----------------------------------------------------------
    if ref is None or ref in ("", "auto", "HEAD"):
        branch = resolver.default_branch(repo)
        if branch is None:
            checks.append(
                Check("source.ref", "fail", "could not determine the repo's default branch")
            )
            raise TaskRefusal(
                409,
                "repo_unavailable",
                f"cannot resolve the default branch of {repo.canonical!r}",
                checks=checks,
            )
        base_ref = branch
        checks.append(Check("source.ref", "pass", f"default branch resolved to {base_ref!r}"))
    elif is_commit_sha(ref):
        # Exact-sha input: the sha itself is the ref — ``git rev-parse
        # <sha>^{commit}`` in the fresh clone is the authoritative check
        # (``resolve_ref`` may still verify existence when the probe can).
        base_ref = ref
        checks.append(Check("source.ref", "pass", f"exact commit pinned at {ref[:12]}"))
    else:
        if not is_safe_ref(ref):
            raise TaskRefusal(
                400,
                "workspace_invalid",
                f"source.ref is not a safe git ref name: {ref!r}",
                checks=checks,
            )
        base_ref = ref
        checks.append(Check("source.ref", "pass", f"ref {base_ref!r} declared"))

    # -- exact sha -----------------------------------------------------------
    base_sha = resolver.resolve_ref(repo, base_ref)
    if base_sha is None:
        if is_commit_sha(base_ref):
            # Exact-sha input stays advisory-unverifiable when the probe
            # cannot see it (private remote without credentials, probe
            # offline); the worker's rev-parse in the fresh clone is the
            # authoritative gate and fails closed if the sha is absent.
            checks.append(
                Check(
                    "source.sha",
                    "warn",
                    f"commit {base_ref[:12]} not advertised by {repo.canonical!r} "
                    "— verified again at run time",
                )
            )
            base_sha = base_ref
            warnings.append(f"base sha {base_ref[:12]} could not be verified remotely")
        else:
            detail = f"ref {base_ref!r} does not resolve to a commit on {repo.canonical!r}"
            checks.append(Check("source.sha", "fail", detail))
            raise TaskRefusal(409, "repo_unavailable", detail, checks=checks)
    else:
        checks.append(Check("source.sha", "pass", f"{base_ref!r} resolves to {base_sha[:12]}"))

    # -- authorization / permission preflight --------------------------------
    access: dict[str, str]
    if repo.kind == "github":
        access = resolver.access(repo)
        source_name = github.token_source(env, repo=repo.slug)
        if access["read"] == "no":
            checks.append(
                Check(
                    "github.read",
                    "fail",
                    f"no credential authorizes read on {repo.slug!r}",
                )
            )
            raise TaskRefusal(
                409,
                "repo_unavailable",
                f"repo {repo.slug!r} is not readable with the configured GitHub credentials",
                checks=checks,
            )
        if access["read"] == "unknown":
            checks.append(
                Check(
                    "github.read",
                    "warn",
                    f"read access on {repo.slug!r} could not be verified "
                    f"(credential source: {source_name or 'none'})",
                )
            )
            warnings.append(f"read access on {repo.slug!r} unverified")
        else:
            checks.append(
                Check(
                    "github.read",
                    "pass",
                    f"{repo.slug!r} readable via {access['source']}",
                )
            )
        if needs_push:
            if access["push"] == "no":
                checks.append(
                    Check(
                        "github.push",
                        "fail",
                        f"credentials lack push permission on {repo.slug!r}",
                    )
                )
                raise TaskRefusal(
                    409,
                    "repo_unavailable",
                    f"repo {repo.slug!r} is not pushable with the configured GitHub credentials",
                    checks=checks,
                )
            if access["push"] == "unknown":
                checks.append(
                    Check(
                        "github.push",
                        "warn",
                        f"push permission on {repo.slug!r} unverified"
                        + ("" if source_name else " — no GitHub credential is configured"),
                    )
                )
                warnings.append(f"push permission on {repo.slug!r} unverified")
            else:
                checks.append(
                    Check("github.push", "pass", f"{repo.slug!r} pushable via {access['source']}")
                )
        if source_name is None and repo.kind == "github":
            checks.append(
                Check(
                    "github.credential",
                    "warn",
                    "no GitHub credential configured — private repos will fail at clone",
                )
            )
            warnings.append("no GitHub credential configured")
    elif repo.kind == "remote":
        access = resolver.access(repo)
        if access["read"] == "unknown":
            checks.append(
                Check("repo.read", "warn", f"{repo.canonical!r} unreachable via git ls-remote")
            )
            warnings.append(f"{repo.canonical!r} could not be probed")
        else:
            checks.append(Check("repo.read", "pass", f"{repo.canonical!r} reachable"))
        if needs_push:
            checks.append(
                Check("repo.push", "warn", "push permission is not verifiable before run time")
            )
            warnings.append("push permission unverified")
    else:
        access = {"read": "yes", "push": "unknown", "source": "local"}
        checks.append(Check("repo.read", "pass", "local repo path — host permissions apply"))

    return (
        SourceResolution(
            repo=repo.canonical,
            kind=repo.kind,
            base_ref=base_ref,
            base_sha=base_sha,
            slug=repo.slug,
            access=access,
        ),
        checks,
        warnings,
    )


# --------------------------------------------------------------------------
# delivery → git policy
# --------------------------------------------------------------------------


def delivery_to_git(delivery: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Task ``delivery`` block → the SOR-128 ``git`` policy dict.

    ``pull_request`` implies push + auto_create_pr; ``auto_publish``
    implies push. ``branch`` alone just names the work branch — publish
    stays manual. ``target`` defaults to the resolved ``base_ref`` inside
    ``normalize_git_policy``.
    """
    if not delivery:
        return None
    branch = delivery.get("branch")
    pr = delivery.get("pull_request")
    auto_publish = bool(delivery.get("auto_publish"))
    if pr:
        git: dict[str, Any] = {
            "push": True,
            "auto_create_pr": True,
            "auto_publish": auto_publish,
            "draft": bool(pr.get("draft")),
        }
        if pr.get("title"):
            git["title"] = pr["title"]
        if pr.get("body"):
            git["body"] = pr["body"]
        if pr.get("target"):
            git["target"] = pr["target"]
        if branch:
            git["branch"] = branch
        return git
    if auto_publish:
        git = {"push": True, "auto_publish": True}
        if branch:
            git["branch"] = branch
        return git
    if branch:
        return {"branch": branch}
    return None


# --------------------------------------------------------------------------
# execution resolution — capability-aware account scheduling
# --------------------------------------------------------------------------


def _ambient_credential(provider: str, account_id: str, env: Mapping[str, str]) -> bool:
    """Whether ambient control env can seed this account's credential."""
    raw = env.get("SBX_ACCOUNT_CREDENTIAL")
    if raw:
        try:
            blob = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            blob = None
        if isinstance(blob, dict) and blob.get("provider") == provider:
            ambient = env.get("SBX_ACCOUNT_ID")
            if ambient is None or ambient == account_id:
                return True
    if provider == "codex" and env.get("CODEX_AUTH_JSON"):
        return True
    return False


def has_auth_material(
    account: Account, registry: AccountRegistry | None, env: Mapping[str, str]
) -> bool:
    """Account-level auth check: managed Secret, stored blob, or ambient.

    Mirrors the three credential channels ``sandbox_env`` can attach at
    provision time — an account with none of them can only fail
    ``auth_invalid`` mid-run, so it is never an eligible ``auto`` pick.
    """
    if account.secret_name:
        return True
    if registry is not None:
        get_blob = getattr(registry, "get_credential_blob", None)
        if callable(get_blob):
            try:
                if get_blob(account.id):
                    return True
            except Exception:
                pass
    return _ambient_credential(account.provider, account.id, env)


@dataclass
class AccountCandidate:
    """Per-account eligibility verdict + the account's own resolved plan."""

    account_id: str
    provider: str
    status: str
    running: int
    max_concurrent: int
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    model: str | None = None
    model_source: str | None = None  # discovered|declared|default
    effort: str | None = None
    effort_source: str | None = None
    snapshot_source: str | None = None
    snapshot_stale: bool = False
    last_used_at: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "provider": self.provider,
            "status": self.status,
            "running": self.running,
            "max_concurrent": self.max_concurrent,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "model": self.model,
            "reasoning_effort": self.effort,
        }


def _snapshot_for(capabilities: Any, account: Account) -> Any:
    """Catalog snapshot without spawning probes (``ensure=False``)."""
    from control.capabilities import declared_snapshot

    if capabilities is None or not callable(getattr(capabilities, "get", None)):
        return declared_snapshot(account)
    try:
        return capabilities.get(account, ensure=False)
    except TypeError:
        return capabilities.get(account)
    except Exception:
        return declared_snapshot(account)


def _model_row(snapshot: Any, model: str | None) -> Any:
    if snapshot is None or model is None:
        return None
    for row in snapshot.models:
        if row.model == model or model in row.aliases:
            return row
    return None


def _effort_row(provider: str, model: str | None, snapshot: Any) -> Any:
    """Same row semantics as the /v1 create path's ``_effort_row``."""
    from control.capabilities import capability_from_model_id

    row = _model_row(snapshot, model) if snapshot is not None else None
    if row is None and model is not None and (snapshot is None or snapshot.source != "discovered"):
        row = capability_from_model_id(provider, model)
    return row


def _account_default_model(
    provider: str, account: Account, snapshot: Any
) -> tuple[str | None, str]:
    """Model for ``auto``: catalog default → declared → provider floor."""
    from control.api_v1.bootstrap import PROVIDER_DEFAULT_MODELS

    if snapshot is not None and snapshot.default_model:
        return snapshot.default_model, snapshot.source
    if account.models:
        return account.models[0], "declared"
    defaults = PROVIDER_DEFAULT_MODELS.get(provider) or ()
    return (defaults[0], "default") if defaults else (None, "default")


def evaluate_account(
    account: Account,
    *,
    model_req: str | None,
    effort_req: str | None,
    registry: AccountRegistry | None,
    capabilities: Any,
    running_count: Callable[[str], int],
    env: Mapping[str, str],
    enabled_providers: Sequence[str],
    now: datetime | None = None,
) -> AccountCandidate:
    """Filter one account through the eligibility chain.

    Order mirrors the issue's filter: provider → runtime → auth → health →
    capacity → capability (model/effort). ``reasons`` accumulates every
    disqualifier, not just the first — a candidate's evidence should say
    everything that kept it out.
    """
    from control.onboarding import provider_auth_argv, provider_models_argv

    now = now or datetime.now(UTC)
    reasons: list[str] = []
    running = 0
    try:
        running = int(running_count(account.id)) if callable(running_count) else 0
    except Exception:
        running = 0

    if account.provider not in CANONICAL_PROVIDERS:
        reasons.append("provider_unsupported")
    elif account.provider not in enabled_providers:
        reasons.append("provider_disabled")
    if (
        provider_models_argv(account.provider) is None
        and provider_auth_argv(account.provider) is None
    ):
        reasons.append("runtime_unavailable")

    status = account.status
    if status == "cooling" and cooldown_expired(account, now):
        status = "active"
    if status != "active":
        reasons.append(f"status_{status}")

    if not has_auth_material(account, registry, env):
        reasons.append("no_credential")

    cap = account.max_concurrent if account.max_concurrent > 0 else 1
    if running >= cap:
        reasons.append("at_capacity")

    snapshot = _snapshot_for(capabilities, account)
    snapshot_source = snapshot.source if snapshot is not None else None
    snapshot_stale = bool(getattr(snapshot, "stale", False))

    # -- model / effort ------------------------------------------------------
    model: str | None = None
    model_source: str | None = None
    effort: str | None = None
    effort_source: str | None = None
    if model_req is not None:
        model = model_req
        model_source = "requested"
        if (
            snapshot is not None
            and snapshot.source == "discovered"
            and _model_row(snapshot, model_req) is None
        ):
            reasons.append("model_not_advertised")
            model_source = None
    else:
        model, model_source = _account_default_model(account.provider, account, snapshot)
        if model is None:
            reasons.append("model_unresolvable")

    if effort_req is not None:
        effort = effort_req
        effort_source = "requested"
        row = _effort_row(account.provider, model, snapshot)
        if row is not None:
            if not row.reasoning_efforts:
                reasons.append("effort_no_surface")
            elif effort_req not in row.reasoning_efforts:
                reasons.append("effort_unsupported")
        elif snapshot is not None and snapshot.source == "discovered":
            reasons.append("effort_unsupported")
        else:
            from runtime.runner.effort import effort_error

            if effort_error(account.provider, effort_req):
                reasons.append("effort_unsupported")
    else:
        row = _effort_row(account.provider, model, snapshot)
        # Adopt the row's default effort only when the model advertises a
        # real surface for it — a tier baked into the model id
        # (``swe-2-high`` on effort-less devin) leaves ``default_effort``
        # set but must not become a ``reasoning_effort`` the provider's
        # CLI then refuses.
        if row is not None and row.default_effort and row.default_effort in row.reasoning_efforts:
            effort = row.default_effort
            effort_source = "default"

    return AccountCandidate(
        account_id=account.id,
        provider=account.provider,
        status=status,
        running=running,
        max_concurrent=account.max_concurrent,
        eligible=not reasons,
        reasons=reasons,
        model=model,
        model_source=model_source,
        effort=effort,
        effort_source=effort_source,
        snapshot_source=snapshot_source,
        snapshot_stale=snapshot_stale,
        last_used_at=account.last_used_at,
    )


@dataclass
class ExecutionResolution:
    """The resolved execution plan + per-field provenance evidence."""

    provider: str
    account_id: str | None
    model: str | None
    reasoning_effort: str | None
    evidence: dict[str, Any]
    candidates: list[AccountCandidate]
    pick: AccountCandidate | None = None

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account_id": self.account_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "evidence": self.evidence,
            "candidates": [c.public() for c in self.candidates],
        }


# Reasons that are pure capability mismatches (vs. health/capacity/etc).
_CAPABILITY_REASONS = frozenset(
    {"model_not_advertised", "model_unresolvable", "effort_no_surface", "effort_unsupported"}
)


def _lru(candidates: list[AccountCandidate]) -> AccountCandidate:
    """LRU among eligible candidates — never-used first, stable id tiebreak."""
    return min(candidates, key=lambda c: (c.last_used_at or "", c.account_id))


def resolve_execution(
    execution: Mapping[str, Any] | None,
    *,
    registry: AccountRegistry,
    scheduler: Any,
    capabilities: Any,
    env: Mapping[str, str] | None = None,
    checks: list[Check] | None = None,
) -> ExecutionResolution:
    """Resolve ``execution`` to a concrete provider/model/effort/account.

    Named accounts are hard-refused when ineligible; ``auto`` picks LRU
    among the eligible set after the full capability filter, and the
    returned ``candidates`` list is the evidence of why every other account
    was excluded.
    """
    env = os.environ if env is None else env
    checks = checks if checks is not None else []
    from control.config import selected_providers

    execution = dict(execution or {})
    provider_req = execution.get("provider")
    model_req = execution.get("model")
    effort_req = execution.get("reasoning_effort")
    account_req = execution.get("account_id")

    provider_req = provider_req if provider_req not in (None, "", "auto") else None
    model_req = model_req if model_req not in (None, "", "auto") else None
    effort_req = effort_req if effort_req not in (None, "", "auto") else None
    account_req = account_req if account_req not in (None, "", "auto") else None

    enabled = tuple(p for p in selected_providers(env) if p in CANONICAL_PROVIDERS)

    if provider_req is not None and provider_req not in CANONICAL_PROVIDERS:
        raise TaskRefusal(400, "invalid_provider", f"unknown provider {provider_req!r}")
    if provider_req is not None and provider_req not in enabled:
        raise TaskRefusal(
            400,
            "invalid_provider",
            f"provider {provider_req!r} is not enabled in this deployment",
            checks=checks,
        )

    def _running(account_id: str) -> int:
        counter = getattr(scheduler, "running_count", None)
        if callable(counter):
            try:
                return int(counter(account_id))
            except Exception:
                pass
        try:
            return int(registry.running_count(account_id))
        except Exception:
            return 0

    # -- named account: the eligibility verdict is authoritative --------------
    if account_req is not None:
        account = registry.get(account_req)
        if account is None:
            raise TaskRefusal(
                409,
                "account_unavailable",
                f"account {account_req!r} does not exist",
                checks=checks,
            )
        if provider_req is not None and account.provider != provider_req:
            raise TaskRefusal(
                409,
                "account_unavailable",
                f"account {account_req!r} is provider {account.provider!r}, not {provider_req!r}",
                checks=checks,
            )
        candidate = evaluate_account(
            account,
            model_req=model_req,
            effort_req=effort_req,
            registry=registry,
            capabilities=capabilities,
            running_count=_running,
            env=env,
            enabled_providers=enabled,
        )
        if not candidate.eligible:
            capability_only = set(candidate.reasons) <= _CAPABILITY_REASONS
            code = "unsupported" if capability_only else "account_unavailable"
            status = 400 if capability_only else 409
            raise TaskRefusal(
                status,
                code,
                f"account {account_req!r} is not eligible: {', '.join(candidate.reasons)}",
                checks=checks,
                candidates=[candidate],
            )
        checks.append(
            Check("execution.account", "pass", f"pinned account {account_req!r} is eligible")
        )
        return ExecutionResolution(
            provider=account.provider,
            account_id=account.id,
            model=candidate.model,
            reasoning_effort=candidate.effort,
            evidence={
                "provider": {
                    "requested": provider_req or "auto",
                    "resolved": account.provider,
                    "source": "account",
                },
                "account_id": {
                    "requested": account_req,
                    "resolved": account.id,
                    "source": "requested",
                },
                "model": {
                    "requested": model_req or "auto",
                    "resolved": candidate.model,
                    "source": candidate.model_source,
                },
                "reasoning_effort": {
                    "requested": effort_req or "auto",
                    "resolved": candidate.effort,
                    "source": candidate.effort_source,
                },
            },
            candidates=[candidate],
            pick=candidate,
        )

    # -- auto account: filter every registered account --------------------------
    providers = (provider_req,) if provider_req is not None else enabled
    candidates: list[AccountCandidate] = []
    for provider in providers:
        for account in registry.list(provider):
            candidates.append(
                evaluate_account(
                    account,
                    model_req=model_req,
                    effort_req=effort_req,
                    registry=registry,
                    capabilities=capabilities,
                    running_count=_running,
                    env=env,
                    enabled_providers=enabled,
                )
            )
    eligible = [c for c in candidates if c.eligible]
    if not candidates:
        checks.append(
            Check(
                "execution.account",
                "fail",
                "no accounts registered for the selected providers",
            )
        )
        raise TaskRefusal(
            429,
            "provider_exhausted",
            "no accounts are registered for the requested provider",
            retry_after=60.0,
            checks=checks,
        )
    if not eligible:
        reason_sets = [set(c.reasons) for c in candidates]
        capability_only = all(rs and rs <= _CAPABILITY_REASONS for rs in reason_sets)
        if capability_only:
            detail = "; ".join(f"{c.account_id}: {', '.join(c.reasons)}" for c in candidates)
            checks.append(Check("execution.capability", "fail", f"no account can serve: {detail}"))
            raise TaskRefusal(
                400,
                "unsupported",
                f"no account can serve model={model_req!r} "
                f"reasoning_effort={effort_req!r} ({detail})",
                checks=checks,
                candidates=candidates,
            )
        cooling = [c for c in candidates if "status_cooling" in c.reasons]
        checks.append(
            Check(
                "execution.account",
                "fail",
                "no eligible account: "
                + "; ".join(f"{c.account_id} ({', '.join(c.reasons)})" for c in candidates),
            )
        )
        retry_after = 60.0 if cooling else None
        raise TaskRefusal(
            429,
            "provider_exhausted",
            "no eligible account after provider/runtime/auth/health/capacity/capability filtering",
            retry_after=retry_after,
            checks=checks,
            candidates=candidates,
        )

    pick = _lru(eligible)
    checks.append(
        Check(
            "execution.account",
            "pass",
            f"{len(eligible)} eligible account(s); LRU picked {pick.account_id!r}",
        )
    )
    global_cap = getattr(scheduler, "max_global", None)
    if isinstance(global_cap, int) and global_cap >= 1:
        active = getattr(scheduler, "active_count", None)
        try:
            running = int(active) if active is not None else None
        except Exception:
            running = None
        if running is not None and running >= global_cap:
            checks.append(
                Check(
                    "scheduler.capacity",
                    "warn",
                    f"global concurrency cap reached ({running}/{global_cap})",
                )
            )
    return ExecutionResolution(
        provider=pick.provider,
        account_id=pick.account_id,
        model=pick.model,
        reasoning_effort=pick.effort,
        evidence={
            "provider": {
                "requested": provider_req or "auto",
                "resolved": pick.provider,
                "source": "lru",
            },
            "account_id": {
                "requested": "auto",
                "resolved": pick.account_id,
                "source": "lru",
            },
            "model": {
                "requested": model_req or "auto",
                "resolved": pick.model,
                "source": pick.model_source,
            },
            "reasoning_effort": {
                "requested": effort_req or "auto",
                "resolved": pick.effort,
                "source": pick.effort_source,
            },
        },
        candidates=candidates,
        pick=pick,
    )


# --------------------------------------------------------------------------
# task resolution — source + execution + delivery
# --------------------------------------------------------------------------


@dataclass
class TaskResolution:
    """Everything ``create`` needs: workspace spec, git policy, execution."""

    source: SourceResolution | None
    git: dict[str, Any] | None
    execution: ExecutionResolution
    checks: list[Check]
    warnings: list[str]

    def resolved_payload(self) -> dict[str, Any]:
        return {
            "source": self.source.evidence() if self.source is not None else None,
            "git": self.git,
            "execution": self.execution.public(),
        }

    def public(self, *, ok: bool) -> dict[str, Any]:
        return {
            "ok": ok,
            "checks": [c.public() for c in self.checks],
            "warnings": list(self.warnings),
            "resolved": self.resolved_payload(),
        }


def resolve_task(
    spec: Mapping[str, Any],
    *,
    registry: AccountRegistry,
    scheduler: Any,
    capabilities: Any,
    resolver: RepoResolver,
    env: Mapping[str, str] | None = None,
) -> TaskResolution:
    """Full resolution for both preflight and create (always re-run)."""
    env = os.environ if env is None else env
    checks: list[Check] = []
    warnings: list[str] = []
    source_spec = spec.get("source")
    delivery_spec = spec.get("delivery")

    git = delivery_to_git(delivery_spec)
    if git is not None:
        from control.workspace import WorkspaceError, validate_git_policy

        try:
            validate_git_policy(git)
        except WorkspaceError as exc:
            raise TaskRefusal(400, exc.code, exc.message, checks=checks) from exc
    if git is not None and source_spec is None:
        raise TaskRefusal(
            400, "workspace_invalid", "delivery requires a source declaration", checks=checks
        )

    source: SourceResolution | None = None
    if source_spec is not None:
        if not isinstance(source_spec, Mapping):
            raise TaskRefusal(400, "workspace_invalid", "source must be an object", checks=checks)
        needs_push = bool(git and git.get("push"))
        source, source_checks, source_warnings = resolve_source(
            source_spec, resolver=resolver, env=env, needs_push=needs_push
        )
        checks.extend(source_checks)
        warnings.extend(source_warnings)
        if git is not None:
            checks.append(Check("delivery", "pass", "git delivery policy resolved"))

    execution_spec = spec.get("execution")
    if execution_spec is not None and not isinstance(execution_spec, Mapping):
        raise TaskRefusal(400, "workspace_invalid", "execution must be an object", checks=checks)
    execution = resolve_execution(
        execution_spec,
        registry=registry,
        scheduler=scheduler,
        capabilities=capabilities,
        env=env,
        checks=checks,
    )
    return TaskResolution(
        source=source, git=git, execution=execution, checks=checks, warnings=warnings
    )


# --------------------------------------------------------------------------
# Task persistence
# --------------------------------------------------------------------------


@dataclass
class TaskRecord:
    """One durable Task row.

    ``request`` is the verbatim caller declaration; ``resolved`` is the
    authoritative plan + per-field evidence. ``response`` pins the create
    reply so a replayed ``Idempotency-Key`` resolves after a restart.
    """

    id: str
    owner: str
    status: str
    request: dict[str, Any]
    resolved: dict[str, Any] | None
    agent_id: str | None
    run_id: str | None
    created_at: str
    updated_at: str
    response: dict[str, Any] | None = None
    idempotency: dict[str, Any] | None = None
    # SOR-224: durable status-transition log — ``{status, reason, at}`` per
    # observed change. Task status derives live from the run ledger + the
    # workspace delivery record, and each new derived value is appended so
    # an orchestrator can read the machine-readable history post-restart.
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "request": self.request,
            "resolved": self.resolved,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "transitions": list(self.transitions),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:16]}"


def record_to_dict(record: TaskRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "owner": record.owner,
        "status": record.status,
        "request": record.request,
        "resolved": record.resolved,
        "agent_id": record.agent_id,
        "run_id": record.run_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "response": record.response,
        "idempotency": record.idempotency,
        "transitions": list(record.transitions),
    }


def record_from_dict(data: Mapping[str, Any]) -> TaskRecord:
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise ValueError("task record missing id")
    if not isinstance(data.get("owner"), str):
        raise ValueError("task record missing owner")
    return TaskRecord(
        id=data["id"],
        owner=data["owner"],
        status=str(data.get("status") or "queued"),
        request=dict(data.get("request") or {}),
        resolved=data.get("resolved"),
        agent_id=data.get("agent_id"),
        run_id=data.get("run_id"),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        response=data.get("response"),
        idempotency=data.get("idempotency"),
        transitions=[dict(t) for t in (data.get("transitions") or []) if isinstance(t, dict)],
    )


@runtime_checkable
class TaskStore(Protocol):
    def put(self, record: TaskRecord) -> None: ...

    def get(self, task_id: str) -> TaskRecord | None: ...

    def delete(self, task_id: str) -> None: ...

    def list(self, owner: str | None = None) -> list[TaskRecord]: ...

    def find_by_idempotency(self, owner: str, key: str) -> TaskRecord | None: ...

    def find_by_agent(self, agent_id: str) -> TaskRecord | None: ...


class InMemoryTaskStore:
    """Thread-safe dict store (tests, ephemeral deployments)."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, record: TaskRecord) -> None:
        with self._lock:
            self._items[record.id] = record_to_dict(record)

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            raw = self._items.get(task_id)
        return record_from_dict(raw) if raw is not None else None

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._items.pop(task_id, None)

    def list(self, owner: str | None = None) -> list[TaskRecord]:
        with self._lock:
            items = list(self._items.values())
        out = []
        for raw in items:
            rec = record_from_dict(raw)
            if owner is None or rec.owner == owner:
                out.append(rec)
        return sorted(out, key=lambda r: r.created_at)

    def find_by_idempotency(self, owner: str, key: str) -> TaskRecord | None:
        for rec in self.list(owner):
            meta = rec.idempotency or {}
            if meta.get("key") == key:
                return rec
        return None

    def find_by_agent(self, agent_id: str) -> TaskRecord | None:
        for rec in self.list():
            if rec.agent_id == agent_id:
                return rec
        return None


class FileTaskStore:
    """Local durable store: ``root/<task_id>.json`` (atomic writes)."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _path(self, task_id: str) -> Path:
        return self._root / f"{task_id}.json"

    def put(self, record: TaskRecord) -> None:
        path = self._path(record.id)
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record_to_dict(record), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)

    def get(self, task_id: str) -> TaskRecord | None:
        try:
            raw = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"stored task record {task_id} is corrupt: {exc}") from exc
        return record_from_dict(raw)

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._path(task_id).unlink(missing_ok=True)

    def list(self, owner: str | None = None) -> list[TaskRecord]:
        try:
            paths = sorted(self._root.glob("task_*.json"))
        except OSError:
            return []
        out: list[TaskRecord] = []
        for path in paths:
            try:
                rec = self.get(path.stem)
            except (OSError, ValueError):
                continue
            if rec is not None and (owner is None or rec.owner == owner):
                out.append(rec)
        return sorted(out, key=lambda r: r.created_at)

    def find_by_idempotency(self, owner: str, key: str) -> TaskRecord | None:
        for rec in self.list(owner):
            meta = rec.idempotency or {}
            if meta.get("key") == key:
                return rec
        return None

    def find_by_agent(self, agent_id: str) -> TaskRecord | None:
        for rec in self.list():
            if rec.agent_id == agent_id:
                return rec
        return None


class ModalDictTaskStore:
    """Production store backed by ``modal.Dict`` (``sbx-tasks``).

    Records live under ``task/<id>``; a per-owner index ``owner/<owner>``
    keeps list()/find_by_idempotency() cheap without a full-dict scan.
    """

    def __init__(self, name: str = TASKS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None
        self._lock = threading.Lock()

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def put(self, record: TaskRecord) -> None:
        d = self._d()
        d.put(f"task/{record.id}", record_to_dict(record))
        index_key = f"owner/{record.owner}"
        with self._lock:
            ids = list(d.get(index_key) or [])
            if record.id not in ids:
                ids.append(record.id)
            d.put(index_key, ids)
            self._index_owner(record.owner)

    def get(self, task_id: str) -> TaskRecord | None:
        raw = self._d().get(f"task/{task_id}")
        return record_from_dict(raw) if raw is not None else None

    def delete(self, task_id: str) -> None:
        rec = self.get(task_id)
        try:
            self._d().pop(f"task/{task_id}")
        except KeyError:
            pass
        if rec is not None:
            index_key = f"owner/{rec.owner}"
            ids = [i for i in list(self._d().get(index_key) or []) if i != task_id]
            self._d().put(index_key, ids)

    def list(self, owner: str | None = None) -> list[TaskRecord]:
        if owner is None:
            # Dict has no key scan — every record goes through an owner
            # index, so listing without owner needs an index of indexes.
            owners = list(self._d().get("__owners__") or [])
            out: list[TaskRecord] = []
            for name in owners:
                out.extend(self.list(str(name)))
            return out
        out = []
        for task_id in list(self._d().get(f"owner/{owner}") or []):
            rec = self.get(str(task_id))
            if rec is not None:
                out.append(rec)
        return sorted(out, key=lambda r: r.created_at)

    def find_by_idempotency(self, owner: str, key: str) -> TaskRecord | None:
        for rec in self.list(owner):
            meta = rec.idempotency or {}
            if meta.get("key") == key:
                return rec
        return None

    def _index_owner(self, owner: str) -> None:
        d = self._d()
        owners = list(d.get("__owners__") or [])
        if owner not in owners:
            owners.append(owner)
            d.put("__owners__", owners)


__all__ = [
    "CANONICAL_PROVIDERS",
    "TASKS_DICT_ENV",
    "TASKS_DICT_NAME",
    "TASK_STORE_DIR_ENV",
    "AccountCandidate",
    "CanonicalRepo",
    "ChainRepoResolver",
    "Check",
    "ExecutionResolution",
    "FileTaskStore",
    "GitHubApiResolver",
    "GitLsRemoteResolver",
    "InMemoryTaskStore",
    "ModalDictTaskStore",
    "RepoResolver",
    "SourceResolution",
    "TaskRecord",
    "TaskRefusal",
    "TaskResolution",
    "TaskStore",
    "canonicalize_repo",
    "default_repo_resolver",
    "delivery_to_git",
    "evaluate_account",
    "has_auth_material",
    "new_task_id",
    "record_from_dict",
    "record_to_dict",
    "resolve_execution",
    "resolve_source",
    "resolve_task",
]
