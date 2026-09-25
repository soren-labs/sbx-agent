"""Error types for the bootstrap CLI.

Every failure surfaced to the user is a :class:`BootstrapError` carrying a
machine-readable ``code`` plus an actionable ``hint`` — the next command or
fix the user should run. The CLI renders ``code``/``hint`` into ``--json``
output and onto stderr otherwise.
"""

from __future__ import annotations


class BootstrapError(Exception):
    """An actionable bootstrap failure.

    ``code`` is stable for scripts (``modal_auth_missing``,
    ``secret_missing``, ...); ``hint`` is the remediation line.
    """

    def __init__(self, message: str, *, hint: str | None = None, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


def missing_prereq(what: str, hint: str) -> BootstrapError:
    """A required tool/credential is absent; ``hint`` says how to get it."""
    return BootstrapError(f"missing prerequisite: {what}", hint=hint, code="missing_prereq")
