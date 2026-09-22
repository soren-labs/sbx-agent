"""SOR-181 per-agent dynamic Sandbox compute.

Agents may declare their sandbox's compute sizing at create time
(``POST /v1/agents`` ``compute``) — independent of SOR-129
``resources.secrets``/``resources.mcp``, which name credential/config
refs and must not carry sizing:

- ``cpu`` — Modal ``Sandbox.create(cpu=...)`` request/limit pair
  ``[min, max]`` in cores (a scalar pins ``min == max``).
- ``memory_mib`` — the same pair in MiB.

Undeclared fields resolve to the canonical defaults
(``cpu=[1, 2]``, ``memory_mib=[1024, 8192]``); an omitted ``compute``
declaration resolves to the full defaults. Validation is fail-closed at
request time: malformed or out-of-bounds values are ``invalid_compute``
— never silently clamped or dropped. The resolved spec is durable state
(``SessionRecord.compute`` + the ``compute`` sandbox tag) so it survives
control-plane restarts and rides sandbox create *and* snapshot restore
(``SandboxSpec.cpu``/``memory_mib``) — recovery never reverts a session
to different sizing. Cost estimates bill at the declared request floor
(``cpu[0]`` / ``memory_mib[0]``), matching the P0 rate model.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from control.config import (
    CPU_USD_PER_CORE_S,
    MEM_USD_PER_GIB_S,
    REQUEST_CPU_CORES,
    REQUEST_MEM_GIB,
)

# Canonical compute defaults (SOR-181). Applied when an agent omits
# ``compute`` or a field inside it; the backend's deployment constants
# (``control.config.CPU``/``MEMORY_MIB``) stay the fallback for non-agent
# sandboxes (verify probes, environment builds).
DEFAULT_CPU = (1.0, 2.0)
DEFAULT_MEMORY_MIB = (1024, 8192)

# Canonical v1 error code for compute validation failures.
INVALID_COMPUTE = "invalid_compute"

# Sanity bounds — fail closed on unit typos (e.g. bytes where MiB was
# meant) without rejecting any realistic sandbox size.
_MIN_CPU = 0.125
_MAX_CPU = 64.0
_MIN_MEMORY_MIB = 128
_MAX_MEMORY_MIB = 256 * 1024  # 256 GiB


class ComputeError(Exception):
    """Domain error for compute declaration validation (code → v1 error code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ComputeSpec:
    """A resolved per-agent compute sizing.

    ``cpu``/``memory_mib`` are ``(request, limit)`` pairs passed verbatim
    to ``Sandbox.create``. ``public()`` is the JSON shape stored on the
    durable session record, echoed on the agent view, and written into
    the ``compute`` sandbox tag.
    """

    cpu: tuple[float, float] = DEFAULT_CPU
    memory_mib: tuple[int, int] = DEFAULT_MEMORY_MIB

    def public(self) -> dict[str, Any]:
        """JSON-safe echo: ``{"cpu": [min, max], "memory_mib": [min, max]}``."""
        return {
            "cpu": [self.cpu[0], self.cpu[1]],
            "memory_mib": [self.memory_mib[0], self.memory_mib[1]],
        }


def _field(decl: Any, name: str) -> Any:
    """Read ``name`` off a pydantic model or mapping; ``None`` when absent."""
    if isinstance(decl, Mapping):
        return decl.get(name)
    return getattr(decl, name, None)


def _is_number(value: Any) -> bool:
    # bool is an int subclass — a JSON true/false is not a cpu/memory size.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _pair(value: Any, name: str) -> tuple[Any, Any]:
    """Normalize a scalar or ``[min, max]`` (or single-element) declaration."""
    values = value if isinstance(value, (list, tuple)) else [value]
    if not 1 <= len(values) <= 2:
        raise ComputeError(
            INVALID_COMPUTE,
            f"compute {name} must be a number or a [min, max] pair, got {value!r}",
        )
    if len(values) == 1:
        values = [values[0], values[0]]
    return values[0], values[1]


def _cpu_range(value: Any) -> tuple[float, float]:
    low, high = _pair(value, "cpu")
    if not all(_is_number(v) for v in (low, high)):
        raise ComputeError(INVALID_COMPUTE, f"compute cpu must be numeric, got {value!r}")
    pair = (float(low), float(high))
    if not all(math.isfinite(v) for v in pair):
        raise ComputeError(INVALID_COMPUTE, f"compute cpu must be finite, got {value!r}")
    if pair[0] > pair[1]:
        raise ComputeError(INVALID_COMPUTE, f"compute cpu min exceeds max: {pair[0]} > {pair[1]}")
    if not (_MIN_CPU <= pair[0] and pair[1] <= _MAX_CPU):
        raise ComputeError(
            INVALID_COMPUTE,
            f"compute cpu out of bounds [{_MIN_CPU}, {_MAX_CPU}]: {value!r}",
        )
    return pair


def _memory_range(value: Any) -> tuple[int, int]:
    low, high = _pair(value, "memory_mib")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (low, high)):
        raise ComputeError(INVALID_COMPUTE, f"compute memory_mib must be integers, got {value!r}")
    pair = (int(low), int(high))
    if pair[0] > pair[1]:
        raise ComputeError(
            INVALID_COMPUTE,
            f"compute memory_mib min exceeds max: {pair[0]} > {pair[1]}",
        )
    if not (_MIN_MEMORY_MIB <= pair[0] and pair[1] <= _MAX_MEMORY_MIB):
        raise ComputeError(
            INVALID_COMPUTE,
            f"compute memory_mib out of bounds [{_MIN_MEMORY_MIB}, {_MAX_MEMORY_MIB}]: {value!r}",
        )
    return pair


