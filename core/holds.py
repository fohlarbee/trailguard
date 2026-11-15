"""Holds: temporary by default, reviewed by a human afterwards.

A hold is placed automatically when the scorer flags an account, or by the agent
on an account in a case's trail. It is sent to the account's bank, which reports
how much it actually held. Unless a human confirms it, the hold lapses when it
expires. A human can release it at any time.

Every function takes the current time, so the service can be replayed against
simulated data on the simulation's own clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg

from . import audit

DEFAULT_TTL = timedelta(hours=24)
REMIND_WAIT = timedelta(minutes=10)     # how long a bank gets before it can be chased again
OPEN = ("requested", "active")


def effective_status(hold: dict, now: datetime) -> str:
    """A hold's status at a given moment: an unconfirmed hold past its expiry has lapsed."""
    if hold["status"] in OPEN and hold["review"] is None and hold["expires_at"] <= now:
        return "expired"
    return hold["status"]


class HoldError(ValueError):
    pass


def _get(conn: psycopg.Connection, hold_id: int) -> dict:
    hold = conn.execute("SELECT * FROM holds WHERE hold_id = %s", (hold_id,)).fetchone()
    if not hold:
        raise LookupError(f"no hold {hold_id}")
    return hold


def expire(conn: psycopg.Connection, now: datetime, account: str | None = None) -> int:
    """Record as lapsed every unconfirmed hold whose time is up (optionally one account's)."""
    lapsed = conn.execute(
        """UPDATE holds SET status = 'expired'
           WHERE status = ANY(%s) AND review IS NULL AND expires_at <= %s
             AND (%s::text IS NULL OR account = %s) RETURNING hold_id, account""",
        (list(OPEN), now, account, account),
    ).fetchall()
    for hold in lapsed:
        audit.append(conn, "hold_expired", {"hold_id": hold["hold_id"], "account": hold["account"], "at": now})
    return len(lapsed)


