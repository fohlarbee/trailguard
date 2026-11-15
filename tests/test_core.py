"""Ingest, tracer and audit log, against a real Postgres.

Needs the dev database running (docker compose up -d). Skipped if it is not.
"""

import json

import pytest
from fastapi.testclient import TestClient

from connector.__main__ import bank_events
from connector.hashing import hash_event, hash_value
from core import audit, db, tracer
from core.evaluate import evaluate
from simulator.config import load_config
from simulator.output import read_jsonl
from simulator.run import generate

from .conftest import SALT, TEST_DB, require_database


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    """A small simulator run pushed through the API by every institution."""
    require_database()

    run = tmp_path_factory.mktemp("run")
    generate(load_config(seed=11, days=3, customers=800, fraud_cases=14), run)
    banks = [i["id"] for i in json.loads((run / "institutions.json").read_text())]

    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", db.DEV_DATABASE_URL.rsplit("/", 1)[0] + "/" + TEST_DB)
    patch.setenv("TRAILGUARD_SALT", SALT)
    patch.setenv("TRAILGUARD_MODEL", str(run / "no-model.joblib"))    # scoring is tested separately
    patch.setenv("TRAILGUARD_API_KEYS", ",".join(f"{b}:key-{b}" for b in banks))

    from core.api import app
    client = TestClient(app)
    with db.connect() as conn:
        db.reset(conn)
        for bank in banks:
            events = [hash_event(e, SALT) for e in bank_events(run, bank)]
            response = client.post("/v1/events", json={"events": events}, headers={"X-API-Key": f"key-{bank}"})
            assert response.status_code == 200, response.text
        yield {"run": run, "banks": banks, "client": client, "conn": conn,
               "truth": json.loads((run / "ground_truth.json").read_text())}
    patch.undo()


def count(conn, table):
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_everything_is_stored_once(loaded):
    conn, run = loaded["conn"], loaded["run"]
    events = read_jsonl(run / "events.jsonl")
    assert count(conn, "transactions") == sum(e["type"] == "transaction" for e in events)
    assert count(conn, "accounts") == len(read_jsonl(run / "accounts.jsonl"))
    assert count(conn, "cases") == len(loaded["truth"]["cases"])


def test_no_raw_identifier_reaches_the_database(loaded):
    conn = loaded["conn"]
    account = read_jsonl(loaded["run"] / "accounts.jsonl")[0]
    stored = conn.execute("SELECT * FROM accounts WHERE account = %s",
                          (hash_value(SALT, "account", account["account_number"]),)).fetchone()
    assert stored["bvn"] == hash_value(SALT, "bvn", account["bvn"])
    for column in ("account", "bvn", "phone", "device"):
        raw = conn.execute(f"SELECT count(*) AS n FROM accounts WHERE {column} !~ '^[0-9a-f]{{32}}$'").fetchone()
        assert raw["n"] == 0
    raw = conn.execute("""SELECT count(*) AS n FROM transactions
                          WHERE sender_account !~ '^[0-9a-f]{32}$'
                             OR receiver_account !~ '^[0-9a-f]{32}$'""").fetchone()
    assert raw["n"] == 0


def test_resending_a_batch_changes_nothing(loaded):
    conn, client, bank = loaded["conn"], loaded["client"], loaded["banks"][0]
    before = (count(conn, "transactions"), count(conn, "accounts"), count(conn, "cases"), count(conn, "audit_log"))
    events = [hash_event(e, SALT) for e in bank_events(loaded["run"], bank)]
    result = client.post("/v1/events", json={"events": events}, headers={"X-API-Key": f"key-{bank}"}).json()
    assert result["duplicates"] == result["received"] == len(events)
    assert before == (count(conn, "transactions"), count(conn, "accounts"), count(conn, "cases"),
                      count(conn, "audit_log"))


def test_an_institution_can_only_send_its_own_events(loaded):
    client, (first, second) = loaded["client"], loaded["banks"][:2]
    foreign = [hash_event(e, SALT) for e in bank_events(loaded["run"], second)
               if e["type"] == "transaction" and first not in (e["sender_bank"], e["receiver_bank"])][:1]
    assert foreign
    response = client.post("/v1/events", json={"events": foreign}, headers={"X-API-Key": f"key-{first}"})
    assert response.status_code == 422
    assert client.post("/v1/events", json={"events": foreign}, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/v1/events", json={"events": [{"type": "transaction"}]},
                       headers={"X-API-Key": f"key-{first}"}).status_code == 422


def test_tracer_finds_the_trails(loaded):
    result = evaluate(loaded["conn"], loaded["run"], SALT)
    overall = result["overall"]
    assert overall["transaction_recall"] >= 0.98
    # A reused ring can have one case's money attributed to another, so per-case
    # precision is lower. Flagging a transaction that is not fraud at all must be rare.
    assert overall["transaction_precision"] >= 0.85
    assert overall["fraud_precision"] >= 0.98
    assert overall["mule_recall"] >= 0.98
    assert overall["mule_precision"] >= 0.95
    # Where no money is mixed in, the trail should match the ground truth exactly.
    for scenario in ("fast_cashout", "layering"):
        scores = result["by_scenario"][scenario]
        assert scores["exact_trails"] == scores["cases"]


def test_every_stolen_kobo_is_accounted_for(loaded):
    conn = loaded["conn"]
    for case in conn.execute("SELECT case_id, reported_at FROM cases").fetchall():
        for as_of in (None, case["reported_at"]):
            trail = tracer.trace(conn, case["case_id"], as_of=as_of)
            assert trail.stolen_kobo > 0
            assert trail.exited_kobo + trail.holdable_kobo == trail.stolen_kobo


def test_tracing_is_repeatable_and_earlier_views_see_less(loaded):
    conn = loaded["conn"]
    case = conn.execute("SELECT case_id, reported_at FROM cases ORDER BY case_id LIMIT 1").fetchone()
    full = tracer.trace(conn, case["case_id"])
    assert full.to_dict() == tracer.trace(conn, case["case_id"]).to_dict()
    early = tracer.trace(conn, case["case_id"], as_of=case["reported_at"])
    assert {h.transaction_id for h in early.hops} <= {h.transaction_id for h in full.hops}
    assert all(h.ts <= case["reported_at"] for h in early.hops)


def test_trace_endpoint_stores_the_trail(loaded):
    client, key = loaded["client"], {"X-API-Key": f"key-{loaded['banks'][0]}"}
    case_id = client.get("/v1/cases", headers=key).json()[0]["case_id"]
    traced = client.post(f"/v1/cases/{case_id}/trace", headers=key).json()
    stored = client.get(f"/v1/cases/{case_id}", headers=key).json()
    assert len(stored["hops"]) == len(traced["hops"]) > 0
    assert stored["stolen_kobo"] == traced["stolen_kobo"]
    assert client.post("/v1/cases/nope/trace", headers=key).status_code == 404


def test_audit_log_detects_tampering(loaded):
    conn = loaded["conn"]
    conn.commit()
    state = audit.verify(conn)
    assert state["valid"] and state["entries"] >= len(loaded["truth"]["cases"])

    target = conn.execute("SELECT id, body FROM audit_log ORDER BY id LIMIT 1 OFFSET 2").fetchone()
    conn.execute("UPDATE audit_log SET body = replace(body, 'case_opened', 'case_closed') WHERE id = %s",
                 (target["id"],))
    broken = audit.verify(conn)
    assert not broken["valid"] and broken["broken_at"] == target["id"]
    conn.rollback()
    assert audit.verify(conn)["valid"]
