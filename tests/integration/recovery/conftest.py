"""Fixtures for the SOR-82/A4 recovery-acceptance suite."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator

import pytest
from tests.integration.recovery import support
from tests.integration.recovery.support import RecoveryEnv


@pytest.fixture
def make_recovery_env(stub_runner) -> Iterator[Callable[..., RecoveryEnv]]:
    """Factory for control-plane instances; every built env is torn down."""
    envs: list[RecoveryEnv] = []

    def _make(**kwargs) -> RecoveryEnv:
        kwargs.setdefault("runner_cmd", [sys.executable, str(stub_runner)])
        env = support.build_env(**kwargs)
        envs.append(env)
        return env

    yield _make
    for env in envs:
        env.close()


@pytest.fixture
def recovery_env(make_recovery_env) -> RecoveryEnv:
    return make_recovery_env()
