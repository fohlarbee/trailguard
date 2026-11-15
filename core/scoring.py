"""Live mule scoring.

When an institution submits transactions, each of its own personal accounts in the
batch is scored as of each transaction, using everything stored about that account
up to that moment. An account that crosses the threshold gets an alert.

An account is scored only on its own bank's submission. Every transaction an
account takes part in appears in its own bank's stream, so that is the point at
which its history is complete.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import psycopg

from scorer import model as model_store
from scorer.features import FEATURES, FeatureStream, I, reasons

from . import audit, holds

WINDOW = timedelta(hours=24)
_ACCOUNT_COLUMNS = "account, bank_id, opened_at, kyc_tier, device, bvn, account_type"
_TX_COLUMNS = """transaction_id, ts, sender_bank, sender_account, receiver_bank,
                 receiver_account, amount_kobo, channel, device"""


def _stream_for(conn: psycopg.Connection, track: dict[str, dict], since: datetime, until: datetime):
    """A FeatureStream for the tracked accounts, plus their transactions in [since, until]."""
    wanted = list(track)

    # Lifetime totals for everything before the window, so the stream need not replay it.
    seed = {a: {"n_in": 0, "n_out": 0, "max_in": 0.0, "last_in": None} for a in wanted}
    for row in conn.execute(
        """SELECT receiver_account AS account, count(*) AS n, max(amount_kobo) AS biggest, max(ts) AS latest
           FROM transactions WHERE receiver_account = ANY(%s) AND ts < %s GROUP BY 1""", (wanted, since)):
        seed[row["account"]].update(n_in=row["n"], max_in=row["biggest"] / 100, last_in=row["latest"])
    for row in conn.execute(
        """SELECT sender_account AS account, count(*) AS n FROM transactions
           WHERE sender_account = ANY(%s) AND ts < %s GROUP BY 1""", (wanted, since)):
        seed[row["account"]]["n_out"] = row["n"]

    history = conn.execute(
        f"""SELECT {_TX_COLUMNS} FROM transactions
            WHERE sender_account = ANY(%(a)s) AND ts >= %(since)s AND ts <= %(until)s
            UNION
            SELECT {_TX_COLUMNS} FROM transactions
            WHERE receiver_account = ANY(%(a)s) AND ts >= %(since)s AND ts <= %(until)s
            ORDER BY ts, transaction_id""",
        {"a": wanted, "since": since, "until": until},
    ).fetchall()

    # Senders' registered devices are needed to tell whether a session used a new one.
    accounts = dict(track)
    senders = list({tx["sender_account"] for tx in history} - set(accounts))
    for row in conn.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE account = ANY(%s)", (senders,)):
        accounts[row["account"]] = row

    by_device: dict[str, list] = {}
    by_bvn: dict[str, list] = {}
    for row in conn.execute("SELECT device, opened_at FROM accounts WHERE device = ANY(%s) ORDER BY opened_at",
                            (list({a["device"] for a in track.values()}),)):
        by_device.setdefault(row["device"], []).append(row["opened_at"])
    for row in conn.execute("SELECT bvn, opened_at FROM accounts WHERE bvn = ANY(%s) ORDER BY opened_at",
                            (list({a["bvn"] for a in track.values()}),)):
        by_bvn.setdefault(row["bvn"], []).append(row["opened_at"])

    stream = FeatureStream(accounts, track=set(track), seed=seed, sharing=(by_device, by_bvn))
    return stream, history


def score_submission(conn: psycopg.Connection, bank_id: str, transactions: list[dict]) -> int:
    """Score the submitting bank's accounts for a batch already stored. Returns new alerts."""
    scorer = model_store.current()
    if scorer is None or not transactions:
        return 0

    own = {tx["sender_account"] for tx in transactions if tx["sender_bank"] == bank_id}
    own |= {tx["receiver_account"] for tx in transactions if tx["receiver_bank"] == bank_id}
    track = {
        row["account"]: row for row in conn.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE account = ANY(%s) AND account_type = 'personal'",
            (list(own),))
    }
    if not track:
        return 0

    times = [datetime.fromisoformat(tx["timestamp"]) for tx in transactions]
    batch = {tx["transaction_id"] for tx in transactions}
    stream, history = _stream_for(conn, track, min(times) - WINDOW, max(times))

    scored = []
    for tx in history:
        for account, row in stream.apply(tx):
            if tx["transaction_id"] in batch:
                scored.append((account, tx, row))
    scores = scorer.score([row for _, _, row in scored])

    # The first transaction at which each account crossed the threshold.
    first: dict[str, tuple] = {}
    for (account, tx, row), score in zip(scored, scores):
        if score >= scorer.high and account not in first:
            first[account] = (tx, row, float(score))

    raised = 0
    for account, (tx, row, score) in first.items():
        inserted = conn.execute(
            """INSERT INTO alerts (account, bank_id, first_flagged_at, transaction_id, score, reasons)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (account) DO NOTHING RETURNING account""",
            (account, bank_id, tx["ts"], tx["transaction_id"], score, json.dumps(reasons(row))),
        ).fetchone()
        if inserted:
            raised += 1
            audit.append(conn, "account_flagged", {
                "account": account, "bank_id": bank_id, "at": tx["ts"],
                "transaction_id": tx["transaction_id"], "score": round(score, 4), "reasons": reasons(row),
            })
            # A flag places a temporary hold straight away: mules move money on within
            # minutes, long before a person could review the alert. A human confirms
            # or releases the hold afterwards, and it lapses by itself if nobody does.
            received = round(row[I["in_1h_naira"]] * 100) or tx["amount_kobo"]
            holds.place(conn, account=account, amount_kobo=received, source="auto_flag", now=tx["ts"],
                        reason="Flagged by the mule scorer: " + "; ".join(reasons(row)[:4]))
    return raised


def score_account(conn: psycopg.Connection, account: str, as_of: datetime | None = None) -> dict:
    """An account's risk as of a moment (default: now), from its latest transaction."""
    scorer = model_store.current()
    if scorer is None:
        raise RuntimeError("no trained model; run `python -m scorer train` first")
    info = conn.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE account = %s", (account,)).fetchone()
    if not info:
        raise LookupError(f"no account {account}")

    result = {"account": account, "bank_id": info["bank_id"], "account_type": info["account_type"],
              "as_of": as_of, "score": None, "tier": "unscored", "reasons": [], "features": {}}
    if info["account_type"] != "personal":
        result["reasons"] = [f"{info['account_type']} accounts are not scored"]
        return result

    latest = conn.execute(
        """SELECT max(ts) AS ts FROM transactions
           WHERE (sender_account = %(a)s OR receiver_account = %(a)s) AND ts <= %(until)s""",
        {"a": account, "until": as_of or datetime(9999, 1, 1, tzinfo=timezone.utc)},
    ).fetchone()["ts"]
    if latest is None:
        result["reasons"] = ["no transactions yet"]
        return result

    stream, history = _stream_for(conn, {account: info}, latest - WINDOW, latest)
    row = None
    for tx in history:
        for _, row in stream.apply(tx):
            pass
    score = float(scorer.score([row])[0])
    result.update(as_of=latest, score=round(score, 4), tier=scorer.tier(score), reasons=reasons(row),
                  features=dict(zip(FEATURES, row)))
    return result
