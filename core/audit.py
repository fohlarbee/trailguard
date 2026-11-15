"""The evidence ledger: an append-only, hash-chained log."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import psycopg

GENESIS = "0" * 64
_LOCK_ID = 727001


def _digest(prev_hash: str, body: str) -> str:
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


def append(conn: psycopg.Connection, kind: str, payload: dict) -> str:
    """Add an entry and return its hash. Runs inside the caller's transaction."""
    # One writer at a time, so two entries can never claim the same predecessor.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))
    last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = last["hash"] if last else GENESIS
    body = json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, "payload": payload},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    entry_hash = _digest(prev_hash, body)
    conn.execute(
        "INSERT INTO audit_log (kind, body, prev_hash, hash) VALUES (%s, %s, %s, %s)",
        (kind, body, prev_hash, entry_hash),
    )
    return entry_hash


def verify(conn: psycopg.Connection) -> dict:
    """Recompute the chain. Reports the first entry that does not match, if any."""
    prev_hash, count = GENESIS, 0
    for row in conn.execute("SELECT id, body, prev_hash, hash FROM audit_log ORDER BY id"):
        if row["prev_hash"] != prev_hash or row["hash"] != _digest(prev_hash, row["body"]):
            return {"valid": False, "entries": count, "broken_at": row["id"], "head": prev_hash}
        prev_hash, count = row["hash"], count + 1
    return {"valid": True, "entries": count, "broken_at": None, "head": prev_hash}
