"""The mule scorer: features, training, and live scoring through ingest."""

from datetime import datetime

import pytest

from connector.hashing import hash_value
from core import audit, scoring
from scorer.data import build_rows, load_run
from scorer.evaluate import evaluate, first_flags
from scorer.features import FEATURES, FeatureStream

from .conftest import SALT


def test_features_never_use_the_future(trained):
    accounts, transactions, _ = load_run(trained["unseen"])
    full, cut = FeatureStream(accounts), FeatureStream(accounts)
    whole = [row for tx in transactions for row in full.apply(tx)]
    prefix = [row for tx in transactions[:2000] for row in cut.apply(tx)]
    assert prefix == whole[:len(prefix)] or all(
        a == b or (a != a and b != b) for (_, x), (_, y) in zip(prefix, whole) for a, b in zip(x, y))
    assert all(len(row) == len(FEATURES) for _, row in prefix)


def test_scorer_separates_mules_from_lookalikes(trained):
    scorer = trained["scorer"]
    result = evaluate(trained["unseen"], lambda rows: scorer.score(rows.X), scorer.high)
    assert result["accounts"]["mule_recall"] >= 0.8
    assert result["accounts"]["false_flag_rate"] <= 0.01
    flagged_lookalikes = {k: v for k, v in result["accounts"]["legitimate_flagged_by_kind"].items()
                          if k != "ordinary_customer"}
    assert sum(flagged_lookalikes.values()) <= 1
    assert result["timing"]["first_mule_flagged_before_it_moved_money"] >= result["timing"]["cases"] * 0.6


def test_live_alerts_match_offline_scoring(trained, live):
    """Scoring inside ingest must agree with scoring the run file directly."""
    scorer = trained["scorer"]
    rows = build_rows(live["run"])
    offline = first_flags(rows, scorer.score(rows.X), scorer.high)
    expected = {hash_value(SALT, "account", account): when for account, when in offline.items()}

    alerts = {a["account"]: a["first_flagged_at"] for a in live["conn"].execute("SELECT * FROM alerts")}
    assert alerts.keys() == expected.keys()
    assert all(alerts[account] == expected[account] for account in expected)
    assert len(alerts) > 0


def test_alerts_are_recorded_and_explained(live):
    conn = live["conn"]
    conn.commit()
    alerts = live["client"].get("/v1/alerts", headers=live["key"]).json()
    assert alerts and all(a["reasons"] for a in alerts)
    flagged = conn.execute("SELECT count(*) AS n FROM audit_log WHERE kind = 'account_flagged'").fetchone()["n"]
    assert flagged == len(alerts)
    assert audit.verify(conn)["valid"]


def test_account_score_on_demand(live):
    conn, client, key = live["conn"], live["client"], live["key"]
    alert = conn.execute("SELECT * FROM alerts ORDER BY first_flagged_at LIMIT 1").fetchone()

    at_flag = scoring.score_account(conn, alert["account"], as_of=alert["first_flagged_at"])
    assert at_flag["tier"] == "high" and at_flag["score"] == pytest.approx(alert["score"], abs=1e-3)
    assert at_flag["reasons"] == alert["reasons"]

    business = conn.execute("SELECT account FROM accounts WHERE account_type = 'business' LIMIT 1").fetchone()
    assert client.get(f"/v1/accounts/{business['account']}/score", headers=key).json()["tier"] == "unscored"
    assert client.get("/v1/accounts/unknown/score", headers=key).status_code == 404

    before = scoring.score_account(conn, alert["account"], as_of=datetime.fromisoformat("2000-01-01T00:00:00+00:00"))
    assert before["tier"] == "unscored"
