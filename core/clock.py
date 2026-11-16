"""The service clock.

Normally this is the real time. For replayed simulator data, set TRAILGUARD_CLOCK
to an ISO time: the clock then starts there when the process starts and ticks
forward normally, and anything dated after it is treated as not having happened yet.

The live demo sets the clock itself with `set_pace`, which also lets it run faster
than real time. That setting wins over TRAILGUARD_CLOCK until `clear` is called.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

_started = datetime.now(timezone.utc)
# (simulated time, real time, rate) at the moment the demo last set the pace.
_pace: tuple[datetime, datetime, float] | None = None


def _real() -> datetime:
    return datetime.now(timezone.utc)


def simulated() -> bool:
    return _pace is not None or bool(os.environ.get("TRAILGUARD_CLOCK"))


def rate() -> float:
    """How many simulated seconds pass per real second."""
    return _pace[2] if _pace else 1.0


def now() -> datetime:
    real = _real()
    if _pace is not None:
        anchor, since, speed = _pace
        return anchor + (real - since) * speed
    anchor = os.environ.get("TRAILGUARD_CLOCK")
    if not anchor:
        return real
    return datetime.fromisoformat(anchor).astimezone(timezone.utc) + (real - _started)


def set_pace(speed: float, at: datetime | None = None) -> tuple[datetime, datetime, float]:
    """From now on, run at `speed`, starting from `at` (default: the current clock time)."""
    global _pace
    start = (at or now()).astimezone(timezone.utc)
    _pace = (start, _real(), speed)
    return _pace


def restore(anchor: datetime, since: datetime, speed: float) -> None:
    """Resume a pace saved earlier, e.g. after the service restarts."""
    global _pace
    _pace = (anchor, since, speed)


def clear() -> None:
    global _pace
    _pace = None
