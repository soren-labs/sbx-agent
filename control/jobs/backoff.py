"""Bounded exponential backoff with jitter and connector retry-after."""

from __future__ import annotations

import random


def delay_for(
    attempt: int, *, base: float = 1.0, cap: float = 300.0, retry_after: float | None = None
) -> float:
    computed = min(cap, base * (2 ** max(0, attempt - 1)))
    jittered = computed * (0.5 + random.random() / 2)
    if retry_after is not None:
        return max(jittered, float(retry_after))
    return jittered
