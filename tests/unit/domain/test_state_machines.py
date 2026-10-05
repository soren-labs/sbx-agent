"""Canonical state machines — exact RFC edge sets + terminal guards."""

from __future__ import annotations

import pytest
from control.domain.delegation import (
    DelegationState,
    require_delegation_transition,
)
from control.domain.delivery import (
    DeliveryState,
    require_delivery_transition,
)
from control.domain.errors import InvalidTransition, TerminalViolation
from control.domain.execution import (
    EXECUTION_TERMINAL,
    ExecutionState,
    LeaseState,
    require_execution_transition,
    require_lease_transition,
)
from control.domain.jobs import JOB_TERMINAL, JobState
from control.domain.sessions import (
    TURN_ACTIVE,
    TURN_TERMINAL,
    TurnState,
    require_turn_transition,
)


def test_turn_edges():
    require_turn_transition(TurnState.QUEUED, TurnState.PREPARING)
    require_turn_transition(TurnState.PREPARING, TurnState.RUNNING)
    require_turn_transition(TurnState.RUNNING, TurnState.SUCCEEDED)
    require_turn_transition(TurnState.RUNNING, TurnState.CANCELLING)
    require_turn_transition(TurnState.CANCELLING, TurnState.CANCELLED)
    require_turn_transition(TurnState.PREPARING, TurnState.QUEUED)  # requeue


@pytest.mark.parametrize(
    "src,dst",
    [
        (TurnState.QUEUED, TurnState.SUCCEEDED),
        (TurnState.RUNNING, TurnState.QUEUED),
        (TurnState.PREPARING, TurnState.CANCELLED),
        (TurnState.CANCELLING, TurnState.RUNNING),
    ],
)
def test_turn_illegal_edges(src, dst):
    with pytest.raises(InvalidTransition):
        require_turn_transition(src, dst)


@pytest.mark.parametrize("state", TURN_TERMINAL)
def test_turn_terminal_guard(state):
    with pytest.raises(TerminalViolation):
        require_turn_transition(state, TurnState.RUNNING)


def test_turn_active_set():
    assert TURN_ACTIVE == frozenset({TurnState.PREPARING, TurnState.RUNNING, TurnState.CANCELLING})


def test_execution_edges():
    require_execution_transition(ExecutionState.PREPARING, ExecutionState.STARTED)
    require_execution_transition(ExecutionState.STARTED, ExecutionState.SUCCEEDED)
    require_execution_transition(ExecutionState.STOP_REQUESTED, ExecutionState.CANCELLED)
    # Runtime loss can land 'unknown' from any live state.
    require_execution_transition(ExecutionState.STARTED, ExecutionState.UNKNOWN)
    with pytest.raises(InvalidTransition):
        require_execution_transition(ExecutionState.PREPARING, ExecutionState.SUCCEEDED)
    for t in EXECUTION_TERMINAL:
        with pytest.raises(TerminalViolation):
            require_execution_transition(t, ExecutionState.STARTED)


def test_lease_edges():
    require_lease_transition(LeaseState.ALLOCATING, LeaseState.READY)
    require_lease_transition(LeaseState.READY, LeaseState.QUIESCING)
    require_lease_transition(LeaseState.QUIESCING, LeaseState.RELEASED)
    require_lease_transition(LeaseState.READY, LeaseState.LOST)
    with pytest.raises(InvalidTransition):
        require_lease_transition(LeaseState.ALLOCATING, LeaseState.RELEASED)
    with pytest.raises(TerminalViolation):
        require_lease_transition(LeaseState.LOST, LeaseState.READY)


def test_delivery_edges():
    require_delivery_transition(DeliveryState.PENDING, DeliveryState.EXECUTING)
    require_delivery_transition(DeliveryState.EXECUTING, DeliveryState.SUCCEEDED)
    require_delivery_transition(DeliveryState.EXECUTING, DeliveryState.BLOCKED)
    require_delivery_transition(DeliveryState.FAILED, DeliveryState.PENDING)


def test_delegation_edges():
    require_delegation_transition(DelegationState.PENDING, DelegationState.ACTIVE)
    require_delegation_transition(DelegationState.ACTIVE, DelegationState.WAITING_RESULT)
    require_delegation_transition(DelegationState.WAITING_RESULT, DelegationState.SUCCEEDED)
    with pytest.raises(InvalidTransition):
        require_delegation_transition(DelegationState.PENDING, DelegationState.SUCCEEDED)


def test_job_terminal():
    assert JOB_TERMINAL == frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})
