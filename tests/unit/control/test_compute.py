"""SOR-181 per-agent compute: resolution, validation, durability, cost."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from control.backend import SandboxSpec
from control.compute import (
    DEFAULT_CPU,
    DEFAULT_MEMORY_MIB,
    INVALID_COMPUTE,
    ComputeError,
    ComputeSpec,
    compute_for_record,
    resolve_compute,
    spec_from_public,
    spec_from_tag,
    usd_per_second,
)
from control.config import (
    CPU,
    CPU_USD_PER_CORE_S,
    MEM_USD_PER_GIB_S,
    MEMORY_MIB,
    SANDBOX_USD_PER_S,
)
from control.service import cost_estimate_usd


def test_defaults_are_the_canonical_pair() -> None:
    assert DEFAULT_CPU == (1.0, 2.0)
    assert DEFAULT_MEMORY_MIB == (1024, 8192)


class TestResolveCompute:
    def test_omitted_resolves_full_defaults(self) -> None:
        spec = resolve_compute(None)
        assert spec == ComputeSpec()
        assert spec.public() == {"cpu": [1.0, 2.0], "memory_mib": [1024, 8192]}

    def test_empty_declaration_resolves_defaults(self) -> None:
        assert resolve_compute({}) == ComputeSpec()

    def test_partial_declaration_fills_defaults(self) -> None:
        spec = resolve_compute({"cpu": [2, 4]})
        assert spec.cpu == (2.0, 4.0)
        assert spec.memory_mib == DEFAULT_MEMORY_MIB
        spec = resolve_compute({"memory_mib": 4096})
        assert spec.cpu == DEFAULT_CPU
        assert spec.memory_mib == (4096, 4096)

    def test_scalar_pins_request_and_limit(self) -> None:
        spec = resolve_compute({"cpu": 4, "memory_mib": [2048, 4096]})
        assert spec.cpu == (4.0, 4.0)
        assert spec.memory_mib == (2048, 4096)

    def test_single_element_pair_repeats(self) -> None:
        spec = resolve_compute({"cpu": [2], "memory_mib": [2048]})
        assert spec.cpu == (2.0, 2.0)
        assert spec.memory_mib == (2048, 2048)

    def test_pydantic_model_input(self) -> None:
        decl = SimpleNamespace(cpu=[2, 4], memory_mib=None)
        spec = resolve_compute(decl)
        assert spec.cpu == (2.0, 4.0)
        assert spec.memory_mib == DEFAULT_MEMORY_MIB

    @pytest.mark.parametrize(
        "decl",
        [
            "2",  # not an object
            {"cpu": "fast"},
            {"cpu": [2, 4, 8]},
            {"cpu": []},
            {"cpu": [0, 2]},  # below floor
            {"cpu": [65, 64]},
            {"cpu": True},  # bool is not a number
            {"cpu": [4, 2]},  # inverted
            {"cpu": [None, 2]},
            {"cpu": [1, float("inf")]},
            {"memory_mib": 64},  # below floor
            {"memory_mib": 300000},  # above ceiling
            {"memory_mib": [4096, 2048]},  # inverted
            {"memory_mib": 1.5},  # int only
            {"memory_mib": [2048, "x"]},
        ],
    )
    def test_invalid_declarations_raise(self, decl: Any) -> None:
        with pytest.raises(ComputeError) as excinfo:
            resolve_compute(decl)
        assert excinfo.value.code == INVALID_COMPUTE
        assert excinfo.value.message

    def test_extra_keys_are_ignored(self) -> None:
        # The /v1 schema forbids extra keys; the resolver itself stays
        # tolerant for durable records written before a key was dropped.
        spec = resolve_compute({"cpu": 2, "bogus": True})
        assert spec.cpu == (2.0, 2.0)


class TestDurability:
    def test_spec_from_public_round_trips(self) -> None:
        spec = ComputeSpec(cpu=(2.0, 4.0), memory_mib=(2048, 4096))
        assert spec_from_public(spec.public()) == spec

    def test_spec_from_public_tolerates_garbage(self) -> None:
        assert spec_from_public(None) is None
        assert spec_from_public({"cpu": [4, 2]}) is None
        assert spec_from_public({"cpu": ["x", "y"]}) is None
        # Stored shapes re-validate tolerantly — a scalar still pins.
        assert spec_from_public({"cpu": 2}) == ComputeSpec(cpu=(2.0, 2.0))

    def test_spec_from_tag(self) -> None:
        spec = ComputeSpec(cpu=(8.0, 8.0), memory_mib=(8192, 16384))
        tags = {"compute": json.dumps(spec.public())}
        assert spec_from_tag(tags) == spec

    def test_spec_from_tag_missing_or_malformed(self) -> None:
        assert spec_from_tag(None) is None
        assert spec_from_tag({}) is None
        assert spec_from_tag({"compute": "not json"}) is None
        assert spec_from_tag({"compute": "{}"}) is None

    def test_compute_for_record_prefers_record_field(self) -> None:
        from_tag = ComputeSpec(cpu=(1.0, 1.0), memory_mib=(1024, 1024))
        from_rec = ComputeSpec(cpu=(4.0, 4.0), memory_mib=(4096, 4096))
        tags = {"compute": json.dumps(from_tag.public())}
        assert compute_for_record(from_rec.public(), tags) == from_rec
        assert compute_for_record(None, tags) == from_tag
        assert compute_for_record(None, None) is None

    def test_tag_fallback_covers_pre_field_records(self) -> None:
        # A record written by a build that had tags but not yet the
        # ``compute`` field still resolves its sizing (recovery path).
        tags = {"compute": '{"cpu": [2, 4], "memory_mib": [2048, 4096]}'}
        spec = compute_for_record(None, tags)
        assert spec == ComputeSpec(cpu=(2.0, 4.0), memory_mib=(2048, 4096))


class TestCost:
    def test_usd_per_second_bills_at_request_floor(self) -> None:
        spec = ComputeSpec(cpu=(4.0, 8.0), memory_mib=(8192, 16384))
        expected = CPU_USD_PER_CORE_S * 4.0 + MEM_USD_PER_GIB_S * 8.0
        assert usd_per_second(spec) == pytest.approx(expected)

    def test_no_spec_uses_p0_floor(self) -> None:
        assert usd_per_second(None) == SANDBOX_USD_PER_S

    def test_cost_estimate_usd_signature(self) -> None:
        spec = ComputeSpec(cpu=(8.0, 8.0), memory_mib=(16384, 16384))
        assert cost_estimate_usd(60.0, spec) == round(usd_per_second(spec) * 60.0, 6)
        # No spec → identical to the pre-SOR-181 estimate.
        assert cost_estimate_usd(60.0) == cost_estimate_usd(60.0, None)

    def test_limit_above_request_does_not_change_estimate(self) -> None:
        low = ComputeSpec(cpu=(2.0, 4.0), memory_mib=(2048, 4096))
        high = ComputeSpec(cpu=(2.0, 8.0), memory_mib=(2048, 8192))
        assert usd_per_second(low) == usd_per_second(high)


class _FakeSecret:
    @staticmethod
    def from_name(name: str) -> tuple[str, str]:
        return ("secret", name)

    @staticmethod
    def from_dict(data: dict) -> tuple[str, dict]:
        return ("dict", data)


class _FakeApp:
    @staticmethod
    def lookup(name: str, create_if_missing: bool = False) -> SimpleNamespace:
        return SimpleNamespace(app_id="app-1", name=name)


class _FakeSandbox:
    """Captures the kwargs ``ModalBackend._create_with_image`` passes."""

    created_kwargs: dict[str, Any] = {}

    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> SimpleNamespace:
        cls.created_kwargs = dict(kwargs)
        return SimpleNamespace(object_id="sb-fake")


_FAKE_MODAL = SimpleNamespace(App=_FakeApp, Sandbox=_FakeSandbox, Secret=_FakeSecret)


class TestModalPlumbing:
    """``_create_with_image`` is shared by create and snapshot restore —
    one spec covers both paths (``ModalSnapshotProvider.restore``)."""

    def test_declared_compute_reaches_sandbox_create(self) -> None:
        from control.backends.modal import ModalBackend

        spec = SandboxSpec(cpu=(4.0, 8.0), memory_mib=(4096, 16384))
        ModalBackend()._create_with_image(_FAKE_MODAL, spec, image="img")
        assert _FakeSandbox.created_kwargs["cpu"] == (4.0, 8.0)
        assert _FakeSandbox.created_kwargs["memory"] == (4096, 16384)

    def test_unsized_spec_falls_back_to_deployment_defaults(self) -> None:
        from control.backends.modal import ModalBackend

        ModalBackend()._create_with_image(_FAKE_MODAL, SandboxSpec(), image="img")
        assert _FakeSandbox.created_kwargs["cpu"] == CPU
        assert _FakeSandbox.created_kwargs["memory"] == MEMORY_MIB
