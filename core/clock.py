"""The service clock.

Normally this is the real time. For replayed simulator data, set TRAILGUARD_CLOCK
to an ISO time: the clock then starts there when the process starts and ticks
forward normally, and anything dated after it is treated as not having happened yet.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

_started = datetime.now(timezone.utc)


def simulated() -> bool:
    return bool(os.environ.get("TRAILGUARD_CLOCK"))


def now() -> datetime:
    real = datetime.now(timezone.utc)
    anchor = os.environ.get("TRAILGUARD_CLOCK")
    if not anchor:
        return real
    return datetime.fromisoformat(anchor).astimezone(timezone.utc) + (real - _started)
