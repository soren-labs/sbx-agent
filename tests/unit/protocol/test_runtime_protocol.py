"""Protocol wire-shape invariants (RFC 167 §03): envelope digest identity,
frame codec, enrollment tokens, operation vocabulary."""

from __future__ import annotations

import pytest
from protocol.runtime import (
    OPERATION_TERMINAL,
    OperationEnvelope,
    OperationKind,
    check_token,
    decode_frame,
    encode_frame,
    hello_frame,
    make_frame,
    mint_enrollment_token,
    protocol_compatible,
)

pytestmark = pytest.mark.unit


def _env(**over):
    fields = dict(
        operation_id="eff_01",
        operation_kind=OperationKind.TURN_START,
        session_id="sess_01",
        lease_id="lease_01",
        lease_generation=1,
        grant_id="grt_01",
        grant_expires_at=9999999999.0,
        payload={"prompt": "hi", "turn_id": "turn_01"},
    )
    fields.update(over)
    return OperationEnvelope(**fields)


class TestEnvelope:
    def test_digest_deterministic(self):
        a, b = _env(), _env()
        assert a.request_digest == b.request_digest
        assert a.request_digest.startswith("sha256:")

    def test_digest_changes_with_body(self):
        assert (
            _env(payload={"prompt": "a"}).request_digest
            != _env(payload={"prompt": "b"}).request_digest
        )

    def test_roundtrip(self):
        e = _env()
        back = OperationEnvelope.from_frame(e.to_dict())
        assert back.operation_id == e.operation_id
        assert back.operation_kind == e.operation_kind
        assert back.request_digest == e.request_digest
        assert back.payload["prompt"] == "hi"


class TestFrames:
    def test_codec(self):
        frame = make_frame("operation.submit", operation_id="eff_1", x=[1, 2])
        line = encode_frame(frame)
        assert line.endswith(b"\n")
        assert decode_frame(line) == frame

    def test_decode_requires_frame_key(self):
        with pytest.raises(ValueError):
            decode_frame(b'{"x": 1}\n')

    def test_hello_shape(self):
        h = hello_frame(
            lease_id="lease_1",
            lease_generation=2,
            runtime_epoch="e1",
            token="tok",
            image_digest="sha256:abc",
            runtime_build="test",
            harness_manifests=[],
            recovered_operations=["eff_1"],
            spool_watermark=3,
            health={"ok": True},
        )
        assert h["frame"] == "hello"
        assert h["lease_id"] == "lease_1"
        assert h["enrollment_token"] == "tok"
        assert h["protocol"]["major"] >= 1
        assert h["spool_watermark"] == 3

    def test_protocol_compatible(self):
        from protocol.runtime import PROTOCOL_MAJOR

        assert protocol_compatible(PROTOCOL_MAJOR, 0)
        assert not protocol_compatible(PROTOCOL_MAJOR + 1, 0)


class TestEnrollmentTokens:
    def test_verify(self):
        from protocol.runtime import token_digest

        tok = mint_enrollment_token()
        assert check_token(tok, token_digest(tok))
        assert not check_token("wrong-token", token_digest(tok))

    def test_unique(self):
        assert mint_enrollment_token() != mint_enrollment_token()


class TestVocabulary:
    def test_terminal_states(self):
        assert OPERATION_TERMINAL == {"succeeded", "failed", "interrupted", "unknown"}

    def test_kind_names_dotted(self):
        assert OperationKind.TURN_START.value == "turn.start"
        assert OperationKind.FILES_WRITE.value == "files.write"
