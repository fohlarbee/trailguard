"""Connector command line.

    python -m connector run connector.toml          the service a bank installs
    python -m connector push data/run-7 --all       replay a simulator run as its institutions
    python -m connector ack-holds data/run-7        answer hold requests as those institutions
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx

from .hashing import hash_event

SNAPSHOT_FIELDS = ("type", "account_number", "bank_id", "bvn", "phone", "device_id",
                   "kyc_tier", "account_type", "opened_at")


def bank_events(run: Path, bank: str) -> list[dict]:
    """Everything one institution would send: its existing accounts, then its event stream."""
    from simulator.output import read_jsonl
    stream = read_jsonl(run / "banks" / bank / "events.jsonl")
    opened_in_run = {e["account_number"] for e in stream if e["type"] == "account_opened"}
    snapshot = [
        {field: account[field] for field in SNAPSHOT_FIELDS}
        for account in read_jsonl(run / "accounts.jsonl")
        if account["bank_id"] == bank and account["account_number"] not in opened_in_run
    ]
    return snapshot + stream


def submissions(run: Path, banks: list[str], slice_minutes: int = 60):
    """Yield (bank, events) in the order live institutions would send them.

    Every institution first sends its existing accounts. Then time moves forward in
    slices, and in each slice every institution sends what happened to it. Events
    therefore reach TrailGuard in roughly the order they occurred, across banks.
    """
    from simulator.output import read_jsonl
    from simulator.schema import event_time

    streams = {}
    for bank in banks:
        events = bank_events(run, bank)
        stream = read_jsonl(run / "banks" / bank / "events.jsonl")
        yield bank, events[: len(events) - len(stream)]      # the account snapshot
        streams[bank] = stream

    def slot(event: dict) -> int:
        return int(datetime.fromisoformat(event_time(event)).timestamp() // (slice_minutes * 60))

    slots = sorted({slot(e) for stream in streams.values() for e in stream})
    position = {bank: 0 for bank in banks}
    for current in slots:
        for bank in banks:
            stream, start = streams[bank], position[bank]
            end = start
            while end < len(stream) and slot(stream[end]) <= current:
                end += 1
            if end > start:
                position[bank] = end
                yield bank, stream[start:end]


def push(run: Path, banks: list[str], url: str, keys: dict[str, str], salt: str,
         batch: int, slice_minutes: int) -> dict[str, dict]:
    from .bank import SimulatedBank

    totals: dict[str, dict] = {bank: {"holds_answered": 0, "held_kobo": 0} for bank in banks}
    simulated = {bank: SimulatedBank(run, bank, salt) for bank in banks}
    with httpx.Client(base_url=url, timeout=120) as client:
        for bank, events in submissions(run, banks, slice_minutes):
            for start in range(0, len(events), batch):
                chunk = [hash_event(e, salt) for e in events[start:start + batch]]
                response = client.post("/v1/events", json={"events": chunk}, headers={"X-API-Key": keys[bank]})
                if response.status_code != 200:
                    sys.exit(f"{bank}: ingest rejected a batch ({response.status_code}): {response.text}")
                counts = response.json()
                for name, value in counts.items():
                    totals[bank][name] = totals[bank].get(name, 0) + value
                # A flag on one of this bank's accounts comes with a hold request.
                # A live bank answers within seconds, so the simulated one does too.
                if counts["alerts_raised"]:
                    answered = simulated[bank].answer_holds(client, keys[bank])
                    totals[bank]["holds_answered"] += answered["answered"]
                    totals[bank]["held_kobo"] += answered["held_kobo"]
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(prog="connector")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("push", help="send a simulated institution's events to TrailGuard")
    p.add_argument("run", type=Path)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--bank", help="institution ID, e.g. BNK01")
    group.add_argument("--all", action="store_true", help="every institution in the run")
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--batch", type=int, default=2000)
    p.add_argument("--slice-minutes", type=int, default=60,
                   help="simulated minutes each institution sends at a time")
    r = sub.add_parser("run", help="run the connector service from a config file")
    r.add_argument("config", type=Path)
    a = sub.add_parser("ack-holds", help="answer pending hold requests as the simulated institutions would")
    a.add_argument("run", type=Path)
    a.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    if args.command == "run":
        import uvicorn

        from .service import Connector, load_config, local_app
        try:
            config = load_config(args.config, dict(os.environ))
        except (ValueError, KeyError, OSError) as error:
            sys.exit(f"Cannot start: {error}")
        host, port = config.listen.rsplit(":", 1)
        print(f"TrailGuard connector for {config.bank_id} ({config.mode} mode), "
              f"sending to {config.url}, listening on {config.listen}")
        uvicorn.run(local_app(Connector(config)), host=host, port=int(port), log_level="warning")
        return

    salt = os.environ.get("TRAILGUARD_SALT")
    keys = dict(pair.split(":", 1) for pair in os.environ.get("TRAILGUARD_API_KEYS", "").split(",") if pair)
    if not salt or not keys:
        sys.exit("Set TRAILGUARD_SALT and TRAILGUARD_API_KEYS (see .env.example).")

    if args.command == "ack-holds":
        from .bank import SimulatedBank
        with httpx.Client(base_url=args.url, timeout=120) as client:
            for bank in sorted(p.name for p in (args.run / "banks").iterdir() if p.is_dir()):
                totals = SimulatedBank(args.run, bank, salt).answer_holds(client, keys[bank])
                print(f"{bank}: answered {totals['answered']} holds; {totals['active']} holding "
                      f"N{totals['held_kobo'] / 100:,.0f}, {totals['declined']} with nothing left")
        return

    banks = sorted(p.name for p in (args.run / "banks").iterdir() if p.is_dir()) if args.all else [args.bank]
    for bank in banks:
        if bank not in keys:
            sys.exit(f"No API key for {bank} in TRAILGUARD_API_KEYS.")
    for bank, totals in push(args.run, banks, args.url, keys, salt, args.batch, args.slice_minutes).items():
        print(f"{bank}: sent {totals['received']:,}; stored {totals['accounts']:,} accounts, "
              f"{totals['transactions']:,} transactions; opened {totals['cases_opened']} cases; "
              f"raised {totals['alerts_raised']} alerts; held N{totals['held_kobo'] / 100:,.0f} "
              f"on {totals['holds_answered']} holds")


if __name__ == "__main__":
    main()
