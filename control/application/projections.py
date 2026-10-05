from control.domain.errors import require


def replay(events):
    """Offline lifecycle reducer; never serves production authority."""
    state = {"lifecycle": "open", "turns": {}, "event_watermark": 0}
    for event in events:
        require(event["seq"] == state["event_watermark"] + 1, "history_reset_required")
        kind, payload = event["type"], event["payload"]
        if kind in {"session.archived", "session.unarchived", "session.closed"}:
            state["lifecycle"] = {
                "session.archived": "archived",
                "session.unarchived": "open",
                "session.closed": "closed",
            }[kind]
        if kind.startswith("turn.") and payload.get("turn_id"):
            suffix = kind.split(".")[1]
            mapped = {"started": "running", "cancel_requested": "cancelling"}.get(suffix, suffix)
            if mapped in {
                "queued",
                "preparing",
                "running",
                "cancelling",
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            }:
                state["turns"][payload["turn_id"]] = mapped
        state["event_watermark"] = event["seq"]
    return state
