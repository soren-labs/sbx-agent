"""AgentAdapter Protocol + provider registry (P2 WP0 shell, frozen after merge).

Each provider CLI is driven through one ``AgentAdapter``: argv construction,
credential file declaration, native-event → canonical-event translation,
native session id extraction, and health classification. WP0 only registers
``codex`` (existing behavior moved in unchanged); the other providers land in
P2-B (SOR-62/SOR-72).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from runtime.runner.codex import build_codex_argv
from runtime.runner.events import parse_event_line

PROVIDERS: tuple[str, ...] = ("codex", "antigravity", "grok", "opencode", "devin")

Health = Literal["ok", "auth_invalid", "rate_limited", "unknown"]

_AUTH_NEEDLES = ("401", "unauthorized", "unauthenticated", "invalid api key", "auth")
_RATE_NEEDLES = ("429", "rate_limit", "rate limit", "too many requests", "quota")


@runtime_checkable
class AgentAdapter(Protocol):
    """Drives one provider CLI inside the sandbox."""

    provider: str
    credential_files: tuple[str, ...]  # relative to $HOME, e.g. (".codex/auth.json",)

    def prepare_home(self, home: Path, model: str) -> None:
        """Write CLI config / instructions under ``home`` before the first turn."""

    def first_turn_argv(self, prompt: str, model: str) -> list[str]:
        """argv for turn 1 (no native session id yet)."""

    def resume_argv(self, prompt: str, native_session_id: str) -> list[str]:
        """argv for follow-up turns resuming ``native_session_id``."""

    def translate(self, raw_line: str) -> list[dict[str, Any]]:
        """Map one native stdout line to 0..n canonical events (events.md)."""

    def extract_session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        """Return the native session id from translated events, if seen."""

    def health_from(self, exit_code: int | None, stderr_tail: str) -> Health:
        """Classify a finished CLI process for account health feedback."""


def get_adapter(provider: str) -> AgentAdapter:
    """Return the registered adapter for ``provider``; KeyError if unknown."""
    try:
        factory = _REGISTRY[provider]
    except KeyError:
        raise KeyError(f"unknown provider {provider!r}; registered: {sorted(_REGISTRY)}") from None
    return factory()


class CodexAdapter:
    """Codex CLI adapter: existing SOR-30 behavior, canonical passthrough."""

    provider = "codex"
    credential_files: tuple[str, ...] = (".codex/auth.json",)

    def __init__(self, auth: str = "auth_json") -> None:
        self.auth = auth

    def prepare_home(self, home: Path, model: str) -> None:
        from runtime.runner.bootstrap import render_config_toml
        from runtime.runner.workspace import atomic_write

        home.mkdir(parents=True, exist_ok=True)
        atomic_write(home / "config.toml", render_config_toml(model=model, auth=self.auth))

    def first_turn_argv(self, prompt: str, model: str) -> list[str]:
        from runtime.runner.workspace import work_root

        return build_codex_argv(work=work_root(), prompt=prompt, thread_id=None, model=model)

    def resume_argv(self, prompt: str, native_session_id: str) -> list[str]:
        from runtime.runner.workspace import work_root

        return build_codex_argv(
            work=work_root(),
            prompt=prompt,
            thread_id=native_session_id,
            model=None,
        )

    def translate(self, raw_line: str) -> list[dict[str, Any]]:
        """Codex native events are already canonical; pass dicts through."""
        obj, bad = parse_event_line(raw_line)
        if bad or obj is None:
            return []
        return [obj]

    def extract_session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "thread.started":
                thread_id = ev.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    return thread_id
        return None

    def health_from(self, exit_code: int | None, stderr_tail: str) -> Health:
        if exit_code == 0:
            return "ok"
        tail = (stderr_tail or "").lower()
        if any(needle in tail for needle in _AUTH_NEEDLES):
            return "auth_invalid"
        if any(needle in tail for needle in _RATE_NEEDLES):
            return "rate_limited"
        return "unknown"


def _antigravity() -> AgentAdapter:
    from runtime.runner.adapters.antigravity import AntigravityAdapter

    return AntigravityAdapter()


def _devin() -> AgentAdapter:
    from runtime.runner.adapters.devin import DevinAdapter

    return DevinAdapter()


_REGISTRY: dict[str, Callable[[], AgentAdapter]] = {
    "codex": CodexAdapter,
    "antigravity": _antigravity,
    "devin": _devin,
}
