import filecmp
import json
from datetime import datetime

import pytest

from simulator.config import load_config
from simulator.output import read_jsonl
from simulator.run import generate
from simulator.schema import event_banks, event_time


def small_config(seed=11):
    return load_config(seed=seed, days=3, customers=800, fraud_cases=14)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("run")
    generate(small_config(), out)
    return {
        "path": out,
        "accounts": read_jsonl(out / "accounts.jsonl"),
        "events": read_jsonl(out / "events.jsonl"),
        "truth": json.loads((out / "ground_truth.json").read_text()),
        "summary": json.loads((out / "summary.json").read_text()),
    }


def files(root):
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


def test_same_seed_gives_identical_output(run, tmp_path):
    generate(small_config(), tmp_path)
    assert files(tmp_path) == files(run["path"])
    for name in files(tmp_path):
        assert filecmp.cmp(tmp_path / name, run["path"] / name, shallow=False), name


def test_different_seed_gives_different_output(run, tmp_path):
    generate(small_config(seed=12), tmp_path)
    assert not filecmp.cmp(tmp_path / "events.jsonl", run["path"] / "events.jsonl", shallow=False)


def test_events_are_in_time_order(run):
    times = [datetime.fromisoformat(event_time(e)) for e in run["events"]]
    assert times == sorted(times)


def test_no_balance_goes_negative_and_accounts_exist_before_use(run):
    start = datetime.fromisoformat(run["summary"]["start"])
    opening = {a["account_number"]: a for a in run["accounts"]}
    balances = {n: a["opening_balance_kobo"] for n, a in opening.items()
                if datetime.fromisoformat(a["opened_at"]) < start}

    for event in run["events"]:
        if event["type"] == "account_opened":
            number = event["account_number"]
            assert number not in balances
            balances[number] = opening[number]["opening_balance_kobo"]
        elif event["type"] == "transaction":
            assert event["amount_kobo"] > 0
            assert event["sender_account"] in balances
            balances[event["sender_account"]] -= event["amount_kobo"]
            assert balances[event["sender_account"]] >= 0
            if event["receiver_account"]:
                assert event["receiver_account"] in balances
                balances[event["receiver_account"]] += event["amount_kobo"]
    assert set(balances) == set(opening)


def test_every_scenario_is_exercised(run):
    assert len(run["truth"]["cases"]) >= 12
    scenarios = {c["scenario"] for c in run["truth"]["cases"]}
    assert scenarios == {"fan_out", "layering", "fast_cashout", "commingled", "insider"}


def test_ground_truth_is_consistent(run):
    txs = {e["transaction_id"]: e for e in run["events"] if e["type"] == "transaction"}
    reports = {e["report_id"]: e for e in run["events"] if e["type"] == "fraud_report"}
    known_mules = {m["account"] for m in run["truth"]["mule_accounts"]}

    for case in run["truth"]["cases"]:
        tainted = {t["transaction_id"]: t for t in case["tainted_transactions"]}

        # The theft transactions are the victim's, and add up to the amount stolen.
        thefts = [txs[i] for i in case["theft_transaction_ids"]]
        assert all(tx["sender_account"] == case["victim_account"] for tx in thefts)
        assert sum(tx["amount_kobo"] for tx in thefts) == case["stolen_kobo"]

        # No transaction carries more stolen money than it moved.
        for tx_id, entry in tainted.items():
            assert 0 < entry["tainted_kobo"] <= txs[tx_id]["amount_kobo"]

        # Every stolen kobo is accounted for: cashed out, or still in a mule account.
        cashed = sum(p["tainted_kobo"] for p in case["cash_out_points"])
        residual = sum(r["tainted_kobo"] for r in case["residual"])
        assert cashed + residual == case["stolen_kobo"]
        assert all(p["transaction_id"] in tainted for p in case["cash_out_points"])

        # After the theft, stolen money only moves out of accounts labelled as mules.
        mules = {m["account"] for m in case["mule_accounts"]}
        assert mules <= known_mules
        for tx_id, entry in tainted.items():
            if entry["hop"] > 1:
                assert txs[tx_id]["sender_account"] in mules

        # The victim's bank reports it, after the theft, naming the theft transactions.
        report = reports[case["report_id"]]
        assert report["victim_account"] == case["victim_account"]
        assert report["reporting_bank"] == case["victim_bank"]
        assert report["transaction_ids"] == case["theft_transaction_ids"]
        assert report["reported_at"] > max(tx["timestamp"] for tx in thefts)

    assert len(reports) == len(run["truth"]["cases"])


def test_each_bank_sees_exactly_its_own_events(run):
    institutions = json.loads((run["path"] / "institutions.json").read_text())
    for institution in institutions:
        bank = institution["id"]
        expected = [e for e in run["events"] if bank in event_banks(e)]
        assert read_jsonl(run["path"] / "banks" / bank / "events.jsonl") == expected
        assert expected

    cross_bank = [e for e in run["events"] if e["type"] == "transaction"
                  and e["receiver_bank"] and e["receiver_bank"] != e["sender_bank"]]
    assert cross_bank


def test_lookalikes_are_never_mules_or_ring_members(run):
    truth = run["truth"]
    lookalikes = {item["account"] for item in truth["lookalikes"]}
    kinds = {item["kind"] for item in truth["lookalikes"]}
    assert kinds == {"business_batch_payer", "savings_group_collector", "new_account_large_inflow"}
    assert not lookalikes & {m["account"] for m in truth["mule_accounts"]}
    assert not lookalikes & {a for ring in truth["rings"] for a in ring["accounts"]}


def test_rings_share_devices(run):
    devices = {a["account_number"]: a["device_id"] for a in run["accounts"]}
    for ring in run["truth"]["rings"]:
        shared = [a for a in ring["accounts"] if devices[a] in ring["shared_devices"]]
        assert len(shared) >= 2
