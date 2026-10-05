"""Service desires and lease-bound realizations (RFC 167 §02/§03)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .errors import InvalidTransition, TerminalViolation


class ServiceInstanceState(enum.StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


SERVICE_TERMINAL: frozenset[ServiceInstanceState] = frozenset(
    {ServiceInstanceState.STOPPED, ServiceInstanceState.FAILED}
)

SERVICE_EDGES: dict[ServiceInstanceState, frozenset[ServiceInstanceState]] = {
    ServiceInstanceState.PENDING: frozenset(
        {ServiceInstanceState.STARTING, ServiceInstanceState.STOPPED, ServiceInstanceState.FAILED}
    ),
    ServiceInstanceState.STARTING: frozenset(
        {
            ServiceInstanceState.READY,
            ServiceInstanceState.DEGRADED,
            ServiceInstanceState.STOPPING,
            ServiceInstanceState.FAILED,
        }
    ),
    ServiceInstanceState.READY: frozenset(
        {
            ServiceInstanceState.DEGRADED,
            ServiceInstanceState.STOPPING,
            ServiceInstanceState.STOPPED,
            ServiceInstanceState.FAILED,
        }
    ),
    ServiceInstanceState.DEGRADED: frozenset(
        {
            ServiceInstanceState.READY,
            ServiceInstanceState.STOPPING,
            ServiceInstanceState.STOPPED,
            ServiceInstanceState.FAILED,
        }
    ),
    ServiceInstanceState.STOPPING: frozenset(
        {ServiceInstanceState.STOPPED, ServiceInstanceState.FAILED}
    ),
    ServiceInstanceState.STOPPED: frozenset(),
    ServiceInstanceState.FAILED: frozenset(),
}


def require_service_transition(current: ServiceInstanceState, target: ServiceInstanceState) -> None:
    if current in SERVICE_TERMINAL:
        raise TerminalViolation("service_instance", current.value)
    if target not in SERVICE_EDGES[current]:
        raise InvalidTransition("service_instance", current.value, target.value)


@dataclass
class ServiceDesire:
    """Durable desired state persisting across lease realizations."""

    id: str
    workspace_id: str
    session_id: str
    name: str
    declaration_digest: str
    desired_state: str  # running|stopped
    version: int = 1
    created_at: object = None
    updated_at: object = None


@dataclass
class ServiceInstance:
    id: str
    workspace_id: str
    session_id: str
    name: str
    executor_lease_id: str
    lease_generation: int
    state: ServiceInstanceState
    observed: dict = field(default_factory=dict)  # pid/port/health observations
    health_at: object = None
    created_at: object = None
    updated_at: object = None
