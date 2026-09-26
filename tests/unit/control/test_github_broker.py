"""SOR-220: the trusted broker service — challenge-proven deployment
registration, signed install sessions, callback binding, claim exchange,
least-privilege token brokering. The GitHub edge and the challenge fetch
are duck-typed fakes; the service code under test is real (InMemory
store).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import stat
import time
from typing import Any

import pytest
from broker.service import (
    CLAIM_TTL_S,
    REGISTRATION_TTL_S,
    BrokerConfig,
    BrokerError,
    FileBrokerStore,
    GitHubBrokerService,
    InMemoryBrokerStore,
    binding_from_dict,
)

APP_SLUG = "sbx-public"
ORIGIN = "https://sbx.example.com"
REDIRECT = f"{ORIGIN}/v1/github/install/callback"


class FakeGitHub:
    """Duck-typed GitHubAppClient — no network, records mint scope."""

    def __init__(self) -> None:
        self.installations: list[dict[str, Any]] = []
        self.repos: list[str] = ["octo/hello"]
        self.deleted: list[int] = []
        self.minted_scopes: list[list[str] | None] = []

    def list_installations(self) -> list[dict[str, Any]]:
        return list(self.installations)

    def create_installation_token(
        self, installation_id: int, *, repositories: list[str] | None = None
    ) -> tuple[str, float]:
        self.minted_scopes.append(repositories)
        return "ghs_broker_minted", time.time() + 3600  # placeholder, not real

    def installation_repositories(self, token: str) -> tuple[str, list[str]]:
        return "selected", list(self.repos)

    def delete_installation(self, installation_id: int) -> bool:
        self.deleted.append(installation_id)
        return True


@pytest.fixture()
def github() -> FakeGitHub:
    fake = FakeGitHub()
    fake.installations = [
        {
            "id": 88,
            "account": {"login": "octo", "type": "Organization"},
            "repository_selection": "selected",
        }
    ]
    return fake


@pytest.fixture()
def challenges() -> dict[str, str]:
    """Deployment-side challenge endpoints, keyed by fetch URL — the
    broker's ``challenge_fetcher`` reads from this map (no network)."""
    return {}


@pytest.fixture()
def service(github: FakeGitHub, challenges: dict[str, str]) -> GitHubBrokerService:
    return GitHubBrokerService(
        BrokerConfig(app_id="1", slug=APP_SLUG, private_key="not-a-real-pem"),
        InMemoryBrokerStore(),
        github,
        challenge_fetcher=lambda url: challenges.get(url),
    )


def register(
    service: GitHubBrokerService,
    challenges: dict[str, str],
    origin: str = ORIGIN,
) -> str:
    """Full registration handshake → the deployment-scoped credential."""
    reg = service.register_deployment(origin)
    challenges[reg["challenge_url"]] = reg["challenge"]
    return str(service.complete_registration(reg["registration_id"])["credential"])


def begin(
    service: GitHubBrokerService,
    challenges: dict[str, str],
    origin: str = ORIGIN,
) -> dict[str, Any]:
    credential = register(service, challenges, origin)
    return service.begin_install(origin, credential)


