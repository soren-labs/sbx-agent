"""SOR-203: control-plane web-container warmth resolution.

``control_warmth_config`` is the single resolution point for the ASGI
function's Modal autoscaler knobs (``scaledown_window`` /
``min_containers`` / ``buffer_containers``), evaluated at deploy time on
the operator's host — never inside the remote functions, and never
applied to the Agent ``Sandbox.create`` lifecycle chain.
"""

from __future__ import annotations

from control.config import (
    CONTROL_SCALEDOWN_WINDOW_MAX_S,
    CONTROL_SCALEDOWN_WINDOW_MIN_S,
    CONTROL_SCALEDOWN_WINDOW_S,
    control_warmth_config,
)


def test_warmth_defaults() -> None:
    warmth = control_warmth_config({})
    # The 300s default is the cost-rational strategy: containers stay warm
    # through a burst of interactive requests (billed only for the idle
    # tail) without paying always-on ``min_containers`` cost.
    assert warmth.scaledown_window_s == CONTROL_SCALEDOWN_WINDOW_S == 300
    assert warmth.min_containers == 0
    assert warmth.buffer_containers == 0


def test_warmth_env_overrides() -> None:
    warmth = control_warmth_config(
        {
            "SBX_CONTROL_SCALEDOWN_WINDOW_S": "600",
            "SBX_CONTROL_MIN_CONTAINERS": "1",
            "SBX_CONTROL_BUFFER_CONTAINERS": "2",
        }
    )
    assert warmth.scaledown_window_s == 600
    assert warmth.min_containers == 1
    assert warmth.buffer_containers == 2


def test_warmth_scaledown_window_clamps_to_modal_bounds() -> None:
    """Modal accepts 2s..1200s; out-of-range overrides clamp rather than
    fail a deploy."""
    assert (
        control_warmth_config({"SBX_CONTROL_SCALEDOWN_WINDOW_S": "1"}).scaledown_window_s
        == CONTROL_SCALEDOWN_WINDOW_MIN_S
    )
    assert (
        control_warmth_config({"SBX_CONTROL_SCALEDOWN_WINDOW_S": "99999"}).scaledown_window_s
        == CONTROL_SCALEDOWN_WINDOW_MAX_S
    )


def test_warmth_counts_clamp_to_zero() -> None:
    warmth = control_warmth_config(
        {"SBX_CONTROL_MIN_CONTAINERS": "-2", "SBX_CONTROL_BUFFER_CONTAINERS": "-1"}
    )
    assert warmth.min_containers == 0
    assert warmth.buffer_containers == 0


def test_warmth_reads_process_env(monkeypatch) -> None:
    monkeypatch.setenv("SBX_CONTROL_SCALEDOWN_WINDOW_S", "900")
    monkeypatch.setenv("SBX_CONTROL_MIN_CONTAINERS", "1")
    warmth = control_warmth_config()
    assert warmth.scaledown_window_s == 900
    assert warmth.min_containers == 1
    monkeypatch.delenv("SBX_CONTROL_SCALEDOWN_WINDOW_S")
    monkeypatch.delenv("SBX_CONTROL_MIN_CONTAINERS")
    assert control_warmth_config().scaledown_window_s == CONTROL_SCALEDOWN_WINDOW_S


def test_warmth_keys_stay_out_of_remote_env() -> None:
    """Warmth is autoscaler config baked at deploy time — it is not remote
    env, so the keys deliberately stay off the ``REMOTE_ENV_KEYS``
    allowlist (deploy_env forwards them to the ``modal deploy`` subprocess
    instead)."""
    from control.config import REMOTE_ENV_KEYS

    for key in (
        "SBX_CONTROL_SCALEDOWN_WINDOW_S",
        "SBX_CONTROL_MIN_CONTAINERS",
        "SBX_CONTROL_BUFFER_CONTAINERS",
    ):
        assert key not in REMOTE_ENV_KEYS
