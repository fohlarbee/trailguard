"""The installable connector, run against the real API."""

import json

import pytest
from fastapi.testclient import TestClient

from connector.__main__ import bank_events
from connector.hashing import hash_value
from connector.service import Config, Connector, load_config, local_app
from core import db, holds
from simulator.config import load_config as sim_config
from simulator.output import read_jsonl
from simulator.run import generate

from .conftest import SALT, TEST_DB, require_database

BANK = "BNK01"


@pytest.fixture()
def setup(tmp_path):
    require_database()
    run = tmp_path / "run"
    generate(sim_config(seed=31, days=1, customers=300, fraud_cases=3), run)
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", db.DEV_DATABASE_URL.rsplit("/", 1)[0] + "/" + TEST_DB)
    patch.setenv("TRAILGUARD_API_KEYS", f"{BANK}:key-{BANK},BNK02:key-BNK02")
    patch.setenv("TRAILGUARD_MODEL", str(tmp_path / "none.joblib"))
    patch.delenv("TRAILGUARD_CLOCK", raising=False)

    from core.api import app
    events = bank_events(run, BANK)
    source = tmp_path / "events.jsonl"
    source.write_text("".join(json.dumps(e) + "\n" for e in events[:400]))
    config = Config(bank_id=BANK, url="http://testserver", api_key=f"key-{BANK}", salt=SALT,
                    source_path=source, holds_path=tmp_path / "holds.jsonl", batch=150,
                    state=tmp_path / "state.db")
    with db.connect() as conn:
        db.reset(conn)
        yield {"conn": conn, "config": config, "events": events, "source": source,
               "api": TestClient(app), "tmp": tmp_path}
    patch.undo()


def test_events_are_hashed_sent_once_and_resumed(setup):
    conn, config = setup["conn"], setup["config"]
    connector = Connector(config, client=setup["api"])
    assert connector.pump_file() == 400
    assert connector.pump_file() == 0                      # nothing new

    # More events arrive, including a line that is still being written.
    with setup["source"].open("a") as out:
        out.write("".join(json.dumps(e) + "\n" for e in setup["events"][400:450]))
        out.write('{"type": "transaction", "transaction_id"')
    restarted = Connector(config, client=setup["api"])     # a restart picks up where it left off
    assert restarted.pump_file() == 50

    conn.commit()
    sent = setup["events"][:450]
    accounts = [e for e in sent if e["type"] == "account_opened"]
    stored = conn.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"]
    assert stored == len(accounts) > 0
    raw = conn.execute("SELECT count(*) AS n FROM accounts WHERE account !~ '^[0-9a-f]{32}$'").fetchone()["n"]
    assert raw == 0


def test_hold_requests_reach_the_bank_with_real_account_numbers(setup):
    conn, config = setup["conn"], setup["config"]
    connector = Connector(config, client=setup["api"])
    connector.pump_file()
    conn.commit()
    account = next(e for e in setup["events"] if e["type"] == "account_opened")
    now = conn.execute("SELECT now() AS t").fetchone()["t"]
    hold = holds.place(conn, account=hash_value(SALT, "account", account["account_number"]),
                       amount_kobo=75_000, reason="Flagged by the mule scorer", source="auto_flag", now=now)
    conn.commit()

    assert connector.poll_holds() == 1
    assert connector.poll_holds() == 0                     # delivered once only
    delivered = read_jsonl(config.holds_path)
    assert delivered == [{**delivered[0], "hold_id": hold["hold_id"],
                          "account_number": account["account_number"], "amount_kobo": 75_000, "shadow": False}]

    # The bank answers through the connector's local endpoint.
    local = TestClient(local_app(connector))
    answered = local.post(f"/holds/{hold['hold_id']}/answer", json={"held_kobo": 60_000})
    assert answered.status_code == 200 and answered.json()["status"] == "active"
    assert local.post(f"/holds/{hold['hold_id']}/answer", json={"held_kobo": 1}).status_code == 422
    health = local.get("/health").json()
    assert health["holds_delivered"] == 1 and health["holds_answered"] == 1


def test_shadow_mode_logs_and_delivers_nothing(setup):
    conn, config = setup["conn"], setup["config"]
    config.mode = "shadow"
    connector = Connector(config, client=setup["api"])
    connector.pump_file()
    conn.commit()
    account = next(e for e in setup["events"] if e["type"] == "account_opened")
    now = conn.execute("SELECT now() AS t").fetchone()["t"]
    holds.place(conn, account=hash_value(SALT, "account", account["account_number"]),
                amount_kobo=10_000, reason="test", source="auto_flag", now=now)
    conn.commit()
    assert connector.poll_holds() == 1
    assert not config.holds_path.exists()
    logged = read_jsonl(config.state.with_suffix(".shadow.jsonl"))
    assert logged[0]["shadow"] is True and logged[0]["account_number"] == account["account_number"]


def test_config_file(tmp_path):
    path = tmp_path / "connector.toml"
    path.write_text(open("connector/connector.example.toml").read())
    env = {"TRAILGUARD_API_KEY": "k", "TRAILGUARD_SALT": "s"}
    config = load_config(path, env)
    assert (config.bank_id, config.mode, config.source, config.api_key) == ("BNK01", "shadow", "file", "k")
    with pytest.raises(ValueError):
        load_config(path, {"TRAILGUARD_API_KEY": "k"})
