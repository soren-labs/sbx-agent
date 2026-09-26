"""Broker core (SOR-220): signed install sessions, bindings, token brokering.

This module is the trusted half of the default one-click GitHub connect. It
is the *only* place the pre-registered public Sorenforge GitHub App's private
key may exist at runtime — it arrives via ``SBX_BROKER_APP_PRIVATE_KEY`` and
never leaves ``BrokerConfig``. Everything the broker returns is either public
metadata (installation id, account login, repo coverage) or freshly minted,
short-lived, least-privilege material (a single-use claim code, a
per-installation broker credential, a GitHub installation access token).

Flow:

- ``begin_install(redirect_uri)`` — for a calling SBX deployment; returns the
  official ``github.com/apps/<slug>/installations/new`` URL carrying an
  HMAC-signed, single-use, short-TTL ``state`` that pins the flow to *that*
  deployment's callback URL.
- ``handle_install_callback`` — GitHub's post-install redirect target (the
  App's Setup URL). Fails closed on any signature/TTL/replay violation, then
  records the binding and returns ``(redirect_uri, claim_code)`` for a 302
  back to the originating deployment.
- ``claim`` — the deployment exchanges the one-time code for installation
  metadata + a per-installation broker credential (returned once).
- ``mint_token`` / ``sync_installation`` / ``revoke_installation`` — the
  minimum token-broker API: every call requires the per-installation
  credential and is fail-closed (unknown installation, wrong credential,
  repo outside the recorded selection → error, never a wider token).

Stores keep pending states, pending claims, and bindings only — no key
material. The file store writes every record ``0600``; the credential is
persisted hashed (SHA-256) so a store leak cannot mint tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from control.github_app import (
    DEFAULT_API_URL,
    GitHubAppClient,
    GitHubAppConfig,
    _iso_from_epoch,
    _origin_of,
    github_web_url,
)

BROKER_APP_ID_ENV = "SBX_BROKER_APP_ID"
BROKER_APP_SLUG_ENV = "SBX_BROKER_APP_SLUG"
BROKER_APP_KEY_ENV = "SBX_BROKER_APP_PRIVATE_KEY"
BROKER_STATE_SECRET_ENV = "SBX_BROKER_STATE_SECRET"
BROKER_API_URL_ENV = "SBX_BROKER_API_URL"
BROKER_STORE_DIR_ENV = "SBX_BROKER_STORE_DIR"
# Modal deployment: name of the ``modal.Dict`` that holds states, one-time
# claims, and per-installation bindings durably across container churn.
BROKER_STORE_DICT_ENV = "SBX_BROKER_STORE_DICT"
DEFAULT_STORE_DICT = "sbx-github-broker-store"

# Install sessions die fast — the operator completes the GitHub install page
# inside this window; claim codes the same (the deployment redeems them
# server-to-server immediately after the redirect).
STATE_TTL_S = 600
CLAIM_TTL_S = 600

_STATE_VERSION = "sbk1"


class BrokerError(Exception):
    """Failures the HTTP layer maps onto ``{error:{code,message}}``."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _unconfigured() -> BrokerError:
    return BrokerError(
        "broker_unconfigured",
        "broker is not configured — set SBX_BROKER_APP_ID, "
        "SBX_BROKER_APP_SLUG and SBX_BROKER_APP_PRIVATE_KEY",
        status_code=503,
    )


@dataclass(frozen=True)
class BrokerConfig:
    """Resolved broker config — the App private key never leaves this type."""

    app_id: str = ""
    slug: str = ""
    private_key: str = ""
    state_secret: str = ""

    @property
    def configured(self) -> bool:
        return all(s.strip() for s in (self.app_id, self.slug, self.private_key))

    @property
    def signing_key(self) -> bytes:
        """HMAC key for install states. A dedicated secret when the operator
        sets one, else derived from the App key — anyone holding the App key
        already holds the whole trust domain, so derivation adds no exposure.
        """
        if self.state_secret.strip():
            return self.state_secret.encode()
        return hashlib.sha256(self.private_key.encode()).digest()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BrokerConfig:
        env = os.environ if env is None else env
        key = (env.get(BROKER_APP_KEY_ENV) or "").strip()
        if "\\n" in key:
            key = key.replace("\\n", "\n")
        return cls(
            app_id=(env.get(BROKER_APP_ID_ENV) or "").strip(),
            slug=(env.get(BROKER_APP_SLUG_ENV) or "").strip().lower(),
            private_key=key,
            state_secret=(env.get(BROKER_STATE_SECRET_ENV) or "").strip(),
        )


