"""GitHub App one-click authorization (SOR-177).

The PAT/env bridge (``control.github`` + ``SBX_GITHUB_EPHEMERAL``) stays as the
compatibility fallback; this module is the preferred source: the control plane
holds the App's private key, records *selected-repo authorization metadata*
(installation id, account, repository selection, repo list) durably, and mints
**short-lived installation access tokens** server-side — the existing
``exec_env``/``GIT_CONFIG_*`` seam then injects the minted token exactly like
an env PAT.

Flow:

- ``POST /v1/github/app/authorize`` returns a one-click install URL carrying a
  server-generated ``state`` (CSRF) token.
- The browser lands on ``GET /v1/github/app/callback`` (GitHub's Setup URL)
  with ``installation_id`` + ``state`` — the state is the capability (the
  redirect cannot carry a Bearer key); the server verifies it, syncs the
  installation's repo selection from GitHub, and records it.
- ``GET /v1/github/app`` reports posture — names/ids/repos only, never token
  or key material. ``POST /v1/github/app/sync`` re-reads GitHub truth.
  ``DELETE /v1/github/app/installations/{id}`` revokes one installation:
  best-effort uninstall upstream, then clears the local record + cached
  tokens. Reconnect is authorize → callback again.

Token safety: the private key and minted tokens travel env/Secret only —
never argv, disk, logs, or responses. ``sandbox_token(repo=...)`` mints a
token scoped to the authorized repo when repo context is known.
"""

from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from control.github import TOKEN_ENVS, repo_slug

# Env config — names only; the private key itself arrives via the same
# env/Modal-Secret channel as GH_TOKEN (``SBX_GITHUB_APP_SECRET_NAME`` names
# the Secret on remote deploys). ``SBX_GITHUB_APP_API_URL`` is a test/dev
# seam — the contract target is https://api.github.com.
APP_ID_ENV = "SBX_GITHUB_APP_ID"
APP_SLUG_ENV = "SBX_GITHUB_APP_SLUG"
APP_KEY_ENV = "SBX_GITHUB_APP_PRIVATE_KEY"
APP_SECRET_NAME_ENV = "SBX_GITHUB_APP_SECRET_NAME"
APP_DICT_ENV = "SBX_GITHUB_APP_DICT"
APP_STORE_DIR_ENV = "SBX_GITHUB_APP_STORE_DIR"
APP_API_URL_ENV = "SBX_GITHUB_APP_API_URL"

DEFAULT_DICT_NAME = "sbx-github-app"
DEFAULT_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

# GitHub installation tokens live ~1h; refresh this early so a minted token
# can never die mid-exec.
_TOKEN_REFRESH_MARGIN_S = 120
# One-click authorize states are single-use and expire quickly — the operator
# completes the browser install inside this window.
AUTHORIZE_STATE_TTL_S = 600

_APP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
# Bare ``owner/repo`` slugs are also accepted as repo context (the durable
# record stores slugs); anything else — local paths, non-github hosts —
# normalizes to ``None`` and fails closed.
_REPO_SLUG_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?$")


def _normalize_slug(repo: Any) -> str | None:
    """``owner/repo`` from a github.com URL or a bare slug, else ``None``."""
    slug = repo_slug(repo)
    if slug is not None:
        return slug
    if isinstance(repo, str):
        match = _REPO_SLUG_RE.match(repo.strip())
        if match is not None:
            return f"{match.group(1)}/{match.group(2)}"
    return None


