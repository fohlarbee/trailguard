"""Grade the tracer against a simulator run's ground truth.

The run must already be loaded into the database (see `python -m connector push`).
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg

from connector.hashing import hash_value

from . import tracer


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 1.0


def evaluate(conn: psycopg.Connection, run: Path, salt: str, **trace_options) -> dict:
    truth = json.loads((Path(run) / "ground_truth.json").read_text())
    rows = []
    # Rings are reused, so one mule can be moving two victims' money at once. A
    # transaction the tracer attaches to the wrong case is still fraud; one that is
    # in no case at all is an innocent transaction wrongly flagged.
    any_case = {t["transaction_id"] for case in truth["cases"] for t in case["tainted_transactions"]}

    for case in truth["cases"]:
        case_id = f"{case['victim_bank']}-{case['report_id']}"
        stored = conn.execute("SELECT reported_at FROM cases WHERE case_id = %s", (case_id,)).fetchone()
        if not stored:
            raise LookupError(f"{case_id} is not in the database; load the run first")

        full = tracer.trace(conn, case_id, **trace_options)
        at_report = tracer.trace(conn, case_id, as_of=stored["reported_at"], **trace_options)

        true_txs = {t["transaction_id"]: t["tainted_kobo"] for t in case["tainted_transactions"]}
        found_txs = {h.transaction_id: h.tainted_kobo for h in full.hops}
        true_mules = {hash_value(salt, "account", m["account"]) for m in case["mule_accounts"]}
        found_mules = {h.receiver_account for h in full.hops if not h.endpoint}

        # What was really still in mule accounts when the victim reported.
        cash_ids = [p["transaction_id"] for p in case["cash_out_points"]]
        gone_by_report = {
            r["transaction_id"] for r in conn.execute(
                "SELECT transaction_id FROM transactions WHERE transaction_id = ANY(%s) AND ts <= %s",
                (cash_ids, stored["reported_at"]),
            )
        }
        true_in_system = case["stolen_kobo"] - sum(
            p["tainted_kobo"] for p in case["cash_out_points"] if p["transaction_id"] in gone_by_report)

        rows.append({
            "case_id": case["case_id"],
            "scenario": case["scenario"],
            "stolen_kobo": case["stolen_kobo"],
            "exact": found_txs == true_txs,
            "tx_true": len(true_txs),
            "tx_found": len(found_txs),
            "tx_correct": len(true_txs.keys() & found_txs.keys()),
            "tx_fraud": len(any_case & found_txs.keys()),
            "mule_true": len(true_mules),
            "mule_found": len(found_mules),
            "mule_correct": len(true_mules & found_mules),
            "holdable_at_report_kobo": at_report.holdable_kobo,
            "true_in_system_at_report_kobo": true_in_system,
        })

    def total(key: str, subset=rows) -> int:
        return sum(r[key] for r in subset)

    def scores(subset: list[dict]) -> dict:
        return {
            "cases": len(subset),
            "exact_trails": sum(r["exact"] for r in subset),
            "transaction_recall": _ratio(total("tx_correct", subset), total("tx_true", subset)),
            "transaction_precision": _ratio(total("tx_correct", subset), total("tx_found", subset)),
            "fraud_precision": _ratio(total("tx_fraud", subset), total("tx_found", subset)),
            "innocent_transactions_flagged": total("tx_found", subset) - total("tx_fraud", subset),
            "mule_recall": _ratio(total("mule_correct", subset), total("mule_true", subset)),
            "mule_precision": _ratio(total("mule_correct", subset), total("mule_found", subset)),
        }

    scenarios = sorted({r["scenario"] for r in rows})
    return {
        "overall": scores(rows),
        "by_scenario": {s: scores([r for r in rows if r["scenario"] == s]) for s in scenarios},
        "at_report_time": {
            "stolen_naira": total("stolen_kobo") // 100,
            "holdable_naira_per_tracer": total("holdable_at_report_kobo") // 100,
            "holdable_naira_actual": total("true_in_system_at_report_kobo") // 100,
            "holdable_share_actual": _ratio(total("true_in_system_at_report_kobo"), total("stolen_kobo")),
            "cases_with_nothing_left": sum(r["true_in_system_at_report_kobo"] * 20 < r["stolen_kobo"] for r in rows),
        },
        "cases": rows,
    }