@dataclass
class Binding:
    """The association between one GitHub installation and one deployment.

    ``credential`` is stored only as a SHA-256 hash — the plaintext crosses
    the boundary exactly once, inside the single-use claim exchange.
    """

    installation_id: int
    account_login: str
    account_type: str
    repository_selection: str
    repositories: list[str] = field(default_factory=list)
    credential_hash: str = ""
    redirect_uri: str = ""
    deployment: str = ""
    suspended: bool = False
    bound_at: str = ""
    synced_at: str = ""

    def credential_ok(self, credential: str) -> bool:
        return hmac.compare_digest(
            self.credential_hash, hashlib.sha256(credential.encode()).hexdigest()
        )

    def covers(self, repositories: list[str]) -> bool:
        """Fail-closed repo check for token mints: ``selected`` bindings only
        cover recorded repo names; ``all`` covers the account's namespace —
        GitHub itself rejects anything outside the installation anyway."""
        if not repositories:
            return True
        if self.suspended:
            return False
        if self.repository_selection == "all":
            return True
        allowed = {r.split("/", 1)[1].lower() for r in self.repositories if "/" in r}
        return all(name.lower() in allowed for name in repositories)

    def public(self) -> dict[str, Any]:
        """Claimable metadata — never the credential hash."""
        return {
            "installation_id": self.installation_id,
            "account_login": self.account_login,
            "account_type": self.account_type,
            "repository_selection": self.repository_selection,
            "repositories": list(self.repositories),
            "suspended": self.suspended,
            "deployment": self.deployment,
            "bound_at": self.bound_at,
            "synced_at": self.synced_at,
        }


def binding_to_dict(b: Binding) -> dict[str, Any]:
    return {
        "installation_id": b.installation_id,
        "account_login": b.account_login,
        "account_type": b.account_type,
        "repository_selection": b.repository_selection,
        "repositories": list(b.repositories),
        "credential_hash": b.credential_hash,
        "redirect_uri": b.redirect_uri,
        "deployment": b.deployment,
        "suspended": b.suspended,
        "bound_at": b.bound_at,
        "synced_at": b.synced_at,
    }


