"""Writers for a run's output folder."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .config import Config
from .ledger import Ledger
from .schema import event_banks
from .world import World


def _jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_run(out: Path, cfg: Config, world: World, ledger: Ledger, truth: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    events = [event.model_dump() for event in ledger.events]

    accounts = []
    for account in world.accounts.values():
        row = ledger.account_record(account).model_dump()
        row["opening_balance_kobo"] = account.opening_balance
        accounts.append(row)

    _json(out / "institutions.json", [asdict(i) for i in world.institutions.values()])
    _jsonl(out / "accounts.jsonl", accounts)
    _jsonl(out / "events.jsonl", events)
    _json(out / "ground_truth.json", truth)

    # Each institution's own view: only the events it took part in.
    views: dict[str, list[dict]] = {bank: [] for bank in world.institutions}
    for event in events:
        for bank in event_banks(event):
            views[bank].append(event)
    for bank, rows in views.items():
        (out / "banks" / bank).mkdir(parents=True, exist_ok=True)
        _jsonl(out / "banks" / bank / "events.jsonl", rows)

    summary = summarise(cfg, accounts, events, truth)
    _json(out / "summary.json", summary)
    return summary


def summarise(cfg: Config, accounts: list[dict], events: list[dict], truth: dict) -> dict:
    transactions = [e for e in events if e["type"] == "transaction"]
    cases = truth["cases"]
    stolen = sum(c["stolen_kobo"] for c in cases)

    def by(key: str) -> dict:
        groups: dict[str, dict] = {}
        for case in cases:
            group = groups.setdefault(case[key], {"cases": 0, "stolen_naira": 0})
            group["cases"] += 1
            group["stolen_naira"] += case["stolen_kobo"] // 100
        for group in groups.values():
            group["share_of_cases"] = round(group["cases"] / len(cases), 3)
            group["share_of_loss"] = round(group["stolen_naira"] * 100 / stolen, 3) if stolen else 0.0
            group["average_loss_naira"] = group["stolen_naira"] // group["cases"]
        return dict(sorted(groups.items()))

    channel_counts: dict[str, int] = {}
    for tx in transactions:
        channel_counts[tx["channel"]] = channel_counts.get(tx["channel"], 0) + 1

    cashed = sum(p["tainted_kobo"] for c in cases for p in c["cash_out_points"])
    lookalike_kinds: dict[str, int] = {}
    for item in truth["lookalikes"]:
        lookalike_kinds[item["kind"]] = lookalike_kinds.get(item["kind"], 0) + 1

    return {
        "seed": cfg.seed,
        "start": cfg.start.isoformat(),
        "days": cfg.days,
        "institutions": cfg.banks + cfg.fintechs,
        "accounts": len(accounts),
        "events": len(events),
        "transactions": len(transactions),
        "transaction_value_naira": sum(tx["amount_kobo"] for tx in transactions) // 100,
        "transactions_by_channel": dict(sorted(channel_counts.items())),
        "fraud": {
            "cases": len(cases),
            "cases_requested": cfg.fraud_cases,
            "reports": sum(1 for e in events if e["type"] == "fraud_report"),
            "stolen_naira": stolen // 100,
            "average_loss_naira": stolen // 100 // len(cases) if cases else 0,
            "cashed_out_naira": cashed // 100,
            "left_in_mule_accounts_naira": (stolen - cashed) // 100,
            "tainted_transactions": sum(len(c["tainted_transactions"]) for c in cases),
            "mule_accounts": len(truth["mule_accounts"]),
            "rings": len(truth["rings"]),
            "by_scenario": by("scenario"),
            "by_method": by("method"),
            "by_channel": by("channel"),
        },
        "lookalikes": dict(sorted(lookalike_kinds.items())),
    }
