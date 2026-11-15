"""Turn a simulator run into labelled feature rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from simulator.output import read_jsonl

from .features import FeatureStream


@dataclass
class Rows:
    X: list[list[float]]
    y: list[int]
    accounts: list[str]
    times: list[datetime]
    transaction_ids: list[str]


def load_run(run: Path) -> tuple[dict, list[dict], dict]:
    """Accounts, transactions in time order, and ground truth from a run folder."""
    run = Path(run)
    accounts = {
        a["account_number"]: {
            "opened_at": datetime.fromisoformat(a["opened_at"]), "kyc_tier": a["kyc_tier"],
            "device": a["device_id"], "bvn": a["bvn"], "account_type": a["account_type"],
            "bank_id": a["bank_id"],
        }
        for a in read_jsonl(run / "accounts.jsonl")
    }
    transactions = [
        {**e, "ts": datetime.fromisoformat(e["timestamp"]), "device": e["device_id"]}
        for e in read_jsonl(run / "events.jsonl") if e["type"] == "transaction"
    ]
    truth = json.loads((run / "ground_truth.json").read_text())
    return accounts, transactions, truth


def build_rows(run: Path) -> Rows:
    """One row per personal account per transaction it took part in.

    A row is labelled 1 when the account is acting as a mule in that transaction:
    it is receiving stolen money, or moving stolen money on. The victim sending the
    theft is labelled 0, and so is a recruited mule's ordinary spending.
    """
    accounts, transactions, truth = load_run(run)
    tainted = {t["transaction_id"] for c in truth["cases"] for t in c["tainted_transactions"]}
    thefts = {tx_id for c in truth["cases"] for tx_id in c["theft_transaction_ids"]}

    stream = FeatureStream(accounts)
    rows = Rows([], [], [], [], [])
    for tx in transactions:
        for account, features in stream.apply(tx):
            is_sender = account == tx["sender_account"]
            mule_act = tx["transaction_id"] in tainted and not (is_sender and tx["transaction_id"] in thefts)
            rows.X.append(features)
            rows.y.append(int(mule_act))
            rows.accounts.append(account)
            rows.times.append(tx["ts"])
            rows.transaction_ids.append(tx["transaction_id"])
    return rows
