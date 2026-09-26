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
- ``register_deployment`` / ``complete_registration`` — a deployment proves
  control of its exact public origin by serving a broker-issued challenge
  at ``/.well-known/sbx-broker-challenge``; on success it receives a
  deployment-scoped ``sbxdep_`` credential and a fixed callback URL.
- ``begin_install(origin, credential)`` — session creation requires the
  deployment credential; the state's ``ru`` is always the *registered*
  callback URL, never requester-supplied (no open client/redirect).
- ``claim`` — the deployment exchanges the one-time code for installation
  metadata + a per-installation broker credential (returned once).
- ``mint_token`` / ``sync_installation`` / ``revoke_installation`` — the
  minimum token-broker API: every call requires the per-installation
  credential and is fail-closed (unknown installation, wrong credential,
  repo outside the recorded selection → error, never a wider token).

No endpoint and no store ever holds the App private key — it exists only
in ``BrokerConfig``. Claims are the one place a *broker-issued* credential
exists at rest: the pending claim payload holds the one-time installation
credential in cleartext until claimed; it is single-use, expires after
``CLAIM_TTL_S`` (enforced on redeem), and stores sweep expired entries on
write. Credentials are persisted hashed (SHA-256) so a store leak cannot
mint tokens or claim sessions. The file store writes every record ``0600``.
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
    _is_loopback_host,
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
# server-to-server immediately after the redirect). Pending deployment
# registrations are equally short-lived.
STATE_TTL_S = 600
CLAIM_TTL_S = 600
REGISTRATION_TTL_S = 600

_STATE_VERSION = "sbk1"
# The challenge a registering deployment must serve to prove origin control.
CHALLENGE_PATH = "/.well-known/sbx-broker-challenge"


def _is_registerable_origin(origin: str) -> bool:
    """Production deployments must be exactly-HTTPS origins; plain http is
    allowed ONLY for loopback (localhost/127.0.0.1/::1) — the explicitly
    scoped exception that keeps local dev and the in-process e2e seam
    working. No wildcard host matching anywhere."""
    parsed = _origin_of(origin)
    if parsed is None:
        return False
    if parsed.startswith("https://"):
        return True
    return parsed.startswith("http://") and _is_loopback_host(parsed)


def _default_challenge_fetch(url: str) -> str | None:
    """GET the deployment's challenge endpoint; None on any failure."""
    import httpx

    try:
        resp = httpx.get(url, timeout=10.0)
    except httpx.HTTPError:
        return None
    return resp.text if resp.status_code == 200 else None


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
    """Pending states/claims/registrations, deployments, bindings.

    ``pop_claim`` returns the stored ``{"payload", "exp"}`` pair — expiry
    is enforced centrally in ``GitHubBrokerService.claim`` so every store
    fails closed identically. Claims carry the one-time credential in
    cleartext for at most ``CLAIM_TTL_S``; implementations must sweep
    expired claims on write."""

    def put_state(self, nonce: str, expires_epoch: float) -> None: ...
    def pop_state(self, nonce: str) -> float | None: ...
    def put_claim(self, code_hash: str, payload: dict[str, Any], expires_epoch: float) -> None: ...
    def pop_claim(self, code_hash: str) -> dict[str, Any] | None: ...
    def put_registration(self, registration_id: str, record: dict[str, Any]) -> None: ...
    def pop_registration(self, registration_id: str) -> dict[str, Any] | None: ...
    def put_deployment(self, origin: str, record: dict[str, Any]) -> None: ...
    def get_deployment(self, origin: str) -> dict[str, Any] | None: ...
    def get_binding(self, installation_id: int) -> dict[str, Any] | None: ...
    def put_binding(self, binding: dict[str, Any]) -> None: ...
    def delete_binding(self, installation_id: int) -> None: ...


