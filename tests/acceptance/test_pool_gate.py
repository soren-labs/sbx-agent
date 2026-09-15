"""pytest shim for the SOR-63/D2 pool-acceptance gate.

``tests/acceptance`` is outside ``testpaths`` — the gate runs only when
invoked directly:

    uv run pytest tests/acceptance                       # fake matrix, both providers
    SBX_POOL_GATE_REAL=1 SBX_V1_API_KEY=... \
        SBX_V1_BASE_URL=https://... uv run pytest tests/acceptance -k real

Fake mode (default) is the offline contract: ``LocalProcessBackend`` +
``stub_runner`` behind ``ProviderPool``, no credentials. Real mode drives
the deployed pool (Antigravity 4 accounts, then Grok 2) and skips unless
``SBX_POOL_GATE_REAL=1`` plus a base URL and bearer key are present.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.acceptance import pool_gate
from tests.acceptance.pool_gate import FakeTransport, RealTransport, reset_state, run_gate

PROVIDERS = ("antigravity", "grok")


def _run(provider: str, transport) -> str:
    spec = pool_gate.SPECS[provider]
    reset_state()
    try:
        return run_gate(
            transport,
            provider=provider,
            expected=spec.expected_accounts,
            model=spec.model,
            prompt=pool_gate.DEFAULT_PROMPT,
            max_fanout=8,
            run_timeout=60.0 if transport.mode == "fake" else 600.0,
        )
    finally:
        transport.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_pool_gate_fake(provider: str, stub_runner: Path) -> None:
    spec = pool_gate.SPECS[provider]
    transport = FakeTransport(
        provider, spec.expected_accounts, model=spec.model, runner=stub_runner
    )
    verdict = _run(provider, transport)
    failed = [c["name"] for c in pool_gate.CHECKS if not c["ok"]]
    assert verdict == "PASS", f"pool gate {provider} failed: {failed}"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_pool_gate_real(provider: str) -> None:
    if os.environ.get("SBX_POOL_GATE_REAL") != "1":
        pytest.skip("real pool gate disabled; set SBX_POOL_GATE_REAL=1")
    base_url = (os.environ.get("SBX_V1_BASE_URL") or "").strip()
    token = (os.environ.get("SBX_V1_API_KEY") or "").strip()
    if not base_url or not token:
        pytest.skip("real pool gate needs SBX_V1_BASE_URL + SBX_V1_API_KEY")
    transport = RealTransport(base_url, token)
    verdict = _run(provider, transport)
    failed = [c["name"] for c in pool_gate.CHECKS if not c["ok"]]
    assert verdict == "PASS", f"real pool gate {provider} failed: {failed}"
