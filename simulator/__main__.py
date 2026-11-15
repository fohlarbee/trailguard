"""Command line: python -m simulator <generate|summary|show-case|replay>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .output import read_jsonl
from .replay import replay
from .run import generate


def naira(amount_kobo: int) -> str:
    return f"N{amount_kobo / 100:,.0f}"


def cmd_generate(args) -> None:
    cfg = load_config(args.config, seed=args.seed, days=args.days,
                      customers=args.customers, fraud_cases=args.cases)
    out = Path(args.out or f"data/run-{cfg.seed}")
    summary = generate(cfg, out)
    fraud = summary["fraud"]
    print(f"Wrote {out}/")
    print(f"  {summary['accounts']:,} accounts, {summary['transactions']:,} transactions "
          f"over {summary['days']} days across {summary['institutions']} institutions")
    print(f"  {fraud['cases']} fraud cases, N{fraud['stolen_naira']:,} stolen, "
          f"{fraud['mule_accounts']} mule accounts in {fraud['rings']} rings")


def cmd_summary(args) -> None:
    print((Path(args.run) / "summary.json").read_text())


def cmd_show_case(args) -> None:
    run = Path(args.run)
    truth = json.loads((run / "ground_truth.json").read_text())
    cases = {c["case_id"]: c for c in truth["cases"]}
    if args.case_id is None:
        for c in truth["cases"]:
            print(f"{c['case_id']}  {c['scenario']:<13} {c['method']:<19} {c['channel']:<7}"
                  f"{naira(c['stolen_kobo']):>14}  {len(c['mule_accounts'])} mules")
        return
    if args.case_id not in cases:
        sys.exit(f"No case {args.case_id} in {run}. Run show-case without an ID to list them.")

    case = cases[args.case_id]
    wanted = {t["transaction_id"]: t for t in case["tainted_transactions"]}
    txs = {e["transaction_id"]: e for e in read_jsonl(run / "events.jsonl")
           if e["type"] == "transaction" and e["transaction_id"] in wanted}
    cash = {p["transaction_id"]: p["type"] for p in case["cash_out_points"]}

    print(f"{case['case_id']}: {case['scenario']} via {case['channel']} ({case['method']}), ring {case['ring_id']}")
    print(f"Victim {case['victim_bank']}/{case['victim_account']} lost {naira(case['stolen_kobo'])}; "
          f"reported {case['reported_at']}")
    print()
    for tainted in case["tainted_transactions"]:
        tx = txs[tainted["transaction_id"]]
        if tx["receiver_account"]:
            to = f"{tx['receiver_bank']}/{tx['receiver_account']}"
        else:
            to = "cash"
        note = f"  [{cash[tx['transaction_id']]}]" if tx["transaction_id"] in cash else ""
        mixed = "" if tx["amount_kobo"] == tainted["tainted_kobo"] else f" of {naira(tx['amount_kobo'])} sent"
        print(f"  hop {tainted['hop']}  {tx['timestamp'][5:19]}  "
              f"{tx['sender_bank']}/{tx['sender_account']} -> {to:<16} "
              f"{naira(tainted['tainted_kobo']):>12}{mixed}  {tx['channel']}{note}")
    print()
    cashed = sum(p["tainted_kobo"] for p in case["cash_out_points"])
    print(f"Cashed out {naira(cashed)}; left in mule accounts {naira(case['stolen_kobo'] - cashed)}")


def cmd_replay(args) -> None:
    replay(Path(args.run), speed=args.speed, bank=args.bank, limit=args.limit)


def main() -> None:
    parser = argparse.ArgumentParser(prog="simulator", description="TrailGuard synthetic data simulator")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="generate a run")
    g.add_argument("--seed", type=int)
    g.add_argument("--out", help="output folder (default data/run-<seed>)")
    g.add_argument("--config", type=Path, help="TOML file overriding the defaults")
    g.add_argument("--days", type=int)
    g.add_argument("--customers", type=int)
    g.add_argument("--cases", type=int, help="number of fraud cases")
    g.set_defaults(fn=cmd_generate)

    s = sub.add_parser("summary", help="print a run's summary")
    s.add_argument("run")
    s.set_defaults(fn=cmd_summary)

    c = sub.add_parser("show-case", help="list fraud cases, or print one case's trail")
    c.add_argument("run")
    c.add_argument("case_id", nargs="?")
    c.set_defaults(fn=cmd_show_case)

    r = sub.add_parser("replay", help="stream a run's events to stdout")
    r.add_argument("run")
    r.add_argument("--speed", type=float, default=0, help="simulated seconds per real second (0 = no pause)")
    r.add_argument("--bank", help="only this institution's view, e.g. BNK01")
    r.add_argument("--limit", type=int)
    r.set_defaults(fn=cmd_replay)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