def place(
    conn: psycopg.Connection,
    *,
    account: str,
    amount_kobo: int,
    reason: str,
    source: str,
    now: datetime,
    case_id: str | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> dict:
    """Ask the account's bank to hold money. Returns the hold, with `already_held`.

    If the account already has an open hold, that hold is reused: it is linked to
    the case and its amount raised if this request is larger.
    """
    if amount_kobo <= 0:
        raise HoldError("the amount to hold must be positive")
    info = conn.execute("SELECT bank_id FROM accounts WHERE account = %s", (account,)).fetchone()
    if not info:
        raise HoldError(f"account {account} is not at a connected institution, so it cannot be held")

    expire(conn, now, account)
    existing = conn.execute(
        "SELECT * FROM holds WHERE account = %s AND status = ANY(%s)", (account, list(OPEN))).fetchone()
    if existing:
        updated = conn.execute(
            """UPDATE holds SET case_id = COALESCE(case_id, %s), requested_kobo = GREATEST(requested_kobo, %s)
               WHERE hold_id = %s RETURNING *""",
            (case_id, amount_kobo, existing["hold_id"]),
        ).fetchone()
        if updated["case_id"] != existing["case_id"] or updated["requested_kobo"] != existing["requested_kobo"]:
            audit.append(conn, "hold_updated", {
                "hold_id": updated["hold_id"], "account": account, "case_id": updated["case_id"],
                "requested_kobo": updated["requested_kobo"], "by": source, "at": now,
            })
        return {**updated, "already_held": True}

    hold = conn.execute(
        """INSERT INTO holds (account, bank_id, case_id, requested_kobo, reason, source, placed_at, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (account, info["bank_id"], case_id, amount_kobo, reason, source, now, now + ttl),
    ).fetchone()
    audit.append(conn, "hold_placed", {
        "hold_id": hold["hold_id"], "account": account, "bank_id": info["bank_id"], "case_id": case_id,
        "requested_kobo": amount_kobo, "reason": reason, "source": source, "at": now,
        "expires_at": hold["expires_at"],
    })
    return {**hold, "already_held": False}


def acknowledge(conn: psycopg.Connection, hold_id: int, bank_id: str, held_kobo: int, now: datetime) -> dict:
    """The bank reports how much it held. Zero means there was nothing left to hold."""
    hold = _get(conn, hold_id)
    if hold["bank_id"] != bank_id:
        raise HoldError("only the account's own bank can acknowledge a hold")
    if hold["status"] != "requested":
        raise HoldError(f"hold {hold_id} is {hold['status']}, not awaiting acknowledgement")
    if held_kobo < 0:
        raise HoldError("the held amount cannot be negative")
    status = "active" if held_kobo > 0 else "declined"
    updated = conn.execute(
        "UPDATE holds SET status = %s, held_kobo = %s, acknowledged_at = %s WHERE hold_id = %s RETURNING *",
        (status, held_kobo, now, hold_id),
    ).fetchone()
    audit.append(conn, "hold_acknowledged", {
        "hold_id": hold_id, "bank_id": bank_id, "held_kobo": held_kobo, "status": status, "at": now})
    return updated


def remind(conn: psycopg.Connection, hold_id: int, now: datetime) -> dict:
    """Chase a bank that has not yet acknowledged a hold."""
    hold = _get(conn, hold_id)
    status = effective_status(hold, now)
    if status != "requested":
        raise HoldError(f"hold {hold_id} is {status}; only unacknowledged holds can be chased")
    last_contact = hold["last_reminded_at"] or hold["placed_at"]
    if now - last_contact < REMIND_WAIT:
        raise HoldError(f"the bank was last contacted at {last_contact.isoformat()}; "
                        f"it can be chased again from {(last_contact + REMIND_WAIT).isoformat()}")
    updated = conn.execute(
        "UPDATE holds SET reminders = reminders + 1, last_reminded_at = %s WHERE hold_id = %s RETURNING *",
        (now, hold_id),
    ).fetchone()
    audit.append(conn, "hold_reminder_sent", {
        "hold_id": hold_id, "bank_id": hold["bank_id"], "reminders": updated["reminders"], "at": now})
    return updated


def review(conn: psycopg.Connection, hold_id: int, decision: str, reviewer: str, now: datetime) -> dict:
    """A human's decision: confirm (keep it past its expiry) or release."""
    if decision not in ("confirm", "release"):
        raise HoldError("decision must be 'confirm' or 'release'")
    if not reviewer.strip():
        raise HoldError("a review needs a named reviewer")
    hold = _get(conn, hold_id)
    if effective_status(hold, now) not in OPEN:
        raise HoldError(f"hold {hold_id} is already {effective_status(hold, now)}")
    updated = conn.execute(
        """UPDATE holds SET review = %s, reviewed_by = %s, reviewed_at = %s, status = %s
           WHERE hold_id = %s RETURNING *""",
        ("confirmed" if decision == "confirm" else "released", reviewer, now,
         hold["status"] if decision == "confirm" else "released", hold_id),
    ).fetchone()
    audit.append(conn, "hold_reviewed", {
        "hold_id": hold_id, "decision": decision, "reviewer": reviewer, "at": now})
    return updated


def traced_in_account(conn: psycopg.Connection, account: str) -> int:
    """Stolen money the latest traces show in an account, across every case."""
    row = conn.execute(
        "SELECT COALESCE(sum(tainted_kobo), 0) AS total FROM trail_holdings WHERE account = %s", (account,)).fetchone()
    return int(row["total"])


def recommend_reduction(conn: psycopg.Connection, hold_id: int, amount_kobo: int, reason: str,
                        now: datetime) -> dict:
    """Record a recommendation to hold less. Nothing is released until a human acts on it.

    The amount can never go below the stolen money traced into the account, so a
    recommendation cannot be used to let stolen money go.
    """
    hold = _get(conn, hold_id)
    if effective_status(hold, now) != "active":
        raise HoldError(f"hold {hold_id} is {effective_status(hold, now)}; only an active hold can be reduced")
    floor = traced_in_account(conn, hold["account"])
    if amount_kobo < floor:
        raise HoldError(f"traces show N{floor / 100:,.2f} of stolen money in this account; "
                        "the hold cannot be recommended below that")
    if amount_kobo >= hold["held_kobo"]:
        raise HoldError(f"the bank is holding N{hold['held_kobo'] / 100:,.2f}; a reduction must be less than that")
    updated = conn.execute(
        "UPDATE holds SET recommended_kobo = %s, recommendation = %s WHERE hold_id = %s RETURNING *",
        (amount_kobo, reason, hold_id)).fetchone()
    audit.append(conn, "hold_reduction_recommended", {
        "hold_id": hold_id, "from_kobo": hold["held_kobo"], "to_kobo": amount_kobo, "reason": reason, "at": now})
    return updated


def reduce(conn: psycopg.Connection, hold_id: int, reviewer: str, now: datetime) -> dict:
    """A human accepts the recommended reduction; the bank releases the difference."""
    if not reviewer.strip():
        raise HoldError("a reduction needs a named reviewer")
    hold = _get(conn, hold_id)
    if hold["recommended_kobo"] is None:
        raise HoldError(f"hold {hold_id} has no recommended reduction")
    if effective_status(hold, now) != "active":
        raise HoldError(f"hold {hold_id} is {effective_status(hold, now)}")
    updated = conn.execute(
        """UPDATE holds SET held_kobo = recommended_kobo, requested_kobo = recommended_kobo,
                  recommended_kobo = NULL WHERE hold_id = %s RETURNING *""", (hold_id,)).fetchone()
    audit.append(conn, "hold_reduced", {
        "hold_id": hold_id, "from_kobo": hold["held_kobo"], "to_kobo": updated["held_kobo"],
        "reviewer": reviewer, "at": now})
    return updated


def listing(conn: psycopg.Connection, *, case_id: str | None = None, bank_id: str | None = None,
            status: str | None = None, accounts: list[str] | None = None,
            now: datetime | None = None) -> list[dict]:
    """Holds matching the filters. With `now`, only holds placed by then, with their status at that moment.

    case_id and accounts together match holds linked to the case or on any of the accounts.
    """
    clauses, values = [], []
    if case_id is not None and accounts is not None:
        clauses.append("(case_id = %s OR account = ANY(%s))")
        values += [case_id, accounts]
    elif case_id is not None:
        clauses.append("case_id = %s")
        values.append(case_id)
    elif accounts is not None:
        clauses.append("account = ANY(%s)")
        values.append(accounts)
    if bank_id is not None:
        clauses.append("bank_id = %s")
        values.append(bank_id)
    if now is not None:
        clauses.append("placed_at <= %s")
        values.append(now)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM holds {where} ORDER BY placed_at, hold_id", values).fetchall()
    if now is not None:
        rows = [{**row, "status": effective_status(row, now)} for row in rows]
    return [row for row in rows if status is None or row["status"] == status]
