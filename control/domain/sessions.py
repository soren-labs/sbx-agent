from control.domain.errors import require

TURN_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
TURN_TRANSITIONS = {
    "queued": {"preparing", "cancelled"},
    "preparing": {"queued", "running", "failed", "cancelling"},
    "running": {"succeeded", "failed", "cancelling", "interrupted"},
    "cancelling": {"cancelled", "interrupted"},
}


def turn_transition(previous: str, following: str) -> None:
    require(following in TURN_TRANSITIONS.get(previous, set()), "version_conflict")


def terminal_verdict(state: str, *, stopped: bool, success: bool, complete: bool) -> str:
    if state in TURN_TERMINAL:
        return state
    if state == "cancelling":
        return "cancelled" if stopped else "interrupted"
    if not stopped or not complete:
        return "interrupted"
    return "succeeded" if success else "failed"