def binding_from_dict(data: Any) -> Binding | None:
    if not isinstance(data, dict):
        return None
    try:
        return Binding(
            installation_id=int(data["installation_id"]),
            account_login=str(data.get("account_login") or ""),
            account_type=str(data.get("account_type") or ""),
            repository_selection=str(data.get("repository_selection") or "selected"),
            repositories=[str(r) for r in (data.get("repositories") or [])],
            credential_hash=str(data.get("credential_hash") or ""),
            redirect_uri=str(data.get("redirect_uri") or ""),
            deployment=str(data.get("deployment") or ""),
            suspended=bool(data.get("suspended", False)),
            bound_at=str(data.get("bound_at") or ""),
            synced_at=str(data.get("synced_at") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# durable store — pending states, pending claims, bindings (never key material)


class BrokerStore(Protocol):
    def put_state(self, nonce: str, expires_epoch: float) -> None: ...
    def pop_state(self, nonce: str) -> float | None: ...
    def put_claim(self, code_hash: str, payload: dict[str, Any], expires_epoch: float) -> None: ...
    def pop_claim(self, code_hash: str) -> dict[str, Any] | None: ...
    def get_binding(self, installation_id: int) -> dict[str, Any] | None: ...
    def put_binding(self, binding: dict[str, Any]) -> None: ...
    def delete_binding(self, installation_id: int) -> None: ...


class InMemoryBrokerStore:
    def __init__(self) -> None:
        self._states: dict[str, float] = {}
        self._claims: dict[str, tuple[dict[str, Any], float]] = {}
        self._bindings: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put_state(self, nonce: str, expires_epoch: float) -> None:
        with self._lock:
            self._states[nonce] = expires_epoch

    def pop_state(self, nonce: str) -> float | None:
        with self._lock:
            return self._states.pop(nonce, None)

    def put_claim(self, code_hash: str, payload: dict[str, Any], expires_epoch: float) -> None:
        with self._lock:
            self._claims[code_hash] = (dict(payload), expires_epoch)

    def pop_claim(self, code_hash: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._claims.pop(code_hash, None)
        return dict(item[0]) if item is not None else None

    def get_binding(self, installation_id: int) -> dict[str, Any] | None:
        with self._lock:
            raw = self._bindings.get(installation_id)
        return dict(raw) if raw is not None else None

    def put_binding(self, binding: dict[str, Any]) -> None:
        with self._lock:
            self._bindings[int(binding["installation_id"])] = dict(binding)

    def delete_binding(self, installation_id: int) -> None:
        with self._lock:
            self._bindings.pop(installation_id, None)


class FileBrokerStore:
    """JSON files under ``$SBX_BROKER_STORE_DIR`` (``0600``) — survives broker
    restarts so an install in flight isn't lost on redeploy."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _write(self, name: str, data: Any) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / name
        tmp = path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
        tmp.replace(path)

    def _read(self, name: str) -> Any:
        try:
            return json.loads((self._root / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put_state(self, nonce: str, expires_epoch: float) -> None:
        with self._lock:
            states = self._read("states.json") or {}
            states[nonce] = expires_epoch
            self._write("states.json", states)

    def pop_state(self, nonce: str) -> float | None:
        with self._lock:
            states = self._read("states.json") or {}
            expiry = states.pop(nonce, None)
            if expiry is not None:
                self._write("states.json", states)
            return float(expiry) if expiry is not None else None

    def put_claim(self, code_hash: str, payload: dict[str, Any], expires_epoch: float) -> None:
        with self._lock:
            claims = self._read("claims.json") or {}
            claims[code_hash] = {"payload": payload, "exp": expires_epoch}
            self._write("claims.json", claims)

    def pop_claim(self, code_hash: str) -> dict[str, Any] | None:
        with self._lock:
            claims = self._read("claims.json") or {}
            item = claims.pop(code_hash, None)
            if item is not None:
                self._write("claims.json", claims)
        if not isinstance(item, dict):
            return None
        return item.get("payload") if isinstance(item.get("payload"), dict) else None

    def get_binding(self, installation_id: int) -> dict[str, Any] | None:
        data = self._read(f"binding-{installation_id}.json")
        return data if isinstance(data, dict) else None

    def put_binding(self, binding: dict[str, Any]) -> None:
        self._write(f"binding-{int(binding['installation_id'])}.json", binding)

    def delete_binding(self, installation_id: int) -> None:
        (self._root / f"binding-{installation_id}.json").unlink(missing_ok=True)


class ModalDictBrokerStore:
    """Production store backed by ``modal.Dict`` — lazy-imports modal.

    Modal containers are ephemeral; a file store inside one loses every
    in-flight install state and every deployment binding on restart. The
    Dict is the broker's only durable state: states/claims are already
    hashed, and bindings store the credential *hash* — the plaintext
    credential and the App private key are never stored anywhere.
    """

    def __init__(self, name: str = DEFAULT_STORE_DICT) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def put_state(self, nonce: str, expires_epoch: float) -> None:
        self._d()[f"state:{nonce}"] = expires_epoch

    def pop_state(self, nonce: str) -> float | None:
        try:
            expiry = self._d().pop(f"state:{nonce}")
        except KeyError:
            return None
        return float(expiry)

    def put_claim(self, code_hash: str, payload: dict[str, Any], expires_epoch: float) -> None:
        self._d()[f"claim:{code_hash}"] = {"payload": payload, "exp": expires_epoch}

    def pop_claim(self, code_hash: str) -> dict[str, Any] | None:
        try:
            item = self._d().pop(f"claim:{code_hash}")
        except KeyError:
            return None
        if not isinstance(item, dict):
            return None
        return item.get("payload") if isinstance(item.get("payload"), dict) else None

    def get_binding(self, installation_id: int) -> dict[str, Any] | None:
        raw = self._d().get(f"binding:{installation_id}")
        return raw if isinstance(raw, dict) else None

    def put_binding(self, binding: dict[str, Any]) -> None:
        self._d()[f"binding:{int(binding['installation_id'])}"] = dict(binding)

    def delete_binding(self, installation_id: int) -> None:
        try:
            self._d().pop(f"binding:{installation_id}")
        except KeyError:
            pass


# ---------------------------------------------------------------------------
# service


class GitHubBrokerService:
    """Orchestrates session signing, install callbacks, and token brokering.

    ``github`` is the shared SOR-177 client (App JWT + installation access
    tokens) — the broker adds exactly what self-hosting cannot provide:
    custody of the public App's private key plus the signed binding between
    an installation and the deployment that initiated it.
    """

    def __init__(
        self,
        config: BrokerConfig,
        store: BrokerStore,
        client: GitHubAppClient | None = None,
        *,
        api_url: str = DEFAULT_API_URL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._store = store
        self._api_url = api_url.rstrip("/")
        self._client = client or GitHubAppClient(
            GitHubAppConfig(app_id=config.app_id, slug=config.slug, private_key=config.private_key),
            api_url=self._api_url,
        )
        self._clock = clock

    # -- state signing ------------------------------------------------------

    def _sign_state(self, payload: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode()
        sig = hmac.new(self._config.signing_key, body.encode(), hashlib.sha256).hexdigest()
        return f"{_STATE_VERSION}.{body}.{sig}"

    def _verify_state(self, state: str) -> dict[str, Any]:
        """Fail closed: malformed, forged, expired, or replayed states all
        raise the same ``broker_state`` — the callback never hints which
        check failed."""
        try:
            version, body, sig = state.split(".", 2)
        except ValueError:
            raise BrokerError("broker_state", "malformed state", status_code=403) from None
        if version != _STATE_VERSION:
            raise BrokerError("broker_state", "unknown state version", status_code=403)
        expected = hmac.new(self._config.signing_key, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise BrokerError("broker_state", "bad state signature", status_code=403)
        try:
            payload = json.loads(base64.urlsafe_b64decode(body.encode()))
        except (ValueError, TypeError):
            raise BrokerError("broker_state", "unreadable state", status_code=403) from None
        if not isinstance(payload, dict):
            raise BrokerError("broker_state", "unreadable state", status_code=403)
        nonce = str(payload.get("nonce") or "")
        exp = float(payload.get("exp") or 0)
        if exp < self._clock():
            raise BrokerError("broker_state", "expired state", status_code=403)
        # Single use: the nonce must still be pending — consuming it here
        # means a replayed callback can never bind twice.
        pending = self._store.pop_state(nonce)
        if pending is None or pending < self._clock():
            raise BrokerError("broker_state", "unknown or consumed state", status_code=403)
        return payload

    # -- install session ----------------------------------------------------

    def begin_install(self, redirect_uri: str) -> dict[str, Any]:
        """Create a signed install session for one SBX deployment.

        ``redirect_uri`` is the deployment's own ``/v1/github/install/callback``
        URL — it is baked into the signed state, so the post-install 302 can
        only ever return to the deployment that started the flow (no open
        redirect, no cross-deployment session swap).
        """
        if not self._config.configured:
            raise _unconfigured()
        if _origin_of(redirect_uri) is None:
            raise BrokerError("broker_invalid", "redirect_uri must be an http(s) URL")
        nonce = secrets.token_urlsafe(16)
        expires_at = self._clock() + STATE_TTL_S
        self._store.put_state(nonce, expires_at)
        state = self._sign_state({"ru": redirect_uri, "nonce": nonce, "exp": int(expires_at)})
        web = github_web_url(self._api_url)
        return {
            "install_url": f"{web}/apps/{self._config.slug}/installations/new?state={state}",
            "state": state,
            "expires_at": _iso_from_epoch(expires_at),
        }

    # -- GitHub post-install callback ----------------------------------------

    def handle_install_callback(self, installation_id: Any, state: str) -> tuple[str, str]:
        """Verify ``state``, bind the installation to the deployment it
        names, and return ``(redirect_uri, one_time_claim_code)``.

        Fails closed at every step: a bad state never reaches GitHub calls
        and a installation not reported for *this* App never gets bound.
        """
        if not self._config.configured:
            raise _unconfigured()
        try:
            iid = int(installation_id)
        except (TypeError, ValueError):
            raise BrokerError(
                "broker_invalid", "callback requires a numeric installation_id"
            ) from None
        payload = self._verify_state(state)
        redirect_uri = str(payload.get("ru") or "")
        if _origin_of(redirect_uri) is None:
            raise BrokerError("broker_state", "state carries no deployment", status_code=403)

        binding = self._read_installation(iid)
        if binding is None:
            raise BrokerError(
                "not_found", "installation not reported by GitHub for this app", status_code=404
            )
        credential = f"sbxbrk_{secrets.token_urlsafe(24)}"
        code = f"sbxclaim_{secrets.token_urlsafe(24)}"
        binding.credential_hash = hashlib.sha256(credential.encode()).hexdigest()
        binding.redirect_uri = redirect_uri
        binding.deployment = _origin_of(redirect_uri) or ""
        binding.bound_at = _iso_from_epoch(self._clock())
        binding.synced_at = binding.bound_at
        self._store.put_binding(binding_to_dict(binding))
        self._store.put_claim(
            hashlib.sha256(code.encode()).hexdigest(),
            {"installation_id": iid, "credential": credential},
            self._clock() + CLAIM_TTL_S,
        )
        return redirect_uri, code

    def _read_installation(self, installation_id: int) -> Binding | None:
        """Fetch the installation + its repo selection from GitHub (JWT).

        The minted enumeration token is used in-line and discarded — the
        record keeps metadata only."""
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
        account = installation.get("account") or {}
        selection = str(installation.get("repository_selection") or "selected")
        repos: list[str] = []
        if selection == "selected":
            token, _ = self._client.create_installation_token(installation_id)
            _, repos = self._client.installation_repositories(token)
        return Binding(
            installation_id=installation_id,
            account_login=str(account.get("login") or ""),
            account_type=str(account.get("type") or ""),
            repository_selection=selection,
            repositories=repos,
            suspended=bool(installation.get("suspended_at")),
        )

    # -- claim (server-to-server, one-time) ----------------------------------

    def claim(self, code: str) -> dict[str, Any]:
        """Redeem a one-time claim code → installation metadata + the
        per-installation broker credential (shown exactly once)."""
        if not code:
            raise BrokerError("broker_invalid", "claim requires a code")
        payload = self._store.pop_claim(hashlib.sha256(code.encode()).hexdigest())
        if payload is None:
            raise BrokerError(
                "broker_claim", "unknown, expired, or already-claimed code", status_code=403
            )
        binding = binding_from_dict(self._store.get_binding(int(payload["installation_id"])))
        if binding is None:
            raise BrokerError("not_found", "installation binding is gone", status_code=404)
        return {
            "installation": binding.public(),
            "credential": str(payload["credential"]),
        }

    # -- token brokering (the only sensitive surface) -------------------------

    def _bound(self, installation_id: int, credential: str) -> Binding:
        """Load the binding and prove the caller holds its credential."""
        binding = binding_from_dict(self._store.get_binding(installation_id))
        if binding is None:
            raise BrokerError("not_found", "no binding for this installation", status_code=404)
        if not credential or not binding.credential_ok(credential):
            raise BrokerError("broker_auth", "invalid broker credential", status_code=403)
        return binding

    def mint_token(
        self,
        installation_id: int,
        credential: str,
        *,
        repositories: list[str] | None = None,
    ) -> dict[str, Any]:
        """Mint a short-lived GitHub installation access token.

        ``repositories`` (bare repo *names*, the shape GitHub's access_tokens
        API expects) narrows the mint — it can never widen past the recorded
        selection, and GitHub rejects anything outside the installation
        regardless. The token is returned to the requesting deployment and
        never persisted here.
        """
        if not self._config.configured:
            raise _unconfigured()
        binding = self._bound(installation_id, credential)
        if binding.suspended:
            raise BrokerError("broker_suspended", "installation is suspended", status_code=403)
        if not binding.covers(repositories or []):
            raise BrokerError(
                "broker_repo_scope",
                "requested repositories are outside the installation's selection",
                status_code=403,
            )
        try:
            token, expiry = self._client.create_installation_token(
                installation_id, repositories=repositories or None
            )
        except Exception as exc:
            raise BrokerError(
                "broker_upstream", "GitHub token mint failed", status_code=502
            ) from exc
        return {"token": token, "expires_at": _iso_from_epoch(expiry)}

    def sync_installation(self, installation_id: int, credential: str) -> dict[str, Any]:
        """Re-read the installation from GitHub and refresh the recorded
        coverage — repo-selection changes on GitHub propagate here, and a
        deleted installation deletes the binding (fail closed)."""
        if not self._config.configured:
            raise _unconfigured()
        self._bound(installation_id, credential)
        fresh = self._read_installation(installation_id)
        if fresh is None:
            self._store.delete_binding(installation_id)
            raise BrokerError("not_found", "installation no longer exists", status_code=404)
        existing = self._bound(installation_id, credential)
        merged = {**binding_to_dict(existing), **binding_to_dict(fresh)}
        merged["credential_hash"] = existing.credential_hash
        merged["redirect_uri"] = existing.redirect_uri
        merged["deployment"] = existing.deployment
        merged["bound_at"] = existing.bound_at
        merged["synced_at"] = _iso_from_epoch(self._clock())
        self._store.put_binding(merged)
        out = binding_from_dict(merged)
        assert out is not None
        return {"installation": out.public()}

    def revoke_installation(self, installation_id: int, credential: str) -> dict[str, Any]:
        """Best-effort upstream uninstall, then always drop the binding —
        a half-revoked install can never keep minting."""
        if not self._config.configured:
            raise _unconfigured()
        self._bound(installation_id, credential)
        remote_deleted = self._client.delete_installation(installation_id)
        self._store.delete_binding(installation_id)
        return {"revoked": 1, "remote_deleted": remote_deleted}

    # -- posture --------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Public liveness — configuration posture only, never key material."""
        return {
            "ok": True,
            "configured": self._config.configured,
            "app_slug": self._config.slug or None,
        }
