import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest
from control.backend import SandboxHandle, SandboxSpec
from control.hosted_auth import HostedAuthError
from control.modal_connection import ModalContext
from control.real_modal import RealModalProvider, UserModalProcess


def context(owner="alice"):
    return ModalContext(
        owner,
        "connection-" + owner,
        {
            "token_id": secrets.token_urlsafe(16),
            "token_secret": "REDACTED",
        },
    )


def test_all_sdk_lookups_receive_user_client_and_no_control_tokens_enter_sandbox():
    calls = []
    clients = {}
    contexts = [context(), context("bob")]

    def client(token_id, token_secret):
        assert token_secret == "REDACTED"
        return clients.setdefault(token_id, object())

    def lookup(*args, **kwargs):
        assert kwargs["client"] in clients.values()
        calls.append(kwargs)
        return SimpleNamespace(app_id="app", hydrate=lambda **kw: lookup(**kw))

    remote = SimpleNamespace(
        object_id="sandbox", get_tags=lambda: spec.tags, poll=lambda: None, terminate=lambda: None
    )

    def create(*args, **kwargs):
        assert kwargs["client"] in clients.values()
        calls.append(kwargs)
        return remote

    sdk = SimpleNamespace(
        Client=SimpleNamespace(from_credentials=client),
        App=SimpleNamespace(lookup=lookup),
        Environment=SimpleNamespace(from_name=lookup),
        Workspace=SimpleNamespace(
            from_context=lambda **kw: SimpleNamespace(
                name="workspace", hydrate=lambda **kw: lookup(**kw)
            )
        ),
        Image=SimpleNamespace(from_id=lookup),
        Sandbox=SimpleNamespace(
            create=create, from_id=lookup, list=lambda **kw: (lookup(**kw),)[:0]
        ),
        exception=SimpleNamespace(NotFoundError=KeyError, ConflictError=IndexError),
    )
    provider = RealModalProvider(sdk=sdk)
    for ctx in contexts:
        assert provider.verify_workspace(ctx) == "workspace"
        assert provider.ensure_namespace(ctx, "workspace") == "workspace/sbx-compute"
        spec = SandboxSpec(
            tags={
                "owner": ctx.user_id,
                "modal_connection": ctx.connection_id,
                "session_id": "agent",
            },
            secrets=["operator-secret"],
            env={
                "MODAL_TOKEN_ID": "REDACTED",
                "MODAL_TOKEN_SECRET": "REDACTED",
                "DATABASE_URL": "REDACTED",
                "RESEND_API_KEY": "REDACTED",
                "safe": "value",
            },
        )
        handle = provider.create(ctx, spec, {"image": "image-id"})
        call = calls[-1]
        assert call["secrets"] == [] and call["env"]["safe"] == "value"
        assert not any(k in call["env"] for k in spec.env if k != "safe")
        assert call["client"] is clients[ctx.credentials["token_id"]]
        assert handle.root == Path("/work")
        assert provider.list(ctx, {}) == []
    assert len(clients) == 2


def test_context_mismatch_is_rejected_before_any_sdk_call():
    ctx = context()
    provider = RealModalProvider(
        sdk=SimpleNamespace(
            exception=SimpleNamespace(NotFoundError=KeyError, ConflictError=IndexError)
        )
    )
    handle = SandboxHandle("sandbox", Path("/work"), {"owner": "bob"})
    for operation in (provider.poll, provider.terminate):
        with pytest.raises(HostedAuthError, match="sandbox_not_found"):
            operation(ctx, handle)


def test_actual_remote_tags_checked_even_if_handle_is_forged():
    ctx = context()
    remote = SimpleNamespace(get_tags=lambda: {"owner": "other-user"})
    sdk = SimpleNamespace(
        Client=SimpleNamespace(from_credentials=lambda *a: object()),
        Sandbox=SimpleNamespace(from_id=lambda *a, **kw: remote),
    )
    handle = SandboxHandle(
        "sandbox",
        Path("/work"),
        {
            "owner": ctx.user_id,
            "modal_connection": ctx.connection_id,
            "session_id": "agent",
        },
    )
    with pytest.raises(HostedAuthError, match="sandbox_not_found"):
        RealModalProvider(sdk=sdk)._sandbox(ctx, handle)


def test_modal_oauth_explicitly_unavailable():
    with pytest.raises(HostedAuthError, match="modal_oauth_not_configured"):
        RealModalProvider().authorization_url("state")


def test_process_splits_partial_chunks_and_never_exposes_stderr():
    process = SimpleNamespace(stdout=iter(["one\ntw", "o\nlast"]), wait=lambda: 1, returncode=1)
    wrapper = UserModalProcess(process, None, "/tmp/pid")
    assert list(wrapper.stdout) == ["one", "two", "last"]
    assert wrapper.wait() == 1
    assert wrapper.stderr_text() == "sandbox command failed"
