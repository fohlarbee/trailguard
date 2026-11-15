"""Store events sent by an institution's connector."""

from __future__ import annotations

import psycopg
from pydantic import TypeAdapter, ValidationError

from simulator.schema import Event, event_banks

from . import audit, scoring

_events = TypeAdapter(list[Event])


class IngestError(ValueError):
    """The batch is malformed or not this institution's to send."""


def store_events(conn: psycopg.Connection, bank_id: str, events: list[dict]) -> dict:
    """Validate and store a batch. Commits on success; stores nothing on failure.

    Events arrive already hashed. An institution may only send events it took part
    in. Records seen before are ignored, so batches are safe to resend, and a
    transfer reported by both of its banks is stored once.
    """
    try:
        parsed = [e.model_dump() for e in _events.validate_python(events)]
    except ValidationError as error:
        raise IngestError(f"invalid event: {error.errors()[0]}") from error
    for event in parsed:
        if bank_id not in event_banks(event):
            raise IngestError(f"{bank_id} is not a party to {event['type']} event")

    accounts = [e for e in parsed if e["type"] == "account_opened"]
    transactions = [e for e in parsed if e["type"] == "transaction"]
    reports = [e for e in parsed if e["type"] == "fraud_report"]
    counts = {"received": len(parsed), "accounts": 0, "transactions": 0, "cases_opened": 0, "alerts_raised": 0}

    with conn.transaction():
        with conn.cursor() as cur:
            if accounts:
                cur.executemany(
                    """INSERT INTO accounts (account, bank_id, bvn, phone, device, kyc_tier, account_type, opened_at)
                       VALUES (%(account_number)s, %(bank_id)s, %(bvn)s, %(phone)s, %(device_id)s,
                               %(kyc_tier)s, %(account_type)s, %(opened_at)s)
                       ON CONFLICT (account) DO NOTHING""",
                    accounts,
                )
                counts["accounts"] = cur.rowcount
            if transactions:
                cur.executemany(
                    """INSERT INTO transactions (session_ref, transaction_id, ts, sender_bank, sender_account,
                                                 receiver_bank, receiver_account, amount_kobo, channel, device)
                       VALUES (%(session_ref)s, %(transaction_id)s, %(timestamp)s, %(sender_bank)s,
                               %(sender_account)s, %(receiver_bank)s, %(receiver_account)s,
                               %(amount_kobo)s, %(channel)s, %(device_id)s)
                       ON CONFLICT (session_ref) DO NOTHING""",
                    transactions,
                )
                counts["transactions"] = cur.rowcount

        # Scored even when the transfer was already stored by the other bank: this
        # is the submission that completes this bank's accounts' history.
        counts["alerts_raised"] = scoring.score_submission(conn, bank_id, transactions)

        for report in reports:
            case_id = f"{report['reporting_bank']}-{report['report_id']}"
            opened = conn.execute(
                """INSERT INTO cases (case_id, report_id, reporting_bank, reported_at, victim_account)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING case_id""",
                (case_id, report["report_id"], report["reporting_bank"], report["reported_at"],
                 report["victim_account"]),
            ).fetchone()
            if not opened:
                continue
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO case_transactions (case_id, transaction_id) VALUES (%s, %s)",
                    [(case_id, tx_id) for tx_id in report["transaction_ids"]],
                )
            audit.append(conn, "case_opened", {
                "case_id": case_id, "reporting_bank": report["reporting_bank"],
                "reported_at": report["reported_at"], "victim_account": report["victim_account"],
                "transaction_ids": report["transaction_ids"],
            })
            counts["cases_opened"] += 1

    counts["duplicates"] = len(parsed) - counts["accounts"] - counts["transactions"] - counts["cases_opened"]
    return counts
