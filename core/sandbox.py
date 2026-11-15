"""Fill a fresh TrailGuard with synthetic activity, for the sandbox."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from connector.__main__ import push
from scorer import model as model_store
from scorer.train import train
from simulator.config import load_config
from simulator.run import generate

from . import db

WAT = timezone(timedelta(hours=1))
SIZE = {"days": 3, "customers": 1500, "fraud_cases": 20}

CONNECTOR_CONFIG = """# Sandbox connector: BNK01 in shadow mode, reading a sample feed.
mode = "shadow"

[bank]
id = "BNK01"

[trailguard]
url = "http://api:8000"

[source]
type = "file"
path = "/data/bnk01-feed.jsonl"

[holds]
deliver = "file"
path = "/data/bnk01-hold-requests.jsonl"
poll_seconds = 5

[local]
listen = "0.0.0.0:8088"
state = "/data/bnk01-connector-state.db"
"""


def seed(url: str, data: Path) -> None:
    data = Path(data)
    salt = db.salt()
    keys = {bank: key for key, bank in db.api_keys().items()}

    if model_store.current() is None:
        print("Training the mule scorer on synthetic runs...")
        runs = []
        for seed_number in (901, 902, 903, 904):
            runs.append(data / f"train-{seed_number}")
            generate(load_config(seed=seed_number, **SIZE), runs[-1])
        train(runs[:3], runs[3]).save(model_store.model_path())

    # Three days of activity ending now, so recent holds are still open.
    config = load_config(seed=int(os.environ.get("SANDBOX_SEED", "42")), **SIZE)
    today = datetime.now(WAT).replace(hour=0, minute=0, second=0, microsecond=0)
    config.start = today - timedelta(days=config.days - 1)
    run = data / "sandbox-run"
    summary = generate(config, run)
    cutoff = datetime.now(WAT).isoformat()

    # Only what has "happened" by now is loaded.
    for path in [run / "events.jsonl", *sorted((run / "banks").glob("*/events.jsonl"))]:
        lines = [line for line in path.read_text().splitlines()
                 if (json.loads(line).get("timestamp") or json.loads(line).get("reported_at")
                     or json.loads(line)["opened_at"]) <= cutoff]
        path.write_text("".join(line + "\n" for line in lines))

    with db.connect() as conn:
        db.reset(conn)
    banks = sorted(p.name for p in (run / "banks").iterdir() if p.is_dir())
    print(f"Loading {summary['accounts']:,} accounts across {len(banks)} institutions...")
    totals = push(run, banks, url, keys, salt, batch=2000, slice_minutes=60)
    for bank, counts in totals.items():
        print(f"  {bank}: {counts.get('transactions', 0):,} transactions, {counts.get('cases_opened', 0)} cases, "
              f"{counts.get('alerts_raised', 0)} alerts")

    # A sample raw feed and config for the example bank connector.
    from connector.__main__ import bank_events
    (data / "bnk01-feed.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in bank_events(run, "BNK01")))
    (data / "connector.toml").write_text(CONNECTOR_CONFIG)
    print("Sandbox ready. API docs: http://localhost:8010/docs  (API key: sandbox-bnk01)")