class GitHubAppError(Exception):
    """Failures the /v1 layer maps onto canonical error codes."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _raise_unconfigured() -> None:
    raise GitHubAppError(
        "github_app_unconfigured",
        "GitHub App is not configured — set SBX_GITHUB_APP_ID and "
        "SBX_GITHUB_APP_PRIVATE_KEY (Modal Secret on remote deploys), or use "
        "the GH_TOKEN/GITHUB_TOKEN compatibility bridge",
        status_code=503,
    )


# --------------------------------------------------------------------------
# config + records


@dataclass(frozen=True)
class GitHubAppConfig:
    """Resolved App configuration — the private key never leaves this type."""

    app_id: str = ""
    slug: str = ""
    private_key: str = ""

    @property
    def configured(self) -> bool:
        """Id + key present — the minimum for JWT signing and token mint."""
        return bool(self.app_id.strip()) and bool(self.private_key.strip())

    @property
    def installable(self) -> bool:
        """Plus a slug — needed to render the one-click install URL."""
        return self.configured and bool(self.slug.strip())

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GitHubAppConfig:
        env = os.environ if env is None else env
        key = (env.get(APP_KEY_ENV) or "").strip()
        # Modal Secrets / shell exports commonly carry the PEM with literal
        # ``\n`` escapes — normalize so JWT signing sees real newlines.
        if "\\n" in key:
            key = key.replace("\\n", "\n")
        return cls(
            app_id=(env.get(APP_ID_ENV) or "").strip(),
            slug=(env.get(APP_SLUG_ENV) or "").strip().lower(),
            private_key=key,
        )


@dataclass
class InstallationRecord:
    """Selected-repo authorization metadata for one GitHub App install.

    ``repositories`` holds ``owner/repo`` slugs and is only populated when
    ``repository_selection == "selected"`` (``"all"`` covers every repo on
    the account). No token material — this record is safe to surface on /v1.
    """

    installation_id: int
    account_login: str
    account_type: str  # "User" | "Organization"
    repository_selection: str  # "all" | "selected"
    repositories: list[str] = field(default_factory=list)
    suspended: bool = False
    recorded_at: str = ""
    synced_at: str = ""

    def authorizes(self, slug: str) -> bool:
        """Whether this installation's repo selection covers ``owner/repo``."""
        if self.suspended:
            return False
        if self.repository_selection == "all":
            return slug.split("/", 1)[0].lower() == self.account_login.lower()
        return slug.lower() in {repo.lower() for repo in self.repositories}

    def public(self) -> dict[str, Any]:
        """API-safe projection — metadata only, never secrets."""
        return {
            "installation_id": self.installation_id,
            "account_login": self.account_login,
            "account_type": self.account_type,
            "repository_selection": self.repository_selection,
            "repositories": list(self.repositories),
            "suspended": self.suspended,
            "recorded_at": self.recorded_at,
            "synced_at": self.synced_at,
        }


def record_to_dict(record: InstallationRecord) -> dict[str, Any]:
    return {
        "installation_id": record.installation_id,
        "account_login": record.account_login,
        "account_type": record.account_type,
        "repository_selection": record.repository_selection,
        "repositories": list(record.repositories),
        "suspended": record.suspended,
        "recorded_at": record.recorded_at,
        "synced_at": record.synced_at,
    }


