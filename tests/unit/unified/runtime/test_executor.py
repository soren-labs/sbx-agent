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

    class FakeImage:
        object_id = "im_test"

        def build(self, app):
            assert app.app_id == "app_test"

    backend.image = FakeImage
    backend.finished_effect = lambda operation: None
    spec = AllocationSpec("wsp_one", "sess_one", "lease_one", 1, "digest", "REDACTED")
    assert backend.allocate(spec, "effect_one") == "opaque_handle"
    assert backend.allocate(spec, "effect_one") == "opaque_handle"
    assert len(calls) == 1
    assert calls[0]["tags"]["sbx_effect"] == "effect_one"
    assert calls[0]["env"]["SBX_IMAGE_DIGEST"] == "im_test"
    assert not hasattr(backend, "exec")


def test_modal_finished_effect_prevents_reallocation_after_daemon_loss():
    from modal_proto import api_pb2

    requests = []

    class Stub:
        async def SandboxList(self, request):
            requests.append(request)
            return api_pb2.SandboxListResponse(
                sandboxes=[api_pb2.SandboxInfo(id="stopped_exact_handle")]
            )

    client = SimpleNamespace(stub=Stub())
    sdk = SimpleNamespace(
        Client=SimpleNamespace(from_credentials=lambda *args: client),
        Sandbox=SimpleNamespace(
            list=lambda **kwargs: [],
            create=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("must not recreate a completed allocation")
            ),
        ),
        App=SimpleNamespace(lookup=lambda *args, **kwargs: SimpleNamespace(app_id="app_test")),
    )
    backend = ModalExecutor(
        {"token_id": "REDACTED", "token_secret": "REDACTED"}, "REDACTED", sdk=sdk
    )
    spec = AllocationSpec("wsp_one", "sess_one", "lease_one", 1, "digest", "REDACTED")
    assert backend.allocate(spec, "effect_one") == "stopped_exact_handle"
    assert requests[0].app_id == "app_test" and requests[0].include_finished
    assert [(tag.tag_name, tag.tag_value) for tag in requests[0].tags] == [
        ("sbx_effect", "effect_one")
    ]
