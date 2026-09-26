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
# SOR-220 default connect: the hosted Sorenforge integration broker. The App
# private key lives ONLY at the broker — this deployment stores installation
# metadata + a revocable per-installation broker credential, and sandboxes
# receive short-lived installation tokens minted through it.
BROKER_URL_ENV = "SBX_GITHUB_BROKER_URL"

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
    # "" (default) = installed on the deployment's own App (env/manifest);
    # "broker" = bound through the hosted Sorenforge broker (SOR-220) — its
    # tokens are minted via the broker's per-installation credential, and the
    # App private key never exists in this deployment.
    via: str = ""

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
            "via": self.via,
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
        "via": record.via,
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
            via=str(data.get("via") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# durable store — installations + pending authorize states


class GitHubAppStore(Protocol):
    """Installations keyed by id, single-use pending authorize/manifest
    states, and the deployment-registered App config (SOR-220)."""

    def list(self) -> list[InstallationRecord]: ...
    def get(self, installation_id: int) -> InstallationRecord | None: ...
    def put(self, record: InstallationRecord) -> None: ...
    def delete(self, installation_id: int) -> None: ...
    def put_state(self, state: str, expires_epoch: float) -> None: ...
    def pop_state(self, state: str) -> float | None:
        """Consume a pending state; returns its expiry epoch, or None."""
        ...

    def get_app_config(self) -> dict[str, Any] | None:
        """The manifest-registered App config (SOR-220), or None."""
        ...

    def put_app_config(self, config: dict[str, Any]) -> None: ...
    def delete_app_config(self) -> None: ...

    # SOR-220 broker lane: the per-installation broker binding —
    # ``{credential, broker_url, deployment, bound_at}``. The credential is a
    # revocable broker-issued bearer scoped to exactly one installation (it
    # can request token mints for it, nothing more); it is NOT the App
    # private key, which never exists in a self-hosted deployment.
    def get_broker_binding(self, installation_id: int) -> dict[str, Any] | None: ...
    def put_broker_binding(self, installation_id: int, binding: dict[str, Any]) -> None: ...
    def delete_broker_binding(self, installation_id: int) -> None: ...


class InMemoryGitHubAppStore:
    def __init__(self) -> None:
        self._items: dict[int, dict[str, Any]] = {}
        self._states: dict[str, float] = {}
        self._app_config: dict[str, Any] | None = None
        self._bindings: dict[int, dict[str, Any]] = {}
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

    def get_app_config(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._app_config) if self._app_config is not None else None

    def put_app_config(self, config: dict[str, Any]) -> None:
        with self._lock:
            self._app_config = dict(config)

    def delete_app_config(self) -> None:
        with self._lock:
            self._app_config = None

    def get_broker_binding(self, installation_id: int) -> dict[str, Any] | None:
        with self._lock:
            raw = self._bindings.get(installation_id)
        return dict(raw) if raw is not None else None

    def put_broker_binding(self, installation_id: int, binding: dict[str, Any]) -> None:
        with self._lock:
            self._bindings[installation_id] = dict(binding)

    def delete_broker_binding(self, installation_id: int) -> None:
        with self._lock:
            self._bindings.pop(installation_id, None)


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

    def _app_config_path(self) -> Path:
        return self._root / "app-config.json"

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

    def get_app_config(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._app_config_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def put_app_config(self, config: dict[str, Any]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._app_config_path()
        tmp = path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(config))
        tmp.replace(path)

    def delete_app_config(self) -> None:
        self._app_config_path().unlink(missing_ok=True)

    def _binding_path(self, installation_id: int) -> Path:
        return self._root / f"broker-binding-{installation_id}.json"

    def get_broker_binding(self, installation_id: int) -> dict[str, Any] | None:
        try:
            data = json.loads(self._binding_path(installation_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def put_broker_binding(self, installation_id: int, binding: dict[str, Any]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._binding_path(installation_id)
        tmp = path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        # 0600 — the broker credential is revocable, installation-scoped
        # bearer material (never the App private key), protected like the
        # manifest registry lane.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(binding))
        tmp.replace(path)

    def delete_broker_binding(self, installation_id: int) -> None:
        self._binding_path(installation_id).unlink(missing_ok=True)


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

    def get_app_config(self) -> dict[str, Any] | None:
        raw = self._d().get("app:config")
        return raw if isinstance(raw, dict) else None

    def put_app_config(self, config: dict[str, Any]) -> None:
        self._d()["app:config"] = dict(config)

    def delete_app_config(self) -> None:
        try:
            self._d().pop("app:config")
        except KeyError:
            pass

    def get_broker_binding(self, installation_id: int) -> dict[str, Any] | None:
        raw = self._d().get(f"binding:{installation_id}")
        return raw if isinstance(raw, dict) else None

    def put_broker_binding(self, installation_id: int, binding: dict[str, Any]) -> None:
        self._d()[f"binding:{installation_id}"] = dict(binding)

    def delete_broker_binding(self, installation_id: int) -> None:
        try:
            self._d().pop(f"binding:{installation_id}")
        except KeyError:
            pass


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
        config_resolver: Callable[[], GitHubAppConfig] | None = None,
    ) -> None:
        self._config = config
        self._config_resolver = config_resolver
        self._api_url = api_url.rstrip("/")
        self._timeout_s = timeout_s
        self._clock = clock
        self._client = httpx.Client(transport=transport, timeout=timeout_s)

    @property
    def config(self) -> GitHubAppConfig:
        return self._config

    def _resolved_config(self) -> GitHubAppConfig:
        """Config in effect *now* — the SOR-220 dynamic resolver picks up a
        manifest-registered app without a service rebuild or redeploy."""
        if self._config_resolver is not None:
            try:
                resolved = self._config_resolver()
            except Exception:
                resolved = None
            if resolved is not None:
                return resolved
        return self._config

    def _jwt(self) -> str:
        """Sign an App JWT (iss=app_id, 10 min bound, 60 s clock-skew backdate)."""
        import jwt  # PyJWT[crypto] — RS256 needs the cryptography extra

        cfg = self._resolved_config()
        now = int(self._clock())
        payload = {"iat": now - 60, "exp": now + 600, "iss": cfg.app_id}
        return jwt.encode(payload, cfg.private_key, algorithm="RS256")

    def _request(
        self,
        method: str,
        path: str,
        *,
        authorization: str | None,
        json_body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
        what: str,
    ) -> Any:
        """One GitHub API call; failures raise ``GitHubAppError`` with a
        clipped, secret-free message (response bodies may echo request
        fragments — never let more than 200 chars through). ``None``
        authorization issues the request unauthenticated (the manifest
        conversion endpoint is anonymous by design)."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if authorization is not None:
            headers["Authorization"] = authorization
        try:
            resp = self._client.request(
                method,
                f"{self._api_url}{path}",
                headers=headers,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise GitHubAppError(
                "github_app_upstream",
                f"GitHub API {what} failed: {type(exc).__name__}",
                status_code=502,
            ) from exc
        if resp.status_code not in expected:
            detail = resp.text[:200].replace(self._resolved_config().app_id or " ", "<app-id>")
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

    def exchange_manifest_code(self, code: str) -> dict[str, Any]:
        """``POST /app-manifests/{code}/conversions`` — SOR-220 registration.

        This endpoint is *unauthenticated* by design: the one-time ``code``
        the browser redirect carries is the credential. Returns the created
        app's full record (``id``/``slug``/``client_id``/``client_secret``/
        ``pem``/``webhook_secret``) — callers must persist it immediately
        and never surface the private fields on the API.
        """
        data = self._request(
            "POST",
            f"/app-manifests/{code}/conversions",
            authorization=None,
            expected=(201,),
            what="exchange manifest code",
        )
        if not isinstance(data, dict) or not data.get("id") or not data.get("pem"):
            raise GitHubAppError(
                "github_app_upstream",
                "GitHub API exchange manifest code returned no app",
                status_code=502,
            )
        return data


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

    SOR-220 zero-config: when the env config is absent, a manifest-
    registered App config stored in the durable registry lane takes over —
    ``_resolve_config`` re-reads it on every call so registration is usable
    immediately, with no redeploy. ``secret_writer`` mirrors the private
    material into the deployment-managed Secret (Modal).
    """

    def __init__(
        self,
        config: GitHubAppConfig,
        store: GitHubAppStore,
        client: GitHubAppClient | None = None,
        *,
        api_url: str = DEFAULT_API_URL,
        clock: Callable[[], float] = time.time,
        records_ttl_s: float = 60.0,
        secret_writer: Any = None,
        secret_name: str | None = None,
        broker: Any = None,
    ) -> None:
        self._env_config = config
        self._store = store
        self._api_url = api_url.rstrip("/")
        self._secret_writer = secret_writer
        self._secret_name = secret_name
        # SOR-220 default path: ``control.github_broker.GitHubBrokerClient``
        # (or a duck-typed fake). Present whenever the broker lane is enabled
        # — it is USED only when no local App is configured.
        self._broker = broker
        self._client = client or GitHubAppClient(
            config, api_url=self._api_url, config_resolver=self._resolve_config
        )
        self._clock = clock
        self._records_ttl_s = records_ttl_s
        self._tokens: dict[tuple[int, str], tuple[str, float]] = {}
        self._records_cache: tuple[float, list[InstallationRecord]] | None = None
        self._health_cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    # -- config resolution (env → registry) --------------------------------

    def _stored_app_record(self) -> dict[str, Any] | None:
        """The manifest-registered config record in the durable store."""
        get = getattr(self._store, "get_app_config", None)
        if not callable(get):
            return None
        try:
            raw = get()
        except Exception:
            return None
        return raw if isinstance(raw, dict) else None

    def _resolve_config(self) -> GitHubAppConfig:
        """The config in effect *now*: env config wins (SOR-177 backcompat),
        else the manifest-registered registry config."""
        if self._env_config.configured:
            return self._env_config
        raw = self._stored_app_record()
        if raw is not None:
            cfg = GitHubAppConfig(
                app_id=str(raw.get("app_id") or ""),
                slug=str(raw.get("slug") or "").lower(),
                private_key=str(raw.get("private_key") or ""),
            )
            if cfg.configured:
                return cfg
        return self._env_config

    def _config_source(self) -> str | None:
        if self._env_config.configured:
            return "env"
        if self._stored_app_record() is not None:
            return "registry"
        if self._broker_bound():
            return "broker"
        return None

    def _broker_bound(self) -> bool:
        """Any recorded installation bound through the hosted broker."""
        try:
            return any(r.via == "broker" for r in self._records())
        except Exception:
            return False

    def _broker_call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Wrap broker client errors as GitHubAppError so the /v1 layer maps
        them uniformly (codes stay ``github_broker_*``)."""
        from control.github_broker import GitHubBrokerError

        try:
            return fn(*args, **kwargs)
        except GitHubBrokerError as exc:
            raise GitHubAppError(
                f"github_broker_{exc.code}", exc.message, status_code=exc.status_code
            ) from exc

    # -- posture -----------------------------------------------------------

    @property
    def config(self) -> GitHubAppConfig:
        return self._resolve_config()

    @property
    def configured(self) -> bool:
        return self._resolve_config().configured

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
        (GH_TOKEN/GITHUB_TOKEN) can still supply tokens. ``source`` names
        where the effective config lives — ``env`` (SOR-177 envs),
        ``registry`` (manifest registration), or ``broker`` (the hosted
        Sorenforge App lane). ``broker`` reports the lane's reference/health
        — non-sensitive metadata only.
        """
        cfg = self._resolve_config()
        stored = self._stored_app_record() or {}
        records = self._records()
        return {
            "configured": cfg.configured or any(r.via == "broker" for r in records),
            "installable": cfg.installable,
            "app_id": cfg.app_id or None,
            "app_slug": cfg.slug or None,
            "source": self._config_source(),
            "app_url": str(stored.get("html_url") or "") or None,
            "broker": self._broker_status(),
            "installations": [r.public() for r in records],
            "bridge_token": any(os.environ.get(name) for name in TOKEN_ENVS),
        }

    def _broker_status(self) -> dict[str, Any] | None:
        """Broker lane reference + cached health probe (60 s TTL; never
        raises — an unreachable broker reports ``healthy: False``)."""
        if self._broker is None:
            return None
        bound = self._broker_bound()
        with self._lock:
            cached = self._health_cache
        if cached is None or self._clock() - cached[0] > 60:
            try:
                probe = self._broker.health()
                fresh = {
                    "healthy": bool(probe.get("ok")),
                    "app_slug": probe.get("app_slug"),
                }
            except Exception:
                fresh = {"healthy": False, "app_slug": None}
            with self._lock:
                self._health_cache = (self._clock(), fresh)
            cached = (self._clock(), fresh)
        return {
            "url": self._broker.base_url,
            "bound": bound,
            "healthy": cached[1].get("healthy"),
            "app_slug": cached[1].get("app_slug"),
        }

    # -- zero-config registration (SOR-220 App Manifest flow) ---------------

    def begin_manifest(
        self,
        *,
        redirect_url: str,
        name: str | None = None,
        org: str | None = None,
        homepage_url: str | None = None,
    ) -> dict[str, Any]:
        """Step 1: return the App manifest + the ``settings/apps/new`` URL.

        The browser POSTs ``manifest`` to ``manifest_url`` (a
        ``application/x-www-form-urlencoded`` form post — GitHub's
        supported registration flow); GitHub then redirects the browser to
        ``redirect_url`` with ``?code=&state=``. The pending ``state`` is
        the single-use capability that proves the callback belongs to this
        registration.
        """
        if self._resolve_config().configured:
            raise GitHubAppError(
                "github_app_configured",
                "a GitHub App is already configured for this deployment",
                status_code=409,
            )
        base = _origin_of(redirect_url)
        if base is None:
            raise GitHubAppError("github_app_invalid", "redirect_url must be an http(s) URL")
        state = _secrets.token_urlsafe(24)
        expires_at = self._clock() + AUTHORIZE_STATE_TTL_S
        self._store.put_state(f"manifest:{state}", expires_at)
        manifest: dict[str, Any] = {
            "name": name or f"sbx-{_secrets.token_hex(3)}",
            "url": homepage_url or base,
            "hook_attributes": {
                "url": f"{base}/v1/github/app/webhook",
                "active": False,
            },
            "redirect_url": redirect_url,
            "description": f"sbx control plane GitHub integration ({base})",
            "public": False,
            # Least privilege for the sandbox seam: clone/push + PRs.
            "default_permissions": {"contents": "write", "pull_requests": "write"},
        }
        web = github_web_url(self._api_url)
        path = f"{web}/organizations/{org}/settings/apps/new" if org else f"{web}/settings/apps/new"
        return {
            "manifest": manifest,
            "manifest_url": f"{path}?state={state}",
            "state": state,
            "expires_at": _iso_from_epoch(expires_at),
        }

    def complete_manifest(self, code: str, state: str | None) -> dict[str, Any]:
        """Step 2: exchange the conversion ``code``, register the App config.

        ``state`` (single-use, from ``begin_manifest``) is the credential —
        the browser redirect cannot carry a Bearer key. The returned app
        material is persisted to the durable store and mirrored into the
        managed Secret (best-effort); the response surfaces metadata only.
        """
        if not code:
            raise GitHubAppError("github_app_invalid", "manifest completion requires a code")
        if not state:
            raise GitHubAppError(
                "github_app_state",
                "manifest completion requires the pending state",
                status_code=403,
            )
        expiry = self._store.pop_state(f"manifest:{state}")
        if expiry is None or expiry < self._clock():
            raise GitHubAppError(
                "github_app_state", "unknown or expired manifest state", status_code=403
            )
        data = self._client.exchange_manifest_code(code)
        record = {
            "app_id": str(data.get("id")),
            "slug": str(data.get("slug") or "").lower(),
            # Private material — stored in the registry lane / managed
            # Secret only, never returned by any API response.
            "private_key": str(data.get("pem") or ""),
            "client_id": str(data.get("client_id") or ""),
            "client_secret": str(data.get("client_secret") or ""),
            "webhook_secret": str(data.get("webhook_secret") or ""),
            "name": str(data.get("name") or ""),
            "html_url": str(data.get("html_url") or ""),
            "registered_at": _iso_from_epoch(self._clock()),
        }
        self._store.put_app_config(record)
        with self._lock:
            # A new App invalidates every cached token/record view.
            self._tokens = {}
            self._records_cache = None
        secret = self._write_manifest_secret(record)
        # Land straight into a usable install surface: re-read installations
        # GitHub already reports (best-effort — a sync miss never undoes the
        # registration; POST /v1/github/app/sync retries it).
        synced = True
        try:
            self.sync()
        except Exception:
            synced = False
        return {
            "app_id": record["app_id"],
            "slug": record["slug"],
            "name": record["name"],
            "html_url": record["html_url"],
            "source": "registry",
            "secret": secret,
            "installations_synced": synced,
        }

    def _write_manifest_secret(self, record: dict[str, Any]) -> str:
        """Best-effort mirror into the managed Modal Secret — the private
        key lands where a redeploy's env config would read it, but the
        registry copy is authoritative. ``skipped``|``refreshed``|``failed``;
        a Secret failure never undoes the registry commit."""
        if self._secret_writer is None or not self._secret_name:
            return "skipped"
        try:
            self._secret_writer.refresh(
                self._secret_name,
                {
                    APP_ID_ENV: record["app_id"],
                    APP_SLUG_ENV: record["slug"],
                    APP_KEY_ENV: record["private_key"],
                },
            )
        except Exception:
            return "failed"
        return "refreshed"

    # -- default connect (SOR-220): brokered public-App install ------------

    def begin_install(self, *, redirect_uri: str) -> dict[str, Any]:
        """The DEFAULT Connect GitHub start — one click, no App creation.

        Mode resolution:
        - ``app``: a deployment-local App is installable (SOR-177 envs or a
          manifest registration) — keep the existing single-App flow.
        - ``broker``: no local App — the hosted broker returns the official
          ``github.com/apps/<public-app>/installations/new`` URL carrying a
          signed, single-use, short-TTL state bound to ``redirect_uri`` (the
          deployment's own ``/v1/github/install/callback``). The browser goes
          DIRECTLY to the install page — never ``settings/apps/new``.
        """
        cfg = self._resolve_config()
        if cfg.installable:
            out = self.begin_authorization()
            out["mode"] = "app"
            return out
        if self._broker is None:
            _raise_unconfigured()
        session = self._broker_call(self._broker.create_session, redirect_uri)
        return {
            "authorize_url": session["install_url"],
            "state": str(session.get("state") or ""),
            "expires_at": str(session.get("expires_at") or ""),
            "mode": "broker",
        }

    def complete_broker(self, code: str) -> InstallationRecord:
        """Broker-mode completion: redeem the one-time claim code the broker
        302'd back with → installation metadata + the per-installation
        credential. Fails closed: unknown/expired/reused codes record
        nothing. The credential lives in the protected binding lane — never
        in responses or agent workspaces."""
        if self._broker is None:
            raise GitHubAppError(
                "github_broker_disabled",
                "the hosted broker lane is disabled (SBX_GITHUB_BROKER_URL=off)",
                status_code=503,
            )
        if not code:
            raise GitHubAppError("github_app_invalid", "broker callback requires a code")
        data = self._broker_call(self._broker.claim, code)
        inst = data.get("installation") if isinstance(data, dict) else None
        if not isinstance(inst, dict):
            raise GitHubAppError(
                "github_broker_error", "broker claim returned no installation", status_code=502
            )
        try:
            iid = int(inst["installation_id"])
        except (KeyError, TypeError, ValueError):
            raise GitHubAppError(
                "github_broker_error", "broker claim returned no installation", status_code=502
            ) from None
        now = _iso_from_epoch(self._clock())
        record = InstallationRecord(
            installation_id=iid,
            account_login=str(inst.get("account_login") or ""),
            account_type=str(inst.get("account_type") or ""),
            repository_selection=str(inst.get("repository_selection") or "selected"),
            repositories=[str(r) for r in (inst.get("repositories") or [])],
            suspended=bool(inst.get("suspended", False)),
            recorded_at=now,
            synced_at=str(inst.get("synced_at") or now),
            via="broker",
        )
        self._store.put(record)
        self._store.put_broker_binding(
            iid,
            {
                "credential": str(data["credential"]),
                "broker_url": self._broker.base_url,
                "deployment": str(inst.get("deployment") or ""),
                "bound_at": now,
            },
        )
        self._invalidate_records()
        return record

    # -- one-click authorization ------------------------------------------

    def begin_authorization(self) -> dict[str, Any]:
        """Create a pending ``state`` and the one-click install URL."""
        cfg = self._resolve_config()
        if not cfg.installable:
            _raise_unconfigured()
        state = _secrets.token_urlsafe(24)
        expires_at = self._clock() + AUTHORIZE_STATE_TTL_S
        self._store.put_state(state, expires_at)
        url = f"{github_web_url(self._api_url)}/apps/{cfg.slug}/installations/new?state={state}"
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
        if not self._resolve_config().configured:
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
        """Re-read all installations from GitHub; drop ones GitHub forgot.

        Broker-bound records refresh through the broker (per-installation
        credential); a broker 404 means the installation is gone — the local
        record AND binding are dropped, fail closed.
        """
        if not self._resolve_config().configured and not self._broker_bound():
            _raise_unconfigured()
        seen: set[int] = set()
        out: list[InstallationRecord] = []
        if self._resolve_config().configured:
            for installation in self._client.list_installations():
                iid = int(installation.get("id") or 0)
                if not iid:
                    continue
                seen.add(iid)
                out.append(self._record_installation(installation))
        for record in self._store.list():
            if record.via == "broker":
                out.append(self._sync_broker_record(record))
            elif record.installation_id not in seen:
                self._store.delete(record.installation_id)
        self._invalidate_records()
        return out

    def _sync_broker_record(self, record: InstallationRecord) -> InstallationRecord | None:
        """Refresh one broker-bound record; None when it must be dropped."""
        binding = self._store.get_broker_binding(record.installation_id)
        credential = str((binding or {}).get("credential") or "")
        if self._broker is None or not credential:
            return None
        try:
            data = self._broker.sync_installation(record.installation_id, credential)
        except Exception as exc:
            from control.github_broker import GitHubBrokerError

            if isinstance(exc, GitHubBrokerError) and exc.code == "not_found":
                self._store.delete(record.installation_id)
                self._store.delete_broker_binding(record.installation_id)
                return None
            raise GitHubAppError(
                "github_broker_error", "broker sync failed", status_code=502
            ) from exc
        inst = data.get("installation") or {}
        record.account_login = str(inst.get("account_login") or record.account_login)
        record.account_type = str(inst.get("account_type") or record.account_type)
        record.repository_selection = str(
            inst.get("repository_selection") or record.repository_selection
        )
        record.repositories = [str(r) for r in (inst.get("repositories") or record.repositories)]
        record.suspended = bool(inst.get("suspended", record.suspended))
        record.synced_at = _iso_from_epoch(self._clock())
        self._store.put(record)
        return record

    def revoke(self, installation_id: int | None = None) -> dict[str, Any]:
        """Revoke installation(s): best-effort upstream uninstall — directly
        for local-App records, through the broker for broker-bound ones —
        then local records + token cache cleared, fail-closed on either side
        so a half-revoked install never keeps working. ``installation_id=None``
        revokes every recorded installation.
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
            if record.via == "broker":
                remote_deleted = self._revoke_broker(record) and remote_deleted
            elif self._resolve_config().configured and not self._client.delete_installation(
                record.installation_id
            ):
                remote_deleted = False
            self._store.delete(record.installation_id)
            self._store.delete_broker_binding(record.installation_id)
            with self._lock:
                self._tokens = {
                    key: value
                    for key, value in self._tokens.items()
                    if key[0] != record.installation_id
                }
                self._records_cache = None
        return {"revoked": len(records), "remote_deleted": remote_deleted}

    def _revoke_broker(self, record: InstallationRecord) -> bool:
        """Best-effort broker-side revoke → upstream uninstall + binding drop."""
        binding = self._store.get_broker_binding(record.installation_id)
        credential = str((binding or {}).get("credential") or "")
        if self._broker is None or not credential:
            return False
        try:
            return bool(
                self._broker.revoke_installation(record.installation_id, credential).get(
                    "remote_deleted"
                )
            )
        except Exception:
            return False

    # -- sandbox token seam -------------------------------------------------

    def installation_for_repo(self, slug: str) -> InstallationRecord | None:
        """The recorded installation authorizing ``owner/repo``, if any."""
        owner = slug.split("/", 1)[0]
        for record in self._records():
            if record.account_login.lower() == owner.lower() and record.authorizes(slug):
                return record
        return None

    def _record_supply_path(self, record: InstallationRecord) -> str | None:
        """Which mint path serves a recorded installation — ``local`` for
        deployment-App records (needs a configured App), ``broker`` for
        broker-bound ones (needs the lane + a stored credential), else None.
        """
        if record.via == "broker":
            binding = self._store.get_broker_binding(record.installation_id)
            if self._broker is None or not (binding or {}).get("credential"):
                return None
            return "broker"
        return "local" if self._resolve_config().configured else None

    def can_supply(self, repo: str | None = None) -> bool:
        """Cheap no-network gate for ``github.injection_enabled``: a recorded
        (repo-authorizing) installation with a live mint path — local App
        or broker-bound."""
        try:
            records = self._records()
        except Exception:
            return False
        slug = _normalize_slug(repo) if repo else None
        if repo and slug is None:
            return False  # non-github.com repo — no App can authorize it
        candidates = (
            [r for r in records if not r.suspended]
            if slug is None
            else [
                r
                for r in records
                if r.account_login.lower() == slug.split("/", 1)[0].lower() and r.authorizes(slug)
            ]
        )
        return any(self._record_supply_path(r) is not None for r in candidates)

    def sandbox_token(self, repo: str | None = None) -> str | None:
        """A short-lived installation token for sandbox injection.

        ``repo`` (an ``owner/repo`` slug or clone URL) selects the authorizing
        installation and narrows the mint to that repo — least privilege when
        the caller knows the target. Without it, the first recorded
        installation's token covers its whole authorized selection.
        ``None`` = no authorized source (fail closed).

        Broker-bound records mint through the broker's per-installation
        credential; a broker ``not_found`` drops the stale record and fails
        closed — repository revocation/selection changes can never leak a
        wider token.
        """
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
        path = self._record_supply_path(record)
        if path is None:
            return None
        key = (record.installation_id, ",".join(sorted(repositories or [])))
        with self._lock:
            cached = self._tokens.get(key)
            if cached is not None and cached[1] - _TOKEN_REFRESH_MARGIN_S > self._clock():
                return cached[0]
        if path == "broker":
            minted = self._broker_mint(record, repositories)
        else:
            minted = self._client.create_installation_token(
                record.installation_id, repositories=repositories
            )
        if minted is None:
            return None
        token, expiry = minted
        with self._lock:
            self._tokens[key] = (token, expiry)
        return token

    def _broker_mint(
        self, record: InstallationRecord, repositories: list[str] | None
    ) -> tuple[str, float] | None:
        """Mint via the broker. Returns ``(token, expiry)`` on success,
        ``None`` fail-closed (a broker 404 also forgets the stale record), or
        re-raises GitHubAppError for non-404 broker failures."""
        from control.github_broker import GitHubBrokerError

        binding = self._store.get_broker_binding(record.installation_id)
        credential = str((binding or {}).get("credential") or "")
        try:
            return self._broker.mint_token(
                record.installation_id, credential, repositories=repositories
            )
        except GitHubBrokerError as exc:
            if exc.code == "not_found":
                self._store.delete(record.installation_id)
                self._store.delete_broker_binding(record.installation_id)
                self._invalidate_records()
                return None
            raise GitHubAppError(
                f"github_broker_{exc.code}", exc.message, status_code=exc.status_code
            ) from exc


def _iso_from_epoch(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _origin_of(url: str) -> str | None:
    """``scheme://host`` of an http(s) URL, else ``None``."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def github_web_url(api_url: str) -> str:
    """Map a GitHub API base onto its web origin (GHE ``/api/v3`` aware)."""
    host = api_url.rstrip("/")
    if host == "https://api.github.com":
        return "https://github.com"
    if host.endswith("/api/v3"):
        return host[: -len("/api/v3")]
    return host


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
    from control.github_broker import broker_url_from_env, client_from_env

    key = (
        config.app_id,
        config.slug,
        bool(config.private_key),
        env.get(APP_DICT_ENV) or DEFAULT_DICT_NAME,
        env.get(APP_STORE_DIR_ENV) or "",
        env.get(APP_API_URL_ENV) or "",
        env.get(BROKER_URL_ENV) or "",
        env.get("SBX_BACKEND") or "",
    )
    with _default_lock:
        if _default_service is not None and _default_key == key:
            return _default_service
        api_url = env.get(APP_API_URL_ENV) or DEFAULT_API_URL
        # SOR-220: on Modal the manifest-registered key material is also
        # mirrored into the deployment-managed Secret (best effort) so a
        # redeploy keeps the app without a manual `modal secret create`.
        secret_writer = None
        secret_name = None
        if env.get("SBX_BACKEND") == "modal":
            from control.credsync import ModalCredentialSecretWriter

            secret_writer = ModalCredentialSecretWriter()
            secret_name = env.get(APP_SECRET_NAME_ENV) or DEFAULT_DICT_NAME
        _default_service = GitHubAppService(
            config,
            _default_store(env),
            api_url=api_url,
            secret_writer=secret_writer,
            secret_name=secret_name,
            broker=client_from_env(env) if broker_url_from_env(env) else None,
        )
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
