"""Evidence packs and suspicious transaction report drafts.

Both are assembled by code from the database. The agent can ask for them and can
supply a narrative, but it never supplies the facts or the figures.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import psycopg

from . import audit, holds


def _canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _case(conn: psycopg.Connection, case_id: str) -> dict:
    case = conn.execute("SELECT * FROM cases WHERE case_id = %s", (case_id,)).fetchone()
    if not case:
        raise LookupError(f"no case {case_id}")
    return case


def case_facts(conn: psycopg.Connection, case_id: str, now: datetime | None = None) -> dict:
    """Everything recorded about a case, as of its most recent trace."""
    case = _case(conn, case_id)
    hops = conn.execute("SELECT * FROM trail_hops WHERE case_id = %s ORDER BY seq", (case_id,)).fetchall()
    holdings = conn.execute(
        "SELECT * FROM trail_holdings WHERE case_id = %s ORDER BY hop, account", (case_id,)).fetchall()
    accounts = sorted({h["receiver_account"] for h in hops if h["receiver_account"] and not h["endpoint"]})
    return {
        "case": case,
        "reported_transactions": conn.execute(
            """SELECT t.* FROM transactions t JOIN case_transactions c USING (transaction_id)
               WHERE c.case_id = %s ORDER BY t.ts""", (case_id,)).fetchall(),
        "trail": hops,
        "holdings": holdings,
        "trail_accounts": conn.execute(
            "SELECT account, bank_id, kyc_tier, account_type, opened_at FROM accounts WHERE account = ANY(%s) ORDER BY account",
            (accounts,)).fetchall(),
        "alerts": conn.execute(
            "SELECT * FROM alerts WHERE account = ANY(%s) ORDER BY first_flagged_at", (accounts,)).fetchall(),
        # Holds linked to the case, and holds already on the trail's accounts.
        "holds": holds.listing(conn, case_id=case_id, accounts=accounts, now=now),
    }


def build_pack(conn: psycopg.Connection, case_id: str, now: datetime) -> dict:
    """Assemble, hash and store the evidence pack for a case."""
    facts = case_facts(conn, case_id, now)
    if not facts["trail"]:
        raise ValueError("the case has not been traced yet; trace it before building evidence")

    # The audit entries that concern this case or the accounts on its trail.
    accounts = [a["account"] for a in facts["trail_accounts"]]
    patterns = [f"%{case_id}%"] + [f"%{account}%" for account in accounts]
    entries = conn.execute(
        "SELECT id, kind, body, prev_hash, hash FROM audit_log WHERE body LIKE ANY(%s) ORDER BY id",
        (patterns,)).fetchall()
    chain = audit.verify(conn)

    pack = {
        "built_at": now,
        **facts,
        "audit_entries": entries,
        "audit_chain": {"valid": chain["valid"], "entries": chain["entries"], "head": chain["head"]},
    }
    pack = json.loads(_canonical(pack))
    digest = hashlib.sha256(_canonical(pack).encode()).hexdigest()
    row = conn.execute(
        "INSERT INTO evidence_packs (case_id, built_at, hash, pack) VALUES (%s, %s, %s, %s) RETURNING pack_id",
        (case_id, now, digest, json.dumps(pack)),
    ).fetchone()
    audit.append(conn, "evidence_pack_built", {
        "case_id": case_id, "pack_id": row["pack_id"], "hash": digest, "at": now})
    return {
        "pack_id": row["pack_id"], "hash": digest, "audit_chain_valid": chain["valid"],
        "trail_transactions": len(facts["trail"]), "holds": len(facts["holds"]),
        "alerts": len(facts["alerts"]), "audit_entries": len(entries),
    }


def draft_str(conn: psycopg.Connection, case_id: str, narrative: str, now: datetime) -> dict:
    """Store a suspicious transaction report draft. Facts come from the database."""
    facts = case_facts(conn, case_id, now)
    if not facts["trail"]:
        raise ValueError("the case has not been traced yet; trace it before drafting a report")
    case, trail = facts["case"], facts["trail"]
    exited: dict[str, int] = {}
    for hop in trail:
        if hop["endpoint"]:
            exited[hop["endpoint"]] = exited.get(hop["endpoint"], 0) + hop["tainted_kobo"]
    body = {
        "case_id": case_id,
        "reporting_institution": case["reporting_bank"],
        "reported_at": case["reported_at"],
        "subject_of_fraud": {"account": case["victim_account"], "role": "victim"},
        "amount_stolen_kobo": case["stolen_kobo"],
        "first_transaction_at": trail[0]["ts"],
        "last_transaction_at": trail[-1]["ts"],
        "institutions_involved": sorted({h["sender_bank"] for h in trail}
                                        | {h["receiver_bank"] for h in trail if h["receiver_bank"]}),
        "suspected_accounts": [
            {"account": a["account"], "institution": a["bank_id"], "opened_at": a["opened_at"],
             "kyc_tier": a["kyc_tier"]} for a in facts["trail_accounts"]],
        "money_that_left_the_system_kobo": exited,
        "money_still_in_accounts_kobo": sum(h["tainted_kobo"] for h in facts["holdings"]),
        "holds": [{"hold_id": h["hold_id"], "account": h["account"], "status": h["status"],
                   "requested_kobo": h["requested_kobo"], "held_kobo": h["held_kobo"]} for h in facts["holds"]],
        "transactions": [h["transaction_id"] for h in trail],
        "narrative": narrative,
    }
    body = json.loads(_canonical(body))
    row = conn.execute(
        "INSERT INTO str_drafts (case_id, drafted_at, body) VALUES (%s, %s, %s) RETURNING draft_id",
        (case_id, now, json.dumps(body)),
    ).fetchone()
    audit.append(conn, "str_drafted", {"case_id": case_id, "draft_id": row["draft_id"], "at": now})
    return {"draft_id": row["draft_id"], "status": "draft",
            "note": "A human must review and file this report; it has not been submitted."}
