"""Grade the scorer on a simulator run it was not trained on.

The question that matters is timing: is a mule account flagged before the money
leaves it, and before the victim has even reported?
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from statistics import median

import numpy as np

from .data import build_rows, load_run


def first_flags(rows, scores: np.ndarray, threshold: float) -> dict[str, datetime]:
    """When each account first scored at or above the threshold."""
    flagged: dict[str, datetime] = {}
    for account, when, score in zip(rows.accounts, rows.times, scores):
        if score >= threshold and account not in flagged:
            flagged[account] = when
    return flagged


def evaluate(run: Path, scores_for, threshold: float) -> dict:
    """scores_for(rows) -> array of scores, so any model variant can be graded."""
    accounts, transactions, truth = load_run(run)
    rows = build_rows(run)
    flagged = first_flags(rows, scores_for(rows), threshold)
    tx_time = {tx["transaction_id"]: tx["ts"] for tx in transactions}
    tx_by_id = {tx["transaction_id"]: tx for tx in transactions}

    mules = {m["account"] for m in truth["mule_accounts"]}
    personal = {a for a, info in accounts.items() if info["account_type"] == "personal"}
    legit = personal - mules
    lookalike_kind = {item["account"]: item["kind"] for item in truth["lookalikes"]}
    false_flags = sorted(set(flagged) & legit)
    false_kinds: dict[str, int] = {}
    for account in false_flags:
        kind = lookalike_kind.get(account, "ordinary_customer")
        false_kinds[kind] = false_kinds.get(kind, 0) + 1

    # Rings are reused, so a mule may already be flagged from an earlier case. The
    # harder test is a mule's first job: flagged on this case's evidence alone.
    first_job: dict[str, str] = {}
    for case in sorted(truth["cases"], key=lambda c: tx_time[c["theft_transaction_ids"][0]]):
        for mule in case["mule_accounts"]:
            first_job.setdefault(mule["account"], case["case_id"])

    stolen = interceptable = 0
    first_hop_caught = before_report = 0
    fresh = fresh_caught = 0
    lead_times, act_windows, fresh_windows = [], [], []
    for case in truth["cases"]:
        stolen += case["stolen_kobo"]
        thefts = set(case["theft_transaction_ids"])
        first_hop = tx_by_id[case["theft_transaction_ids"][0]]["receiver_account"]
        onward = [t for t in case["tainted_transactions"] if t["transaction_id"] not in thefts]

        # Was the first mule flagged before it moved any of the money on?
        moved_at = min((tx_time[t["transaction_id"]] for t in onward
                        if tx_by_id[t["transaction_id"]]["sender_account"] == first_hop), default=None)
        flag_at = flagged.get(first_hop)
        is_fresh = first_job.get(first_hop) == case["case_id"]
        fresh += is_fresh
        if flag_at and (moved_at is None or flag_at < moved_at):
            first_hop_caught += 1
            fresh_caught += is_fresh
            interceptable += case["stolen_kobo"]
            if moved_at:
                act_windows.append((moved_at - flag_at).total_seconds())
                if is_fresh:
                    fresh_windows.append((moved_at - flag_at).total_seconds())
        else:
            # Otherwise, count what left through cash-out accounts already flagged.
            for point in case["cash_out_points"]:
                tx = tx_by_id[point["transaction_id"]]
                when = flagged.get(tx["sender_account"])
                if when and when < tx["ts"]:
                    interceptable += point["tainted_kobo"]

        trail_flags = [flagged[m["account"]] for m in case["mule_accounts"] if m["account"] in flagged]
        reported = datetime.fromisoformat(case["reported_at"])
        if trail_flags and min(trail_flags) < reported:
            before_report += 1
            lead_times.append((reported - min(trail_flags)).total_seconds())

    cases = len(truth["cases"])
    return {
        "threshold": round(float(threshold), 4),
        "accounts": {
            "mules": len(mules),
            "mules_flagged": len(set(flagged) & mules),
            "mule_recall": round(len(set(flagged) & mules) / len(mules), 4) if mules else 1.0,
            "legitimate_accounts": len(legit),
            "legitimate_flagged": len(false_flags),
            "false_flag_rate": round(len(false_flags) / len(legit), 5),
            "legitimate_flagged_by_kind": dict(sorted(false_kinds.items())),
        },
        "timing": {
            "cases": cases,
            "first_mule_flagged_before_it_moved_money": first_hop_caught,
            "cases_flagged_before_victim_report": before_report,
            "median_minutes_ahead_of_report": round(median(lead_times) / 60, 1) if lead_times else None,
            "median_seconds_to_act_before_money_moves": round(median(act_windows)) if act_windows else None,
            "first_job_cases": fresh,
            "first_job_mule_flagged_before_it_moved_money": fresh_caught,
            "first_job_median_seconds_to_act": round(median(fresh_windows)) if fresh_windows else None,
        },
        "money": {
            "stolen_naira": stolen // 100,
            "interceptable_naira": interceptable // 100,
            "interceptable_share": round(interceptable / stolen, 4) if stolen else 0.0,
        },
    }
