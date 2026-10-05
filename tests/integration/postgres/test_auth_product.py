"""Product auth lifecycle against real Postgres (RFC 167 §06):
signup → verify → login → cookie/API-key principal → reset → revoke."""

import pytest
from control.application.auth import AuthService, RateLimiter
from control.domain.errors import DomainError
from control.persistence.unit_of_work import SqlUnitOfWork

pytestmark = pytest.mark.integration


@pytest.fixture()
def auth(pg):
    return AuthService(pg, login_limiter=RateLimiter(limit=50, window_s=60))


def _signup(auth, pg, email="a@x.dev", password="correct horse battery"):
    with SqlUnitOfWork(pg) as uow:
        return auth.signup(uow, email=email, password=password)


class TestSignupLogin:
    def test_signup_verify_login(self, pg, auth):
        res = _signup(auth, pg)
        assert res.user_id.startswith("usr_")
        assert res.workspace_id.startswith("wsp_")

        with SqlUnitOfWork(pg) as uow:
            auth.verify_email(uow, token=res.email_verification_token)
            user = uow.users.get(res.user_id)
            assert user["verified_at"] is not None

        with SqlUnitOfWork(pg) as uow:
            login = auth.login(uow, email="a@x.dev", password="correct horse battery")
        assert login.token
        assert login.principal.user_id == res.user_id
        assert res.workspace_id in login.principal.workspace_ids

        with SqlUnitOfWork(pg) as uow:
            again = auth.authenticate_cookie(uow, token=login.token)
            assert again.user_id == res.user_id

    def test_duplicate_email_rejected(self, pg, auth):
        _signup(auth, pg)
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(DomainError) as ei:
                auth.signup(uow, email="A@X.dev", password="whatever long")
            assert ei.value.code == "idempotency_conflict"

    def test_wrong_password_rejected(self, pg, auth):
        _signup(auth, pg)
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(DomainError) as ei:
                auth.login(uow, email="a@x.dev", password="wrong password here")
            assert ei.value.code == "unauthenticated"

    def test_logout_kills_cookie(self, pg, auth):
        _signup(auth, pg)
        with SqlUnitOfWork(pg) as uow:
            login = auth.login(uow, email="a@x.dev", password="correct horse battery")
            auth.logout(uow, login_session_id=login.login_session_id)
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(DomainError):
                auth.authenticate_cookie(uow, token=login.token)

    def test_login_rate_limited(self, pg):
        auth = AuthService(pg, login_limiter=RateLimiter(limit=2, window_s=60))
        _signup(auth, pg)
        with SqlUnitOfWork(pg) as uow:
            for _ in range(2):
                with pytest.raises(DomainError):
                    auth.login(uow, email="a@x.dev", password="nope nope nope")
            with pytest.raises(DomainError) as ei:
                auth.login(uow, email="a@x.dev", password="correct horse battery")
            assert ei.value.code == "rate_limited"


class TestApiKeys:
    def test_key_plaintext_once_then_hash_auth(self, pg, auth):
        res = _signup(auth, pg)
        with SqlUnitOfWork(pg) as uow:
            key = auth.create_api_key(
                uow, user_id=res.user_id, label="ci", scopes=["sessions:write"]
            )
        assert key.plaintext.startswith("sbx_k_")
        with SqlUnitOfWork(pg) as uow:
            row = uow.api_keys.get(key.api_key_id)
            assert key.plaintext not in (row["key_hash"],)
            assert row["key_hash"].startswith("sha256:")
            p = auth.authenticate_api_key(uow, key=key.plaintext)
            assert p.user_id == res.user_id
            assert p.scopes == frozenset({"sessions:write"})

    def test_revoked_key_rejected(self, pg, auth):
        res = _signup(auth, pg)
        with SqlUnitOfWork(pg) as uow:
            key = auth.create_api_key(uow, user_id=res.user_id, label=None, scopes=[])
            auth.revoke_api_key(uow, user_id=res.user_id, api_key_id=key.api_key_id)
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(DomainError):
                auth.authenticate_api_key(uow, key=key.plaintext)

    def test_other_users_key_id_is_404(self, pg, auth):
        a = _signup(auth, pg, email="a@x.dev")
        b = _signup(auth, pg, email="b@x.dev")
        with SqlUnitOfWork(pg) as uow:
            key = auth.create_api_key(uow, user_id=a.user_id, label=None, scopes=[])
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(DomainError) as ei:
                auth.revoke_api_key(uow, user_id=b.user_id, api_key_id=key.api_key_id)
            assert ei.value.code == "not_found"


class TestPasswordReset:
    def test_reset_rotates_auth_epoch_and_kills_sessions(self, pg, auth):
        res = _signup(auth, pg)
        with SqlUnitOfWork(pg) as uow:
            login = auth.login(uow, email="a@x.dev", password="correct horse battery")
        with SqlUnitOfWork(pg) as uow:
            token = auth.request_password_reset(uow, email="a@x.dev")
            assert token
            auth.reset_password(uow, token=token, new_password="new pass phrase!!")
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(DomainError):
                auth.authenticate_cookie(uow, token=login.token)  # stale epoch
            login2 = auth.login(uow, email="a@x.dev", password="new pass phrase!!")
            assert login2.principal.user_id == res.user_id