def resolve_compute(decl: Any) -> ComputeSpec:
    """Validate a create-time ``compute`` declaration into a ComputeSpec.

    ``None`` resolves to the canonical defaults — every ``/v1`` agent gets
    a concrete resolved spec so its durable record is honest about the
    sandbox's sizing. Scalars pin ``min == max``; omitted fields take the
    defaults.

    Raises :class:`ComputeError` (``invalid_compute``) for malformed,
    inverted, or out-of-bounds declarations.
    """
    if decl is None:
        return ComputeSpec()
    if not isinstance(decl, Mapping) and not (hasattr(decl, "cpu") or hasattr(decl, "memory_mib")):
        raise ComputeError(INVALID_COMPUTE, f"compute must be an object, got {decl!r}")
    cpu_raw = _field(decl, "cpu")
    mem_raw = _field(decl, "memory_mib")
    return ComputeSpec(
        cpu=_cpu_range(cpu_raw) if cpu_raw is not None else DEFAULT_CPU,
        memory_mib=_memory_range(mem_raw) if mem_raw is not None else DEFAULT_MEMORY_MIB,
    )


def spec_from_public(data: Any) -> ComputeSpec | None:
    """Re-read a stored ``public()`` shape (session record / tag payload).

    Returns ``None`` when ``data`` is missing or malformed — recovery must
    not fail on a record written before SOR-181.
    """
    if not isinstance(data, Mapping):
        return None
    cpu = data.get("cpu")
    mem = data.get("memory_mib")
    try:
        if cpu is None and mem is None:
            return None
        return resolve_compute({"cpu": cpu, "memory_mib": mem})
    except ComputeError:
        return None


def spec_from_tag(tags: Mapping[str, str] | None) -> ComputeSpec | None:
    """Recover the declared compute from sandbox tags after a restart."""
    raw = (tags or {}).get("compute")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return spec_from_public(parsed)


def compute_for_record(
    compute: Mapping[str, Any] | None, tags: Mapping[str, str] | None
) -> ComputeSpec | None:
    """Resolve the durable compute for a session record.

    The record's ``compute`` field is authoritative; the ``compute``
    sandbox tag is the restart/recovery fallback for records written
    before the field existed (or reconstructed from the live sandbox).
    """
    spec = spec_from_public(compute)
    if spec is not None:
        return spec
    return spec_from_tag(tags)


def usd_per_second(spec: ComputeSpec | None) -> float:
    """USD per sandbox-second billed at the declared request floor.

    ``None`` (a record predating SOR-181, or a non-agent sandbox) reports
    the P0 deployment floor (``REQUEST_CPU_CORES``/``REQUEST_MEM_GIB``).
    """
    if spec is None:
        return CPU_USD_PER_CORE_S * REQUEST_CPU_CORES + MEM_USD_PER_GIB_S * REQUEST_MEM_GIB
    return CPU_USD_PER_CORE_S * spec.cpu[0] + MEM_USD_PER_GIB_S * (spec.memory_mib[0] / 1024.0)