class TestRegistration:
    def test_round_trip_issues_deployment_credential(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        credential = register(service, challenges)
        assert credential.startswith("sbxdep_")
        dep = service._store.get_deployment(ORIGIN)
        assert dep["redirect_uri"] == REDIRECT
        # Only the credential's hash is stored — never the plaintext.
        assert credential not in json.dumps(dep)

    def test_challenge_mismatch_or_absent_fails_closed(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        reg = service.register_deployment(ORIGIN)
        # No challenge served → 403, and the registration is consumed.
        with pytest.raises(BrokerError) as exc:
            service.complete_registration(reg["registration_id"])
        assert exc.value.code == "broker_registration"
        assert exc.value.status_code == 403
        # Replay of the same registration_id is dead (single-use).
        challenges[reg["challenge_url"]] = reg["challenge"]
        with pytest.raises(BrokerError) as exc:
            service.complete_registration(reg["registration_id"])
        assert exc.value.code == "broker_registration"

    def test_wrong_challenge_body_fails_closed(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        reg = service.register_deployment(ORIGIN)
        challenges[reg["challenge_url"]] = "attacker-controlled-body"
        with pytest.raises(BrokerError) as exc:
            service.complete_registration(reg["registration_id"])
        assert exc.value.code == "broker_registration"

    def test_rejects_non_https_and_malformed_origins(self, service: GitHubBrokerService) -> None:
        for bad in (
            "",
            "notaurl",
            "file:///etc/passwd",
            "javascript:x",
            "http://sbx.example.com",  # non-loopback http — never registerable
        ):
            with pytest.raises(BrokerError) as exc:
                service.register_deployment(bad)
            assert exc.value.code in {"broker_invalid", "broker_origin"}

    def test_http_loopback_origin_is_the_scoped_exception(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        credential = register(service, challenges, "http://localhost:8080")
        assert credential.startswith("sbxdep_")
        dep = service._store.get_deployment("http://localhost:8080")
        assert dep["redirect_uri"] == ("http://localhost:8080/v1/github/install/callback")

    def test_expired_registration_fails_closed(
        self, github: FakeGitHub, challenges: dict[str, str]
    ) -> None:
        now = [time.time()]
        svc = GitHubBrokerService(
            BrokerConfig(app_id="1", slug=APP_SLUG, private_key="k"),
            InMemoryBrokerStore(),
            github,
            clock=lambda: now[0],
            challenge_fetcher=lambda url: challenges.get(url),
        )
        reg = svc.register_deployment(ORIGIN)
        challenges[reg["challenge_url"]] = reg["challenge"]
        now[0] += REGISTRATION_TTL_S + 1
        with pytest.raises(BrokerError) as exc:
            svc.complete_registration(reg["registration_id"])
        assert exc.value.code == "broker_registration"

    def test_reregistration_rotates_credential(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        old = register(service, challenges)
        new = register(service, challenges)
        assert old != new
        with pytest.raises(BrokerError) as exc:
            service.begin_install(ORIGIN, old)
        assert exc.value.code == "broker_auth"
        assert service.begin_install(ORIGIN, new)["install_url"]


class TestBeginInstall:
    def test_url_is_installations_new_not_apps_new(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        out = begin(service, challenges)
        # The FIRST GitHub page is the App *installation* page — the
        # settings/apps/new registration surface is never the default.
        assert out["install_url"].startswith(
            f"https://github.com/apps/{APP_SLUG}/installations/new?state="
        )
        assert "settings/apps/new" not in out["install_url"]
        assert out["state"].startswith("sbk1.")
        assert out["expires_at"]

    def test_unregistered_origin_fails_closed(self, service: GitHubBrokerService) -> None:
        with pytest.raises(BrokerError) as exc:
            service.begin_install(ORIGIN, "sbxdep_anything")
        assert exc.value.code == "broker_unregistered"
        assert exc.value.status_code == 403

    def test_wrong_credential_fails_closed(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        register(service, challenges)
        with pytest.raises(BrokerError) as exc:
            service.begin_install(ORIGIN, "sbxdep_forged")
        assert exc.value.code == "broker_auth"

    def test_credential_is_origin_bound(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        credential = register(service, challenges)
        other = register(service, challenges, "https://other.example.com")
        # A valid credential minted for one origin opens no session on another.
        with pytest.raises(BrokerError) as exc:
            service.begin_install("https://other.example.com", credential)
        assert exc.value.code == "broker_auth"
        assert service.begin_install("https://other.example.com", other)["install_url"]

    def test_unconfigured_is_503(self, github: FakeGitHub) -> None:
        svc = GitHubBrokerService(BrokerConfig(), InMemoryBrokerStore(), github)
        with pytest.raises(BrokerError) as exc:
            svc.register_deployment(ORIGIN)
        assert exc.value.code == "broker_unconfigured"
        assert exc.value.status_code == 503
        with pytest.raises(BrokerError) as exc:
            svc.begin_install(ORIGIN, "sbxdep_x")
        assert exc.value.code == "broker_unconfigured"


class TestCallbackBinding:
    def test_round_trip_binds_to_initiating_deployment(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        session = begin(service, challenges)
        redirect_uri, code = service.handle_install_callback(88, session["state"])
        assert redirect_uri == REDIRECT
        assert code.startswith("sbxclaim_")

        claimed = service.claim(code)
        inst = claimed["installation"]
        assert inst["installation_id"] == 88
        assert inst["account_login"] == "octo"
        assert inst["repositories"] == ["octo/hello"]
        assert inst["deployment"] == ORIGIN
        assert claimed["credential"].startswith("sbxbrk_")
        # The claim is single-use.
        with pytest.raises(BrokerError) as exc:
            service.claim(code)
        assert exc.value.code == "broker_claim"

    def test_state_is_single_use(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        session = begin(service, challenges)
        service.handle_install_callback(88, session["state"])
        with pytest.raises(BrokerError) as exc:
            service.handle_install_callback(88, session["state"])
        assert exc.value.code == "broker_state"

    def test_forged_and_garbage_states_fail_closed(self, service: GitHubBrokerService) -> None:
        for bad in ("", "junk", "sbk1.a.b", "sbk2.a.b"):
            with pytest.raises(BrokerError) as exc:
                service.handle_install_callback(88, bad)
            assert exc.value.code == "broker_state"

    def test_tampered_payload_fails(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        """Swapping the embedded redirect_uri invalidates the signature —
        the callback can never be turned into an open redirect."""
        begin(service, challenges)
        body = base64.urlsafe_b64encode(
            json.dumps({"ru": "https://evil.example/x", "nonce": "n", "exp": 9999999999}).encode()
        ).decode()
        forged = f"sbk1.{body}.{'0' * 64}"
        with pytest.raises(BrokerError) as exc:
            service.handle_install_callback(88, forged)
        assert exc.value.code == "broker_state"

    def test_unknown_installation_is_404_and_state_still_consumed(
        self,
        service: GitHubBrokerService,
        github: FakeGitHub,
        challenges: dict[str, str],
    ) -> None:
        session = begin(service, challenges)
        with pytest.raises(BrokerError) as exc:
            service.handle_install_callback(999, session["state"])
        assert exc.value.code == "not_found"

    def test_expired_state_fails(self, github: FakeGitHub, challenges: dict[str, str]) -> None:
        now = [time.time()]
        svc = GitHubBrokerService(
            BrokerConfig(app_id="1", slug=APP_SLUG, private_key="k"),
            InMemoryBrokerStore(),
            github,
            clock=lambda: now[0],
            challenge_fetcher=lambda url: challenges.get(url),
        )
        session = begin(svc, challenges)
        now[0] += 700  # past STATE_TTL_S
        with pytest.raises(BrokerError) as exc:
            svc.handle_install_callback(88, session["state"])
        assert exc.value.code == "broker_state"


class TestClaimExpiry:
    """Finding 1: CLAIM_TTL_S must be enforced on redeem, fail-closed, with
    one-time semantics preserved (an expired claim is destroyed on pop and
    can never be replayed)."""

    def _svc(self, github: FakeGitHub, challenges: dict[str, str], now: list[float]):
        return GitHubBrokerService(
            BrokerConfig(app_id="1", slug=APP_SLUG, private_key="k"),
            InMemoryBrokerStore(),
            github,
            clock=lambda: now[0],
            challenge_fetcher=lambda url: challenges.get(url),
        )

    def test_expired_claim_fails_closed_and_cannot_replay(
        self, github: FakeGitHub, challenges: dict[str, str]
    ) -> None:
        now = [time.time()]
        svc = self._svc(github, challenges, now)
        session = begin(svc, challenges)
        _, code = svc.handle_install_callback(88, session["state"])

        now[0] += CLAIM_TTL_S + 1  # past the claim window
        with pytest.raises(BrokerError) as exc:
            svc.claim(code)
        assert exc.value.code == "broker_claim"
        assert exc.value.status_code == 403
        # Expired-and-consumed: replay stays dead even if the clock moved back.
        now[0] -= CLAIM_TTL_S + 1
        with pytest.raises(BrokerError) as exc:
            svc.claim(code)
        assert exc.value.code == "broker_claim"

    def test_claim_within_ttl_still_works(
        self, github: FakeGitHub, challenges: dict[str, str]
    ) -> None:
        now = [time.time()]
        svc = self._svc(github, challenges, now)
        session = begin(svc, challenges)
        _, code = svc.handle_install_callback(88, session["state"])
        now[0] += CLAIM_TTL_S - 60  # inside the window
        claimed = svc.claim(code)
        assert claimed["credential"].startswith("sbxbrk_")
        # And the normal one-time replay guard is unchanged.
        with pytest.raises(BrokerError) as exc:
            svc.claim(code)
        assert exc.value.code == "broker_claim"


class TestTokenBrokering:
    def _bound(self, service: GitHubBrokerService, challenges: dict[str, str]) -> str:
        session = begin(service, challenges)
        _, code = service.handle_install_callback(88, session["state"])
        return service.claim(code)["credential"]

    def test_mint_is_least_privilege(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        credential = self._bound(service, challenges)
        out = service.mint_token(88, credential, repositories=["hello"])
        assert out["token"] == "ghs_broker_minted"
        assert out["expires_at"]

    def test_wrong_credential_fails_closed(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        self._bound(service, challenges)
        with pytest.raises(BrokerError) as exc:
            service.mint_token(88, "sbxbrk_forged")
        assert exc.value.code == "broker_auth"
        with pytest.raises(BrokerError) as exc:
            service.mint_token(999, "sbxbrk_forged")
        assert exc.value.code == "not_found"

    def test_repo_outside_selection_fails_closed(
        self,
        service: GitHubBrokerService,
        github: FakeGitHub,
        challenges: dict[str, str],
    ) -> None:
        credential = self._bound(service, challenges)
        calls_before = len(github.minted_scopes)
        with pytest.raises(BrokerError) as exc:
            service.mint_token(88, credential, repositories=["other-repo"])
        assert exc.value.code == "broker_repo_scope"
        assert len(github.minted_scopes) == calls_before  # upstream never called

    def test_suspended_installation_fails_closed(
        self,
        service: GitHubBrokerService,
        github: FakeGitHub,
        challenges: dict[str, str],
    ) -> None:
        credential = self._bound(service, challenges)
        github.installations[0]["suspended_at"] = "2026-01-01T00:00:00Z"
        service.sync_installation(88, credential)
        with pytest.raises(BrokerError) as exc:
            service.mint_token(88, credential)
        assert exc.value.code == "broker_suspended"

    def test_sync_refreshes_and_gone_install_deletes_binding(
        self,
        service: GitHubBrokerService,
        github: FakeGitHub,
        challenges: dict[str, str],
    ) -> None:
        credential = self._bound(service, challenges)
        github.repos = ["octo/hello", "octo/world"]
        out = service.sync_installation(88, credential)
        assert out["installation"]["repositories"] == ["octo/hello", "octo/world"]
        github.installations = []
        with pytest.raises(BrokerError) as exc:
            service.sync_installation(88, credential)
        assert exc.value.code == "not_found"
        # Binding is gone — even the right credential cannot mint anymore.
        with pytest.raises(BrokerError):
            service.mint_token(88, credential)

    def test_revoke_uninstalls_and_drops_binding(
        self,
        service: GitHubBrokerService,
        github: FakeGitHub,
        challenges: dict[str, str],
    ) -> None:
        credential = self._bound(service, challenges)
        out = service.revoke_installation(88, credential)
        assert out == {"revoked": 1, "remote_deleted": True}
        assert github.deleted == [88]
        with pytest.raises(BrokerError):
            service.mint_token(88, credential)


class TestSecretBoundary:
    def test_no_key_or_credential_material_leaks(
        self, service: GitHubBrokerService, challenges: dict[str, str]
    ) -> None:
        """The App private key never appears in any outbound payload, and
        stored bindings/deployments keep only credential hashes."""
        session = begin(service, challenges)
        redirect_uri, code = service.handle_install_callback(88, session["state"])
        claimed = service.claim(code)
        raw_binding = service._store.get_binding(88)
        blob = json.dumps(
            {
                "session": session,
                "callback": [redirect_uri, code],
                "claimed": claimed,
                "binding_record": raw_binding,
                "deployment_record": service._store.get_deployment(ORIGIN),
                "health": service.health(),
            }
        )
        assert "not-a-real-pem" not in blob
        assert "PRIVATE KEY" not in blob
        assert "credential_hash" in raw_binding
        assert claimed["credential"] not in blob.split("claimed")[0]
        binding = binding_from_dict(raw_binding)
        assert binding is not None and "credential" not in binding.public()

    def test_health_is_metadata_only(self, service: GitHubBrokerService) -> None:
        assert service.health() == {
            "ok": True,
            "configured": True,
            "app_slug": APP_SLUG,
        }


class TestFileStore:
    def test_files_are_0600_and_round_trip(self, tmp_path) -> None:
        store = FileBrokerStore(tmp_path)
        store.put_state("nonce", time.time() + 60)
        assert store.pop_state("nonce") is not None
        store.put_claim("hash", {"installation_id": 1}, time.time() + 60)
        item = store.pop_claim("hash")
        assert item["payload"] == {"installation_id": 1}
        assert item["exp"] > time.time()
        store.put_registration("reg1", {"origin": ORIGIN, "exp": 1.0})
        assert store.pop_registration("reg1") == {"origin": ORIGIN, "exp": 1.0}
        assert store.pop_registration("reg1") is None
        store.put_deployment(ORIGIN, {"redirect_uri": REDIRECT})
        assert store.get_deployment(ORIGIN)["redirect_uri"] == REDIRECT
        store.put_binding({"installation_id": 7, "account_login": "octo"})
        assert store.get_binding(7)["account_login"] == "octo"
        store.delete_binding(7)
        assert store.get_binding(7) is None
        for path in tmp_path.iterdir():
            if path.is_file():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_state_signature_uses_dedicated_secret_when_set(
    github: FakeGitHub, challenges: dict[str, str]
) -> None:
    """SBX_BROKER_STATE_SECRET pins the HMAC key independent of the PEM."""
    cfg = BrokerConfig(app_id="1", slug=APP_SLUG, private_key="pem", state_secret="sekrit")
    svc = GitHubBrokerService(
        cfg,
        InMemoryBrokerStore(),
        github,
        challenge_fetcher=lambda url: challenges.get(url),
    )
    session = begin(svc, challenges)
    _, body, sig = session["state"].split(".", 2)
    expected = hmac.new(b"sekrit", body.encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(sig, expected)
