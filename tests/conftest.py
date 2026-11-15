"""Shared fixtures: a trained scorer and a run loaded live through the API."""

import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from connector.__main__ import submissions
from connector.bank import SimulatedBank
from connector.hashing import hash_event
from core import db
from scorer.train import train
from simulator.config import load_config
from simulator.run import generate

SALT = "test-salt"
TEST_DB = "trailguard_test"


def require_database() -> None:
    """Create the test database if needed; skip the test if Postgres is not running."""
    try:
        with psycopg.connect(db.DEV_DATABASE_URL, autocommit=True, connect_timeout=3) as admin:
            if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone():
                admin.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError:
        pytest.skip("Postgres is not running: docker compose up -d")


@pytest.fixture(scope="session")
def trained(tmp_path_factory):
    """A model trained on two small runs, with a third it has never seen."""
    root = tmp_path_factory.mktemp("scorer")
    runs = []
    for seed in (21, 22, 23, 24):
        generate(load_config(seed=seed, days=3, customers=800, fraud_cases=14), root / f"run-{seed}")
        runs.append(root / f"run-{seed}")
    scorer = train(runs[:2], runs[2])
    scorer.save(root / "model.joblib")
    return {"scorer": scorer, "path": root / "model.joblib", "unseen": runs[3]}


@pytest.fixture(scope="module")
def live(trained):
    """The unseen run sent through the API the way live institutions would send it."""
    require_database()
    run = trained["unseen"]
    banks = [i["id"] for i in json.loads((run / "institutions.json").read_text())]
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", db.DEV_DATABASE_URL.rsplit("/", 1)[0] + "/" + TEST_DB)
    patch.setenv("TRAILGUARD_SALT", SALT)
    patch.setenv("TRAILGUARD_API_KEYS", ",".join(f"{b}:key-{b}" for b in banks))
    patch.setenv("TRAILGUARD_MODEL", str(trained["path"]))

    from core.api import app
    client = TestClient(app)
    with db.connect() as conn:
        db.reset(conn)
        simulated = {bank: SimulatedBank(run, bank, SALT) for bank in banks}
        for bank, events in submissions(run, banks, slice_minutes=30):
            response = client.post("/v1/events", json={"events": [hash_event(e, SALT) for e in events]},
                                   headers={"X-API-Key": f"key-{bank}"})
            assert response.status_code == 200, response.text
            if response.json()["alerts_raised"]:      # the bank answers its hold requests at once
                simulated[bank].answer_holds(client, f"key-{bank}")
        yield {"run": run, "banks": banks, "client": client, "conn": conn,
               "key": {"X-API-Key": f"key-{banks[0]}"},
               "truth": json.loads((run / "ground_truth.json").read_text())}
    patch.undo()
