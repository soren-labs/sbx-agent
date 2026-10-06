"""Small explicit transition tables shared by all domain state machines."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from control.domain.errors import DomainError


@dataclass(frozen=True)
class Lifecycle:
    name: str
    transitions: Mapping[str, frozenset[str]]
    terminal: frozenset[str]

    @property
    def states(self) -> frozenset[str]:
        out = set(self.transitions) | set(self.terminal)
        for targets in self.transitions.values():
            out |= targets
        return frozenset(out)

    def allowed(self, current: str, target: str) -> bool:
        return target in self.transitions.get(current, frozenset())

    def check(self, current: str, target: str) -> None:
        if not self.allowed(current, target):
            raise DomainError(
                "invalid_transition",
                f"{self.name} cannot move from {current} to {target}",
                details={"entity": self.name, "from": current, "to": target},
            )

    def is_terminal(self, state: str) -> bool:
        return state in self.terminal


def table(spec: Mapping[str, tuple[str, ...]]) -> dict[str, frozenset[str]]:
    return {state: frozenset(targets) for state, targets in spec.items()}