def record_from_dict(data: Any) -> InstallationRecord | None:
    if not isinstance(data, dict):
        return None
    try:
        return InstallationRecord(
            installation_id=int(data["installation_id"]),
            account_login=str(data.get("account_login") or ""),
            account_type=str(data.get("account_type") or ""),
            repository_selection=str(data.get("repository_selection") or "selected"),
            repositories=[str(r) for r in (data.get("repositories") or [])],
            suspended=bool(data.get("suspended", False)),
            recorded_at=str(data.get("recorded_at") or ""),
            synced_at=str(data.get("synced_at") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# durable store — installations + pending authorize states


class GitHubAppStore(Protocol):
    """Installations keyed by id, plus single-use pending authorize states."""

    def list(self) -> list[InstallationRecord]: ...
    def get(self, installation_id: int) -> InstallationRecord | None: ...
    def put(self, record: InstallationRecord) -> None: ...
    def delete(self, installation_id: int) -> None: ...
    def put_state(self, state: str, expires_epoch: float) -> None: ...
    def pop_state(self, state: str) -> float | None:
        """Consume a pending state; returns its expiry epoch, or None."""
        ...


class InMemoryGitHubAppStore:
    def __init__(self) -> None:
        self._items: dict[int, dict[str, Any]] = {}
        self._states: dict[str, float] = {}
        self._lock = threading.Lock()

    def list(self) -> list[InstallationRecord]:
        with self._lock:
            items = list(self._items.values())
        out = [record_from_dict(r) for r in items]
        return sorted((r for r in out if r is not None), key=lambda r: r.installation_id)

    def get(self, installation_id: int) -> InstallationRecord | None:
        with self._lock:
            raw = self._items.get(installation_id)
        return record_from_dict(raw) if raw is not None else None

    def put(self, record: InstallationRecord) -> None:
        with self._lock:
            self._items[record.installation_id] = record_to_dict(record)

    def delete(self, installation_id: int) -> None:
        with self._lock:
            self._items.pop(installation_id, None)

    def put_state(self, state: str, expires_epoch: float) -> None:
        with self._lock:
            self._states[state] = expires_epoch

    def pop_state(self, state: str) -> float | None:
        with self._lock:
            return self._states.pop(state, None)


class FileGitHubAppStore:
    """JSON-per-installation store for the local (non-Modal) control plane.

    Same re-open semantics as ``FileWorkspaceStore`` — records persist under
    ``$SBX_GITHUB_APP_STORE_DIR`` (or ``$XDG_STATE_HOME/sbx-browser/github-app``)
    and survive local restarts.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _path(self, installation_id: int) -> Path:
        return self._root / f"installation-{installation_id}.json"

    def _states_path(self) -> Path:
        return self._root / "authorize-states.json"

    def list(self) -> list[InstallationRecord]:
        out: list[InstallationRecord] = []
        with self._lock:
            paths = sorted(self._root.glob("installation-*.json"))
            for path in paths:
                try:
                    rec = record_from_dict(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
                if rec is not None:
                    out.append(rec)
        return out

    def get(self, installation_id: int) -> InstallationRecord | None:
        path = self._path(installation_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return record_from_dict(json.loads(raw))
        except json.JSONDecodeError:
            return None

    def put(self, record: InstallationRecord) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.installation_id)
        with self._lock:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record_to_dict(record)), encoding="utf-8")
            tmp.replace(path)

    def delete(self, installation_id: int) -> None:
        with self._lock:
            try:
                self._path(installation_id).unlink()
            except FileNotFoundError:
                pass

    def put_state(self, state: str, expires_epoch: float) -> None:
        with self._lock:
            states = self._read_states()
            states[state] = expires_epoch
            self._write_states(states)

    def pop_state(self, state: str) -> float | None:
        with self._lock:
            states = self._read_states()
            expiry = states.pop(state, None)
            if expiry is not None:
                self._write_states(states)
            return float(expiry) if expiry is not None else None

    def _read_states(self) -> dict[str, float]:
        try:
            raw = self._states_path().read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}

    def _write_states(self, states: dict[str, float]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._states_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(states), encoding="utf-8")
        tmp.replace(path)


class ModalDictGitHubAppStore:
    """Production store backed by ``modal.Dict`` — lazy-imports modal."""

    def __init__(self, name: str = DEFAULT_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def list(self) -> list[InstallationRecord]:
        out: list[InstallationRecord] = []
        for key, raw in self._d().items():
            if str(key).startswith("installation:"):
                rec = record_from_dict(raw)
                if rec is not None:
                    out.append(rec)
        return sorted(out, key=lambda r: r.installation_id)

    def get(self, installation_id: int) -> InstallationRecord | None:
        raw = self._d().get(f"installation:{installation_id}")
        return record_from_dict(raw)

    def put(self, record: InstallationRecord) -> None:
        self._d()[f"installation:{record.installation_id}"] = record_to_dict(record)

    def delete(self, installation_id: int) -> None:
        try:
            self._d().pop(f"installation:{installation_id}")
        except KeyError:
            pass

    def put_state(self, state: str, expires_epoch: float) -> None:
        self._d()[f"state:{state}"] = expires_epoch

    def pop_state(self, state: str) -> float | None:
        try:
            expiry = self._d().pop(f"state:{state}")
        except KeyError:
            return None
        return float(expiry)


# --------------------------------------------------------------------------
# GitHub API client — app JWT + installation access tokens


class GitHubAppClient:
    """Minimal github.com REST client for the App auth flow.

    All calls go through injectable httpx plumbing (``transport`` for tests,
    ``api_url`` for GHE/dev). JWTs are RS256-signed with the App private key
    and live ≤10 min; installation access tokens are minted per call and
    live ~1h upstream — the service caches them below their expiry.
    """

    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        api_url: str = DEFAULT_API_URL,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 15.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._api_url = api_url.rstrip("/")
        self._timeout_s = timeout_s
        self._clock = clock
        self._client = httpx.Client(transport=transport, timeout=timeout_s)

    @property
    def config(self) -> GitHubAppConfig:
        return self._config

    def _jwt(self) -> str:
        """Sign an App JWT (iss=app_id, 10 min bound, 60 s clock-skew backdate)."""
        import jwt  # PyJWT[crypto] — RS256 needs the cryptography extra

        now = int(self._clock())
        payload = {"iat": now - 60, "exp": now + 600, "iss": self._config.app_id}
        return jwt.encode(payload, self._config.private_key, algorithm="RS256")

    def _request(
        self,
        method: str,
        path: str,
        *,
        authorization: str,
        json_body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
        what: str,
    ) -> Any:
        """One GitHub API call; failures raise ``GitHubAppError`` with a
        clipped, secret-free message (response bodies may echo request
        fragments — never let more than 200 chars through)."""
        try:
            resp = self._client.request(
                method,
                f"{self._api_url}{path}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": authorization,
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise GitHubAppError(
                "github_app_upstream",
                f"GitHub API {what} failed: {type(exc).__name__}",
                status_code=502,
            ) from exc
        if resp.status_code not in expected:
            detail = resp.text[:200].replace(self._config.app_id or " ", "<app-id>")
            raise GitHubAppError(
                "github_app_upstream",
                f"GitHub API {what} returned {resp.status_code}"
                + (f": {detail}" if detail else ""),
                status_code=502,
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise GitHubAppError(
                "github_app_upstream",
                f"GitHub API {what} returned no JSON",
                status_code=502,
            ) from exc

    def _app_request(self, method: str, path: str, *, what: str, **kwargs: Any) -> Any:
        return self._request(
            method, path, authorization=f"Bearer {self._jwt()}", what=what, **kwargs
        )

    def list_installations(self) -> list[dict[str, Any]]:
        """``GET /app/installations`` (JWT) — the app's own installs only."""
        data = self._app_request("GET", "/app/installations", what="list installations")
        return data if isinstance(data, list) else []

    def create_installation_token(
        self, installation_id: int, *, repositories: list[str] | None = None
    ) -> tuple[str, float]:
        """Mint a short-lived installation access token → (token, expiry epoch).

        ``repositories`` narrows the token to named repos within the
        installation's selection — the least-privilege mint used when the
        workspace's repo is known.
        """
        body: dict[str, Any] = {}
        if repositories:
            body["repositories"] = repositories
        data = self._app_request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            json_body=body or None,
            expected=(200, 201),
            what="create installation token",
        )
        if not isinstance(data, dict) or not data.get("token"):
            raise GitHubAppError(
                "github_app_upstream",
                "GitHub API create installation token returned no token",
                status_code=502,
            )
        expires_at = data.get("expires_at")
        try:
            expiry = _parse_github_time(expires_at)
        except ValueError:
            expiry = self._clock() + 3600
        return str(data["token"]), expiry

    def installation_repositories(self, token: str) -> tuple[str, list[str]]:
        """``GET /installation/repositories`` with an installation token.

        Returns ``(repository_selection, [owner/repo, ...])`` — the selected-
        repo metadata recorded server-side. The token arrives as an argument
        and is used inline only; it is never stored or returned.
        """
        repos: list[str] = []
        selection = "selected"
        page = 1
        while True:
            data = self._request(
                "GET",
                f"/installation/repositories?per_page=100&page={page}",
                authorization=f"token {token}",
                what="list installation repositories",
            )
            if not isinstance(data, dict):
                break
            selection = str(data.get("repository_selection") or selection)
            batch = [
                str(r["full_name"])
                for r in (data.get("repositories") or [])
                if isinstance(r, dict) and r.get("full_name")
            ]
            repos.extend(batch)
            total = int(data.get("total_count") or len(repos))
            if len(repos) >= total or not batch:
                break
            page += 1
        return selection, repos

    def delete_installation(self, installation_id: int) -> bool:
        """``DELETE /app/installations/{id}`` (JWT) — upstream uninstall."""
        try:
            self._app_request(
                "DELETE",
                f"/app/installations/{installation_id}",
                expected=(204,),
                what="delete installation",
            )
            return True
        except GitHubAppError:
            return False


def _parse_github_time(value: Any) -> float:
    """GitHub ``expires_at`` (``2024-01-01T00:00:00Z``) → epoch seconds."""
    from datetime import datetime

    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


# --------------------------------------------------------------------------
# service — authorize/callback/sync/revoke + token minting for the bridge


class GitHubAppService:
    """Orchestrates the one-click flow and the sandbox token seam.

    ``sandbox_token`` is the only entry the injection path (``control.github``)
    uses: it picks the recorded installation that authorizes the repo (when
    repo context is given) and returns a cached-or-fresh installation token.
    Everything here is fail-closed — an unconfigured app or a repo outside
    every installation's selection yields ``None``, not a wider token.
    """

    def __init__(
        self,
        config: GitHubAppConfig,
        store: GitHubAppStore,
        client: GitHubAppClient | None = None,
        *,
        clock: Callable[[], float] = time.time,
        records_ttl_s: float = 60.0,
    ) -> None:
        self._config = config
        self._store = store
        self._client = client or GitHubAppClient(config)
        self._clock = clock
        self._records_ttl_s = records_ttl_s
        self._tokens: dict[tuple[int, str], tuple[str, float]] = {}
        self._records_cache: tuple[float, list[InstallationRecord]] | None = None
        self._lock = threading.Lock()

    # -- posture -----------------------------------------------------------

    @property
    def config(self) -> GitHubAppConfig:
        return self._config

    @property
    def configured(self) -> bool:
        return self._config.configured

    def _records(self) -> list[InstallationRecord]:
        """Recorded installations, bounded-refresh cached (60 s default)."""
        with self._lock:
            if self._records_cache is not None:
                cached_at, records = self._records_cache
                if self._clock() - cached_at < self._records_ttl_s:
                    return list(records)
        records = self._store.list()
        with self._lock:
            self._records_cache = (self._clock(), records)
        return list(records)

    def _invalidate_records(self) -> None:
        with self._lock:
            self._records_cache = None

    def status(self) -> dict[str, Any]:
        """Public posture: config + installation metadata, never secrets.

        ``bridge_token`` reports whether the PAT/env compatibility fallback
        (GH_TOKEN/GITHUB_TOKEN) can still supply tokens.
        """
        return {
            "configured": self._config.configured,
            "installable": self._config.installable,
            "app_id": self._config.app_id or None,
            "app_slug": self._config.slug or None,
            "installations": [r.public() for r in self._records()],
            "bridge_token": any(os.environ.get(name) for name in TOKEN_ENVS),
        }

    # -- one-click authorization ------------------------------------------

    def begin_authorization(self) -> dict[str, Any]:
        """Create a pending ``state`` and the one-click install URL."""
        if not self._config.installable:
            _raise_unconfigured()
        state = _secrets.token_urlsafe(24)
        expires_at = self._clock() + AUTHORIZE_STATE_TTL_S
        self._store.put_state(state, expires_at)
        url = f"https://github.com/apps/{self._config.slug}/installations/new?state={state}"
        return {
            "authorize_url": url,
            "state": state,
            "expires_at": _iso_from_epoch(expires_at),
        }

    def complete_authorization(self, installation_id: Any, state: str | None) -> InstallationRecord:
        """Consume the pending state and record the install GitHub reports.

        ``state`` is the capability — the browser redirect cannot carry a
        Bearer key. Unknown/expired/reused states are refused; the recorded
        metadata comes from GitHub's API, never from callback params alone.
        """
        if not self._config.configured:
            _raise_unconfigured()
        try:
            iid = int(installation_id)
        except (TypeError, ValueError):
            raise GitHubAppError(
                "github_app_invalid", "callback requires a numeric installation_id"
            ) from None
        if not state:
            raise GitHubAppError(
                "github_app_state", "callback requires the pending state", status_code=403
            )
        expiry = self._store.pop_state(state)
        if expiry is None or expiry < self._clock():
            raise GitHubAppError(
                "github_app_state", "unknown or expired authorize state", status_code=403
            )
        record = self._sync_one(iid)
        if record is None:
            raise GitHubAppError(
                "not_found",
                "installation not reported by GitHub for this app",
                status_code=404,
            )
        return record

    def _sync_one(self, installation_id: int) -> InstallationRecord | None:
        """Fetch + record one installation (JWT list → repo selection)."""
        installation = next(
            (
                i
                for i in self._client.list_installations()
                if int(i.get("id") or 0) == installation_id
            ),
            None,
        )
        if installation is None:
            return None
        return self._record_installation(installation)

    def _record_installation(self, installation: dict[str, Any]) -> InstallationRecord:
        iid = int(installation["id"])
        account = installation.get("account") or {}
        selection = str(installation.get("repository_selection") or "selected")
        existing = self._store.get(iid)
        # Mint a scoped-free token only to enumerate the selected repos — the
        # token is used in-line and discarded; the record keeps metadata only.
        repos: list[str] = []
        if selection == "selected":
            token, _expiry = self._client.create_installation_token(iid)
            _, repos = self._client.installation_repositories(token)
        now = _iso_from_epoch(self._clock())
        record = InstallationRecord(
            installation_id=iid,
            account_login=str(account.get("login") or ""),
            account_type=str(account.get("type") or ""),
            repository_selection=selection,
            repositories=repos,
            suspended=bool(installation.get("suspended_at")),
            recorded_at=existing.recorded_at if existing else now,
            synced_at=now,
        )
        self._store.put(record)
        self._invalidate_records()
        return record

    def sync(self) -> list[InstallationRecord]:
        """Re-read all installations from GitHub; drop ones GitHub forgot."""
        if not self._config.configured:
            _raise_unconfigured()
        seen: set[int] = set()
        out: list[InstallationRecord] = []
        for installation in self._client.list_installations():
            iid = int(installation.get("id") or 0)
            if not iid:
                continue
            seen.add(iid)
            out.append(self._record_installation(installation))
        for record in self._store.list():
            if record.installation_id not in seen:
                self._store.delete(record.installation_id)
        self._invalidate_records()
        return out

    def revoke(self, installation_id: int | None = None) -> dict[str, Any]:
        """Revoke installation(s): best-effort upstream uninstall, then local
        records + token cache cleared — fail-closed on either side so a half-
        revoked install never keeps working. ``installation_id=None`` revokes
        every recorded installation. Returns ``{"revoked": n, "remote_deleted": b}``.
        """
        records = self._records()
        if installation_id is not None:
            records = [r for r in records if r.installation_id == installation_id]
            if not records:
                raise GitHubAppError(
                    "not_found",
                    f"no recorded installation {installation_id}",
                    status_code=404,
                )
        remote_deleted = True
        for record in records:
            if self._config.configured and not self._client.delete_installation(
                record.installation_id
            ):
                remote_deleted = False
            self._store.delete(record.installation_id)
            with self._lock:
                self._tokens = {
                    key: value
                    for key, value in self._tokens.items()
                    if key[0] != record.installation_id
                }
                self._records_cache = None
        return {"revoked": len(records), "remote_deleted": remote_deleted}

    # -- sandbox token seam -------------------------------------------------

    def installation_for_repo(self, slug: str) -> InstallationRecord | None:
        """The recorded installation authorizing ``owner/repo``, if any."""
        owner = slug.split("/", 1)[0]
        for record in self._records():
            if record.account_login.lower() == owner.lower() and record.authorizes(slug):
                return record
        return None

    def can_supply(self, repo: str | None = None) -> bool:
        """Cheap no-network gate for ``github.injection_enabled``: a
        configured app with at least one recorded (repo-authorizing)
        installation."""
        if not self._config.configured:
            return False
        try:
            records = self._records()
        except Exception:
            return False
        if repo is None:
            return any(not r.suspended for r in records)
        slug = _normalize_slug(repo)
        if slug is None:
            return False  # non-github.com repo — no App can authorize it
        return self.installation_for_repo(slug) is not None

    def sandbox_token(self, repo: str | None = None) -> str | None:
        """A short-lived installation token for sandbox injection.

        ``repo`` (an ``owner/repo`` slug or clone URL) selects the authorizing
        installation and narrows the mint to that repo — least privilege when
        the caller knows the target. Without it, the first recorded
        installation's token covers its whole authorized selection.
        ``None`` = no authorized source (fail closed).
        """
        if not self._config.configured:
            return None
        slug = _normalize_slug(repo) if repo else None
        if repo and slug is None:
            return None
        if slug is not None:
            record = self.installation_for_repo(slug)
            if record is None:
                return None
            repositories: list[str] | None = [slug.split("/", 1)[1]]
        else:
            record = next((r for r in self._records() if not r.suspended), None)
            if record is None:
                return None
            repositories = None
        key = (record.installation_id, ",".join(sorted(repositories or [])))
        with self._lock:
            cached = self._tokens.get(key)
            if cached is not None and cached[1] - _TOKEN_REFRESH_MARGIN_S > self._clock():
                return cached[0]
        token, expiry = self._client.create_installation_token(
            record.installation_id, repositories=repositories
        )
        with self._lock:
            self._tokens[key] = (token, expiry)
        return token


def _iso_from_epoch(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat()


# --------------------------------------------------------------------------
# default service — shared by the /v1 routes and the sandbox injection seam


def _default_store(env: Mapping[str, str]) -> GitHubAppStore:
    if env.get("SBX_BACKEND") == "modal":
        return ModalDictGitHubAppStore(env.get(APP_DICT_ENV) or DEFAULT_DICT_NAME)
    override = env.get(APP_STORE_DIR_ENV)
    if override:
        return FileGitHubAppStore(override)
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path(env.get("HOME") or str(Path.home())) / ".local" / "state"
    return FileGitHubAppStore(base / "sbx-browser" / "github-app")


_default_lock = threading.Lock()
_default_key: tuple[Any, ...] | None = None
_default_service: GitHubAppService | None = None


def default_service(env: Mapping[str, str] | None = None) -> GitHubAppService:
    """The process-wide service (memoized on the resolved config + store
    identity) so the token/records cache is shared across sandbox execs."""
    global _default_key, _default_service
    env = os.environ if env is None else env
    config = GitHubAppConfig.from_env(env)
    key = (
        config.app_id,
        config.slug,
        bool(config.private_key),
        env.get(APP_DICT_ENV) or DEFAULT_DICT_NAME,
        env.get(APP_STORE_DIR_ENV) or "",
        env.get(APP_API_URL_ENV) or "",
        env.get("SBX_BACKEND") or "",
    )
    with _default_lock:
        if _default_service is not None and _default_key == key:
            return _default_service
        client = GitHubAppClient(
            config,
            api_url=env.get(APP_API_URL_ENV) or DEFAULT_API_URL,
        )
        _default_service = GitHubAppService(config, _default_store(env), client)
        _default_key = key
        return _default_service


def reset_default_service() -> None:
    """Drop the memoized service (tests; config rotation)."""
    global _default_key, _default_service
    with _default_lock:
        _default_key = None
        _default_service = None


def sandbox_token(env: Mapping[str, str] | None = None, *, repo: str | None = None) -> str | None:
    """Module-level seam ``control.github`` calls: mint-or-serve a token."""
    try:
        return default_service(env).sandbox_token(repo=repo)
    except GitHubAppError:
        return None


def can_supply(env: Mapping[str, str] | None = None, *, repo: str | None = None) -> bool:
    """Module-level no-network-ish gate for ``github.injection_enabled``."""
    try:
        return default_service(env).can_supply(repo=repo)
    except Exception:
        return False


def posture(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Detection-side posture for ``sbx init``/``doctor``: names/counts only."""
    try:
        service = default_service(env)
        records = service._records()
        return {
            "configured": service.config.configured,
            "installable": service.config.installable,
            "app_id": service.config.app_id or None,
            "installations": len(records),
        }
    except Exception:
        return {"configured": False, "installable": False, "app_id": None, "installations": 0}


__all__ = [
    "APP_API_URL_ENV",
    "APP_DICT_ENV",
    "APP_ID_ENV",
    "APP_KEY_ENV",
    "APP_SECRET_NAME_ENV",
    "APP_SLUG_ENV",
    "APP_STORE_DIR_ENV",
    "AUTHORIZE_STATE_TTL_S",
    "DEFAULT_API_URL",
    "DEFAULT_DICT_NAME",
    "FileGitHubAppStore",
    "GitHubAppClient",
    "GitHubAppConfig",
    "GitHubAppError",
    "GitHubAppService",
    "GitHubAppStore",
    "InMemoryGitHubAppStore",
    "InstallationRecord",
    "ModalDictGitHubAppStore",
    "can_supply",
    "default_service",
    "posture",
    "record_from_dict",
    "record_to_dict",
    "reset_default_service",
    "sandbox_token",
]
