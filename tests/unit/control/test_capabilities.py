"""SOR-204: capability discovery — parsing, catalog TTL/stale, probes.

No real credentials: the sandbox probe test rides ``LocalProcessBackend``
+ ``stub_runner`` + the ``fake_*`` CLIs, same seam as
``SandboxAuthVerifyProbe``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from control.backend import LocalProcessBackend
from control.capabilities import (
    CapabilityCatalog,
    DeclaredCapabilityProbe,
    DiscoveryResult,
    ModelCapability,
    SandboxCapabilityProbe,
    declared_model_ids,
    fallback_models,
    parse_models_output,
)
from control.ports import Account


def _iso() -> str:
    return datetime.now(UTC).isoformat()


def _account(provider: str = "codex", **kw: Any) -> Account:
    return Account(
        id=kw.pop("id", f"acct-{provider}"),
        provider=provider,
        label=provider,
        created_at=_iso(),
        **kw,
    )


class TestParseModelsOutput:
    def test_json_models_list(self) -> None:
        out = (
            '{"models": ['
            '{"id": "swe-2-high", "display_name": "SWE-2 High",'
            ' "efforts": ["low", "high"], "default": true},'
            ' {"id": "swe-2-max"}]}'
        )
        caps = parse_models_output("devin", out)
        assert [c.model for c in caps] == ["swe-2-high", "swe-2-max"]
        first = caps[0]
        assert first.display_name == "SWE-2 High"
        assert first.reasoning_efforts == ("low", "high")
        assert first.effort_native == {"low": "low", "high": "high"}
        assert "swe-2" in first.aliases

    def test_json_bare_list_of_strings(self) -> None:
        caps = parse_models_output("grok", '["grok-4.6", "grok-4.5"]')
        assert [c.model for c in caps] == ["grok-4.6", "grok-4.5"]
        # No explicit efforts → verified floor applies.
        assert caps[0].reasoning_efforts == ("low", "medium", "high")

    def test_text_lines_with_efforts_kv(self) -> None:
        out = "Available models:\n  grok-4.6 efforts=low,medium,high,xhigh\n  grok-4.5\n"
        caps = parse_models_output("grok", out)
        assert [c.model for c in caps] == ["grok-4.6", "grok-4.5"]
        assert caps[0].reasoning_efforts == ("low", "medium", "high", "xhigh")
        assert caps[0].effort_native["xhigh"] == "xhigh"

    def test_text_native_effort_spellings_map_canonical(self) -> None:
        caps = parse_models_output("grok", "grok-4.6 efforts=off,min,med,ultra,maximal\n")
        assert caps[0].reasoning_efforts == ("none", "minimal", "medium", "xhigh", "max")
        assert caps[0].effort_native == {
            "none": "off",
            "minimal": "min",
            "medium": "med",
            "xhigh": "ultra",
            "max": "maximal",
        }

    def test_text_paren_group_and_default_marker(self) -> None:
        out = "gemini-3.8-flash-low\ngemini-3.8-pro (low,medium) (default)\n"
        caps = parse_models_output("antigravity", out)
        # The (default) marker pulls its row to the front.
        assert [c.model for c in caps] == ["gemini-3.8-pro", "gemini-3.8-flash-low"]
        pro = caps[0]
        assert pro.reasoning_efforts == ("low", "medium")
        flash = caps[1]
        assert "gemini-3.8-flash" in flash.aliases
        assert flash.default_effort == "low"
        # Suffix-encoded tier does not grant an effort surface — the
        # provider's floor still does.
        assert flash.reasoning_efforts == ("low", "medium", "high")

    def test_effortless_provider_keeps_baked_in_tier(self) -> None:
        caps = parse_models_output("devin", "swe-2-medium\nswe-2-high\nswe-2-max\n")
        assert [c.model for c in caps] == ["swe-2-medium", "swe-2-high", "swe-2-max"]
        for cap in caps:
            assert cap.reasoning_efforts == ()  # devin has no effort knob
            assert "swe-2" in cap.aliases
        assert caps[2].default_effort == "max"

    def test_opencode_zen_free_model(self) -> None:
        out = "openai/gpt-5.6-luna\nopencode/claude-sonnet-4-5\nmuse-spark-1.3-contributor-free\n"
        caps = parse_models_output("opencode", out)
        assert "muse-spark-1.3-contributor-free" in {c.model for c in caps}
        muse = next(c for c in caps if c.model == "muse-spark-1.3-contributor-free")
        assert muse.aliases == ()  # ``-free`` is not an effort suffix
        assert muse.family == "muse-spark"

    def test_prose_and_headers_skipped(self) -> None:
        caps = parse_models_output("codex", "Models:\nLogged in\ngpt-5.6-luna\n")
        assert [c.model for c in caps] == ["gpt-5.6-luna"]

    def test_unparseable_is_empty_never_raises(self) -> None:
        assert parse_models_output("codex", "garbage output, no ids") == ()
        assert parse_models_output("codex", "") == ()


class TestFallbacks:
    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SBX_GROK_MODELS", "grok-9-turbo, grok-4.6")
        assert fallback_models("grok") == ("grok-9-turbo", "grok-4.6")

    def test_declared_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SBX_CODEX_MODELS", "env-model-1")
        account = _account("codex", models=("declared-1",))
        assert declared_model_ids(account) == (("declared-1",), "declared")
        bare = _account("codex")
        assert declared_model_ids(bare) == (("env-model-1",), "env")
        monkeypatch.delenv("SBX_CODEX_MODELS")
        assert declared_model_ids(bare)[1] == "static"


class _ScriptedProbe:
    """Deterministic probe for catalog tests; ``results`` are returned in order."""

    def __init__(self, *results: DiscoveryResult) -> None:
        self.calls: list[str] = []
        self._results = list(results)
        self._last = results[-1] if results else DiscoveryResult(error="empty")

    def probe(self, account: Account, blob: dict | None) -> DiscoveryResult:
        self.calls.append(account.id)
        return self._results.pop(0) if self._results else self._last


def _caps(*models: str) -> tuple[ModelCapability, ...]:
    probe = DeclaredCapabilityProbe()
    account = _account("codex", models=models)
    return probe.probe(account, None).models


class TestCapabilityCatalog:
    def test_get_cold_serves_declared(self) -> None:
        catalog = CapabilityCatalog(_ScriptedProbe(), ttl_s=60, auto_refresh=False)
        account = _account("codex", models=("declared-1", "declared-2"))
        snap = catalog.get(account)
        assert snap.source == "declared"
        assert [m.model for m in snap.models] == ["declared-1", "declared-2"]
        assert snap.stale is False

    def test_refresh_then_ttl_hit(self) -> None:
        probe = _ScriptedProbe(
            DiscoveryResult(models=_caps("live-1", "live-2"), default_model="live-1")
        )
        clock = iter([0.0, 10.0, 20.0])
        catalog = CapabilityCatalog(probe, ttl_s=60, clock=lambda: next(clock), auto_refresh=False)
        account = _account("codex")
        snap = catalog.refresh(account)
        assert snap.source == "discovered"
        assert [m.model for m in snap.models] == ["live-1", "live-2"]
        assert snap.stale is False
        # Within TTL: cached, no new probe.
        assert catalog.get(account).models == snap.models
        assert probe.calls == ["acct-codex"]

    def test_expired_get_marks_stale_and_keeps_last_good(self) -> None:
        probe = _ScriptedProbe(
            DiscoveryResult(models=_caps("live-1")),
            DiscoveryResult(error="boom"),
        )
        times = [0.0, 100.0, 200.0]
        catalog = CapabilityCatalog(probe, ttl_s=60, clock=lambda: times.pop(0), auto_refresh=False)
        account = _account("codex")
        good = catalog.refresh(account)
        stale = catalog.get(account)  # expired → stale copy, no re-probe
        assert stale.stale is True
        assert stale.models == good.models
        assert probe.calls == ["acct-codex"]

    def test_failed_refresh_preserves_last_good(self) -> None:
        probe = _ScriptedProbe(
            DiscoveryResult(models=_caps("live-1")),
            DiscoveryResult(error="probe down"),
        )
        catalog = CapabilityCatalog(probe, ttl_s=0.0, auto_refresh=False)
        account = _account("codex")
        catalog.refresh(account)
        snap = catalog.refresh(account)
        assert snap.stale is True
        assert snap.error == "probe down"
        assert [m.model for m in snap.models] == ["live-1"]

    def test_failed_first_refresh_falls_back_to_declared_stale(self) -> None:
        probe = _ScriptedProbe(DiscoveryResult(error="nope"))
        catalog = CapabilityCatalog(probe, ttl_s=60, auto_refresh=False)
        account = _account("grok", models=("grok-4.6",))
        snap = catalog.refresh(account)
        assert snap.source == "declared"
        assert snap.stale is True
        assert [m.model for m in snap.models] == ["grok-4.6"]


class TestSandboxCapabilityProbe:
    """Throwaway-sandbox discovery against stub_runner + fake CLIs."""

    def _probe(self, stub_runner: Path, bin_env: dict[str, str]) -> SandboxCapabilityProbe:
        return SandboxCapabilityProbe(
            LocalProcessBackend(),
            [sys.executable, str(stub_runner)],
            bin_env=bin_env,
        )

    def test_discovers_via_fake_cli(
        self, tmp_path: Path, stub_runner: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = repo_root / "tests" / "fakes" / "fake_agy.py"
        monkeypatch.setenv("FAKE_AGY_MODELS", "gemini-3.8-flash-low\ngemini-3.8-pro (default)\n")
        probe = self._probe(stub_runner, {"AGY_BIN": str(fake)})
        account = _account("antigravity", id="agy-1")
        blob = {
            "provider": "antigravity",
            "files": {".gemini/antigravity-cli/antigravity-oauth-token": "REDACTED"},
        }
        result = probe.probe(account, blob)
        assert result.error is None
        assert result.default_model == "gemini-3.8-pro"
        assert [m.model for m in result.models] == [
            "gemini-3.8-pro",
            "gemini-3.8-flash-low",
        ]

    def test_no_credential_is_error_not_hang(self, stub_runner: Path, repo_root: Path) -> None:
        fake = repo_root / "tests" / "fakes" / "fake_grok.py"
        probe = self._probe(stub_runner, {"GROK_BIN": str(fake)})
        result = probe.probe(_account("grok"), None)
        assert result.error == "no_credential"

    def test_cli_rejection_is_error(
        self, stub_runner: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restored-but-invalid credential → ``models_list`` fails closed."""
        fake = repo_root / "tests" / "fakes" / "fake_grok.py"
        probe = self._probe(stub_runner, {"GROK_BIN": str(fake)})
        account = _account("grok")
        # Blob restores a different path than the CLI checks → not logged in.
        blob = {"provider": "grok", "files": {".grok/other.json": "REDACTED"}}
        result = probe.probe(account, blob)
        assert result.error == "models_list_failed:1"