class InMemoryBrokerStore:
    def __init__(self) -> None:
        self._states: dict[str, float] = {}
        self._claims: dict[str, dict[str, Any]] = {}
        self._regs: dict[str, dict[str, Any]] = {}
        self._deps: dict[str, dict[str, Any]] = {}
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
            self._claims[code_hash] = {"payload": dict(payload), "exp": float(expires_epoch)}
            self._sweep_claims_locked()

    def _sweep_claims_locked(self) -> None:
        """Drop expired pending claims — the plaintext credential must not
        outlive its TTL at rest."""
        now = time.time()
        for key in [k for k, v in self._claims.items() if float(v.get("exp") or 0) < now]:
            self._claims.pop(key, None)

    def pop_claim(self, code_hash: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._claims.pop(code_hash, None)
        if not isinstance(item, dict):
            return None
        payload = item.get("payload")
        return (
            {"payload": dict(payload), "exp": float(item.get("exp") or 0)}
            if isinstance(payload, dict)
            else None
        )

    def put_registration(self, registration_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._regs[registration_id] = dict(record)

    def pop_registration(self, registration_id: str) -> dict[str, Any] | None:
        with self._lock:
            raw = self._regs.pop(registration_id, None)
        return dict(raw) if isinstance(raw, dict) else None

    def put_deployment(self, origin: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._deps[origin] = dict(record)

    def get_deployment(self, origin: str) -> dict[str, Any] | None:
        with self._lock:
            raw = self._deps.get(origin)
        return dict(raw) if isinstance(raw, dict) else None

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
            # sweep expired pending claims — the plaintext credential must
            # not outlive its TTL at rest
            now = time.time()
            claims = {
                k: v
                for k, v in claims.items()
                if isinstance(v, dict) and float(v.get("exp") or 0) >= now
            }
            claims[code_hash] = {"payload": payload, "exp": float(expires_epoch)}
            self._write("claims.json", claims)

    def pop_claim(self, code_hash: str) -> dict[str, Any] | None:
        with self._lock:
            claims = self._read("claims.json") or {}
            item = claims.pop(code_hash, None)
            if item is not None:
                self._write("claims.json", claims)
        if not isinstance(item, dict):
            return None
        if not isinstance(item.get("payload"), dict):
            return None
        return {"payload": dict(item["payload"]), "exp": float(item.get("exp") or 0)}

    def put_registration(self, registration_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            regs = self._read("registrations.json") or {}
            regs[registration_id] = record
            self._write("registrations.json", regs)

    def pop_registration(self, registration_id: str) -> dict[str, Any] | None:
        with self._lock:
            regs = self._read("registrations.json") or {}
            item = regs.pop(registration_id, None)
            if item is not None:
                self._write("registrations.json", regs)
        return item if isinstance(item, dict) else None

    def put_deployment(self, origin: str, record: dict[str, Any]) -> None:
        with self._lock:
            deps = self._read("deployments.json") or {}
            deps[origin] = record
            self._write("deployments.json", deps)

    def get_deployment(self, origin: str) -> dict[str, Any] | None:
        deps = self._read("deployments.json") or {}
        item = deps.get(origin)
        return item if isinstance(item, dict) else None

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
        self._d()[f"claim:{code_hash}"] = {"payload": payload, "exp": float(expires_epoch)}
        # sweep expired pending claims — the plaintext credential must not
        # outlive its TTL at rest
        now = time.time()
        for key in self._d().keys():
            if not str(key).startswith("claim:"):
                continue
            item = self._d().get(key)
            if isinstance(item, dict) and float(item.get("exp") or 0) < now:
                try:
                    self._d().pop(key)
                except KeyError:
                    pass

    def pop_claim(self, code_hash: str) -> dict[str, Any] | None:
        try:
            item = self._d().pop(f"claim:{code_hash}")
        except KeyError:
            return None
        if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
            return None
        return {"payload": dict(item["payload"]), "exp": float(item.get("exp") or 0)}

    def put_registration(self, registration_id: str, record: dict[str, Any]) -> None:
        self._d()[f"reg:{registration_id}"] = dict(record)

    def pop_registration(self, registration_id: str) -> dict[str, Any] | None:
        try:
            item = self._d().pop(f"reg:{registration_id}")
        except KeyError:
            return None
        return item if isinstance(item, dict) else None

    def put_deployment(self, origin: str, record: dict[str, Any]) -> None:
        self._d()[f"dep:{origin}"] = dict(record)

    def get_deployment(self, origin: str) -> dict[str, Any] | None:
        raw = self._d().get(f"dep:{origin}")
        return raw if isinstance(raw, dict) else None

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
        challenge_fetcher: Callable[[str], str | None] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._api_url = api_url.rstrip("/")
        self._client = client or GitHubAppClient(
            GitHubAppConfig(app_id=config.app_id, slug=config.slug, private_key=config.private_key),
            api_url=self._api_url,
        )
        self._clock = clock
        # How the broker fetches a registering deployment's well-known
        # challenge (injectable for tests; default is a real HTTPS GET).
        self._fetch = challenge_fetcher or _default_challenge_fetch

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

    # -- deployment registration (challenge-proven origin binding) ---------

    def register_deployment(self, origin: str) -> dict[str, Any]:
        """Step 1 of origin binding: issue a single-use registration +
        challenge token. The deployment must serve ``challenge`` at
        ``<origin>/.well-known/sbx-broker-challenge`` — only whoever controls
        the exact HTTPS origin can complete registration, so the broker never
        mints a session credential for an origin the caller doesn't own.
        """
        if not self._config.configured:
            raise _unconfigured()
        parsed = _origin_of(origin or "")
        if parsed is None:
            raise BrokerError("broker_invalid", "origin must be an http(s) URL")
        if not _is_registerable_origin(parsed):
            raise BrokerError("broker_origin", "deployment origin must be https", status_code=403)
        registration_id = secrets.token_urlsafe(16)
        challenge = secrets.token_urlsafe(24)
        self._store.put_registration(
            registration_id,
            {
                "origin": parsed,
                "challenge_hash": hashlib.sha256(challenge.encode()).hexdigest(),
                "exp": self._clock() + REGISTRATION_TTL_S,
            },
        )
        return {
            "registration_id": registration_id,
            "challenge": challenge,
            "challenge_url": f"{parsed}{CHALLENGE_PATH}",
            "expires_at": _iso_from_epoch(self._clock() + REGISTRATION_TTL_S),
        }

    def complete_registration(self, registration_id: str) -> dict[str, Any]:
        """Step 2: fetch the deployment's challenge endpoint over its
        *registered* origin and compare — proof of origin control issues a
        deployment-scoped ``sbxdep_`` credential and pins the callback URL
        ``<origin>/v1/github/install/callback``. Single-use and fail closed:
        a failed fetch consumes the registration."""
        if not self._config.configured:
            raise _unconfigured()
        record = self._store.pop_registration(str(registration_id or ""))
        if record is None or float(record.get("exp") or 0) < self._clock():
            raise BrokerError(
                "broker_registration", "unknown or expired registration", status_code=403
            )
        origin = str(record.get("origin") or "")
        body = self._fetch(f"{origin}{CHALLENGE_PATH}")
        served_hash = hashlib.sha256((body or "").strip().encode()).hexdigest()
        if body is None or not hmac.compare_digest(
            served_hash, str(record.get("challenge_hash") or "")
        ):
            raise BrokerError(
                "broker_registration",
                "deployment did not serve the challenge at its well-known URL",
                status_code=403,
            )
        credential = f"sbxdep_{secrets.token_urlsafe(24)}"
        redirect_uri = f"{origin}/v1/github/install/callback"
        self._store.put_deployment(
            origin,
            {
                "origin": origin,
                "credential_hash": hashlib.sha256(credential.encode()).hexdigest(),
                "redirect_uri": redirect_uri,
                "registered_at": _iso_from_epoch(self._clock()),
            },
        )
        return {"origin": origin, "credential": credential, "redirect_uri": redirect_uri}

    def _deployment(self, origin: str, credential: str) -> dict[str, Any]:
        """Authenticate a session request: exact registered origin + the
        deployment-scoped credential issued at registration."""
        dep = self._store.get_deployment(origin or "")
        if dep is None:
            raise BrokerError(
                "broker_unregistered",
                "deployment origin is not registered",
                status_code=403,
            )
        expected = str(dep.get("credential_hash") or "")
        actual = hashlib.sha256((credential or "").encode()).hexdigest()
        if not credential or not hmac.compare_digest(expected, actual):
            raise BrokerError("broker_auth", "invalid deployment credential", status_code=403)
        return dep

    # -- install session ----------------------------------------------------

    def begin_install(self, origin: str, credential: str) -> dict[str, Any]:
        """Create a signed install session for one *registered* deployment.

        The caller must present the deployment-scoped credential issued by
        ``complete_registration`` for ``origin``; the state's ``ru`` is the
        registered callback URL ``<origin>/v1/github/install/callback`` —
        never requester-supplied, so the post-install 302 can only return to
        the verified deployment (no open redirect, no dynamic client).
        """
        if not self._config.configured:
            raise _unconfigured()
        dep = self._deployment(origin, credential)
        redirect_uri = str(dep.get("redirect_uri") or "")
        if not _is_registerable_origin(redirect_uri):
            raise BrokerError(
                "broker_origin", "registered callback origin is not https", status_code=403
            )
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
        origin = _origin_of(redirect_uri)
        # The redirect target must still be a *registered* deployment's exact
        # callback — a state minted for a since-removed deployment fails here.
        dep = self._store.get_deployment(origin or "") if origin else None
        if origin is None or dep is None or str(dep.get("redirect_uri")) != redirect_uri:
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
        item = self._store.pop_claim(hashlib.sha256(code.encode()).hexdigest())
        # Pop-then-check keeps one-time semantics even when expired: the
        # claim is destroyed either way, so an expired code is dead forever
        # and can never be replayed.
        if item is None or float(item.get("exp") or 0) < self._clock():
            raise BrokerError(
                "broker_claim", "unknown, expired, or already-claimed code", status_code=403
            )
        payload = item["payload"]
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
