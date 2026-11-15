"""Read models for the fraud desk dashboard. Everything is computed as of `now`."""

from __future__ import annotations

from datetime import datetime

import psycopg

from . import holds, tracer


def _trail_accounts(trail: tracer.Trail) -> list[str]:
    return sorted({h.receiver_account for h in trail.hops if h.receiver_account and not h.endpoint})


def _held(rows: list[dict], trail: tracer.Trail) -> int:
    """This case's stolen money that is under an active hold.

    A hold may cover more than this case's money (the account can hold other
    funds), so each account counts only up to what the trace shows is there.
    """
    in_account = {h.account: h.tainted_kobo for h in trail.holdings}
    return sum(min(h["held_kobo"] or 0, in_account.get(h["account"], 0)) for h in rows if h["status"] == "active")


def _figures(conn: psycopg.Connection, case: dict, now: datetime) -> tuple[dict, tracer.Trail, list[dict]]:
    trail = tracer.trace(conn, case["case_id"], as_of=now)
    on_trail = holds.listing(conn, case_id=case["case_id"], accounts=_trail_accounts(trail), now=now)
    return {
        "minutes_since_report": round((now - case["reported_at"]).total_seconds() / 60, 1),
        "stolen_kobo": trail.stolen_kobo,
        "exited_kobo": trail.exited_kobo,
        "in_accounts_kobo": trail.holdable_kobo,
        "held_kobo": _held(on_trail, trail),
        "holds_awaiting_review": sum(h["status"] in holds.OPEN and h["review"] is None for h in on_trail),
    }, trail, on_trail


def case_list(conn: psycopg.Connection, now: datetime) -> list[dict]:
    """Every case reported so far, newest first, with its current figures."""
    cases = conn.execute(
        """SELECT c.case_id, c.reporting_bank, c.reported_at, c.victim_account,
                  (SELECT outcome FROM agent_runs r WHERE r.case_id = c.case_id AND r.as_of <= %s
                   ORDER BY run_id DESC LIMIT 1) AS agent_outcome
           FROM cases c WHERE c.reported_at <= %s ORDER BY c.reported_at DESC""", (now, now)).fetchall()
    return [{**case, **_figures(conn, case, now)[0]} for case in cases]


def case_detail(conn: psycopg.Connection, case_id: str, now: datetime, bank: str) -> dict:
    case = conn.execute("SELECT * FROM cases WHERE case_id = %s", (case_id,)).fetchone()
    if not case:
        raise LookupError(f"no case {case_id}")
    figures, trail, on_trail = _figures(conn, case, now)
    accounts = _trail_accounts(trail)
    alerts = {a["account"]: a for a in conn.execute(
        "SELECT * FROM alerts WHERE account = ANY(%s) AND first_flagged_at <= %s", (accounts, now))}
    in_account = {h.account: h.tainted_kobo for h in trail.holdings}
    pack = conn.execute(
        """SELECT pack_id, built_at, hash FROM evidence_packs WHERE case_id = %s AND built_at <= %s
           ORDER BY pack_id DESC LIMIT 1""", (case_id, now)).fetchone()
    draft = conn.execute(
        """SELECT draft_id, drafted_at, status, body FROM str_drafts WHERE case_id = %s AND drafted_at <= %s
           ORDER BY draft_id DESC LIMIT 1""", (case_id, now)).fetchone()
    return {
        **case, **figures, "now": now,
        "hops": trail.to_dict()["hops"],
        "holdings": trail.to_dict()["holdings"],
        "accounts": [
            {**row, "stolen_in_account_kobo": in_account.get(row["account"], 0),
             "alert": alerts.get(row["account"])}
            for row in conn.execute(
                """SELECT account, bank_id, kyc_tier, opened_at FROM accounts
                   WHERE account = ANY(%s) ORDER BY account""", (accounts,))],
        "holds": [{**h, "mine": h["bank_id"] == bank} for h in on_trail],
        "runs": conn.execute(
            """SELECT run_id, as_of, model, outcome, summary, steps FROM agent_runs
               WHERE case_id = %s AND as_of <= %s ORDER BY run_id DESC""", (case_id, now)).fetchall(),
        "evidence": pack,
        "str_draft": draft,
    }
