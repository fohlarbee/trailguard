"""Stream a run's events in time order, as a bank would send them.

For now events go to stdout, one JSON record per line. An HTTP sink is added when
the ingest service exists.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from .output import read_jsonl
from .schema import event_time


def replay(run: Path, speed: float = 0, bank: str | None = None, limit: int | None = None) -> int:
    """Print events, pausing between them so that `speed` simulated seconds pass per second.

    speed=0 prints everything immediately. bank restricts the stream to one institution's view.
    """
    path = Path(run) / "banks" / bank / "events.jsonl" if bank else Path(run) / "events.jsonl"
    previous = None
    count = 0
    for event in read_jsonl(path):
        if limit is not None and count >= limit:
            break
        now = datetime.fromisoformat(event_time(event))
        if speed > 0 and previous is not None:
            time.sleep(max((now - previous).total_seconds(), 0) / speed)
        previous = now
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        count += 1
    return count
