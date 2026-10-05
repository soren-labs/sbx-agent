import pytest
from control.domain.errors import DomainError
from control.domain.sessions import terminal_verdict, turn_transition


def test_cancel_precedence_and_unknown():
    assert terminal_verdict("cancelling", stopped=True, success=True, complete=True) == "cancelled"
    assert (
        terminal_verdict("cancelling", stopped=False, success=True, complete=True) == "interrupted"
    )
    assert terminal_verdict("running", stopped=True, success=True, complete=False) == "interrupted"
    assert terminal_verdict("succeeded", stopped=True, success=False, complete=True) == "succeeded"
    with pytest.raises(DomainError):
        turn_transition("cancelled", "succeeded")
