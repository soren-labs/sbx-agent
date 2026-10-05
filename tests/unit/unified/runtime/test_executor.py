from types import SimpleNamespace

from control.executors.modal import ModalExecutor
from control.executors.port import AllocationSpec


def test_modal_lost_allocate_response_discovers_same_effect_and_user_client():
    client = object()
    calls = []
    sandboxes = []

    class FakeSandbox:
        @staticmethod
        def list(**kwargs):
            assert kwargs["client"] is client
            return sandboxes

        @staticmethod
        def create(*args, **kwargs):
            assert kwargs["client"] is client
            assert kwargs["secrets"] == []
            assert (
                not {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "DATABASE_URL"} & kwargs["env"].keys()
            )
            calls.append(kwargs)
            sandboxes.append(SimpleNamespace(object_id="opaque_handle"))
            raise OSError("response dropped after allocation")

    class FakeClient:
        @staticmethod
        def from_credentials(token_id, token_secret):
            assert token_id == token_secret == "REDACTED"
            return client

    sdk = SimpleNamespace(
        Client=FakeClient,
        Sandbox=FakeSandbox,
        App=SimpleNamespace(lookup=lambda *a, **kw: SimpleNamespace(app_id="app_test")),
    )
    backend = ModalExecutor(
        {"token_id": "REDACTED", "token_secret": "REDACTED"}, "REDACTED", sdk=sdk
    )
    backend.image = lambda: "image_test"
    spec = AllocationSpec("wsp_one", "sess_one", "lease_one", 1, "digest", "REDACTED")
    assert backend.allocate(spec, "effect_one") == "opaque_handle"
    assert backend.allocate(spec, "effect_one") == "opaque_handle"
    assert len(calls) == 1
    assert calls[0]["tags"]["sbx_effect"] == "effect_one"
    assert not hasattr(backend, "exec")
