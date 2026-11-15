"""Holds, evidence and the investigator.

The model is replaced by a scripted stand-in, so these tests check everything
around it: the tools, their guardrails, the loop, and what gets recorded.
"""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from agent.investigator import investigate
from agent.tools import TOOLS, Toolbox
from connector.bank import SimulatedBank
from core import audit, holds, tracer

from .conftest import SALT


class ScriptedModel:
    """Stands in for the Claude client. Each step sees the previous tool results."""

    def __init__(self, steps):
        self.steps, self.requests = list(steps), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        # The loop keeps appending to its message list, so keep a copy of what was sent.
        self.requests.append({**request, "messages": list(request["messages"])})
        last = request["messages"][-1]["content"]
        results = [json.loads(r["content"]) for r in last] if isinstance(last, list) else []
        step = self.steps.pop(0)(results)
        if isinstance(step, str):
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=step)])
        if step is None:
            return SimpleNamespace(stop_reason="refusal", content=[])
        return SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="tool_use", id=f"call_{len(self.requests)}_{i}", name=name, input=arguments)
            for i, (name, arguments) in enumerate(step)])


@pytest.fixture(scope="module")
def case(live):
    """A case and a moment at which its stolen money is still sitting in an account."""
    conn = live["conn"]
    for row in conn.execute("SELECT case_id, reported_at FROM cases ORDER BY case_id").fetchall():
        first = conn.execute(
            """SELECT min(t.ts) AS ts FROM transactions t JOIN case_transactions c USING (transaction_id)
               WHERE c.case_id = %s""", (row["case_id"],)).fetchone()["ts"]
        now = first + timedelta(seconds=40)
        trail = tracer.trace(conn, row["case_id"], as_of=now)
        if trail.holdings and not trail.exited_kobo:
            return {"case_id": row["case_id"], "now": now, "account": trail.holdings[0].account,
                    "tainted_kobo": trail.holdings[0].tainted_kobo}
    pytest.fail("no case with money still in an account shortly after the theft")


def test_every_flag_places_a_temporary_hold(live):
    conn = live["conn"]
    alerts = conn.execute("SELECT * FROM alerts").fetchall()
    placed = {h["account"]: h for h in conn.execute("SELECT * FROM holds WHERE source = 'auto_flag'")}
    assert alerts and len(placed) == len(alerts)
    for alert in alerts:
        hold = placed[alert["account"]]
        assert hold["placed_at"] == alert["first_flagged_at"]
        assert hold["expires_at"] - hold["placed_at"] == timedelta(hours=24)
        assert hold["requested_kobo"] > 0 and hold["review"] is None


def test_simulated_banks_answer_holds(live):
    conn, client = live["conn"], live["client"]
    conn.commit()
    answered = conn.execute("SELECT * FROM holds WHERE source = 'auto_flag'").fetchall()
    assert sum(h["held_kobo"] for h in answered) > 0
    for hold in answered:
        # Answered within seconds; later it stays held, or lapses if nobody confirmed it.
        assert hold["acknowledged_at"] - hold["placed_at"] == timedelta(seconds=30)
        assert hold["status"] in ("active", "declined", "expired")
        assert 0 <= hold["held_kobo"] <= hold["requested_kobo"]
        if hold["status"] == "declined":
            assert hold["held_kobo"] == 0

    # A bank cannot answer for another bank's account, or answer twice.
    hold = conn.execute("SELECT * FROM holds LIMIT 1").fetchone()
    other = next(b for b in live["banks"] if b != hold["bank_id"])
    for key in (f"key-{other}", f"key-{hold['bank_id']}"):
        response = client.post(f"/v1/holds/{hold['hold_id']}/ack", json={"held_kobo": 1}, headers={"X-API-Key": key})
        assert response.status_code == 422


def test_agent_works_a_case_end_to_end(live, case):
    conn = live["conn"]
    stranger = conn.execute(
        "SELECT account FROM accounts WHERE account_type = 'personal' AND account <> %s LIMIT 1",
        (case["account"],)).fetchone()["account"]

    def after_trace(results):
        holding = results[1]["accounts_still_holding_stolen_money"][0]
        return [
            ("score_accounts", {"accounts": [holding["account"]]}),
            ("find_ring", {"account": holding["account"]}),
            ("place_hold", {"account": holding["account"], "amount_naira": holding["stolen_naira"],
                            "reason": "Received the reported theft and still holds it."}),
            ("place_hold", {"account": stranger, "amount_naira": 5000, "reason": "Not on the trail."}),
            ("place_hold", {"account": holding["account"], "amount_naira": holding["stolen_naira"] * 3,
                            "reason": "More than the trace shows."}),
        ]

    model = ScriptedModel([
        lambda results: [("get_case", {}), ("trace_funds", {})],
        after_trace,
        lambda results: [("list_holds", {}), ("build_evidence_pack", {})],
        lambda results: [("draft_str", {"narrative": "Funds were moved to a newly opened account."})],
        lambda results: "Stolen funds traced and held. One hold awaits your review.",
    ])
    before = audit.verify(conn)["entries"]
    result = investigate(conn, case["case_id"], now=case["now"], client=model)

    assert result["outcome"] == "completed"
    assert result["summary"].startswith("Stolen funds traced")
    assert [s["tool"] for s in result["steps"]] == [
        "get_case", "trace_funds", "score_accounts", "find_ring", "place_hold", "place_hold", "place_hold",
        "list_holds", "build_evidence_pack", "draft_str"]
    # The two holds outside policy were refused by the tool, not by the model.
    assert [s["failed"] for s in result["steps"]] == [False] * 5 + [True, True] + [False] * 3

    # What the model was sent: the tools, a cached system prompt, and results grouped per turn.
    request = model.requests[2]
    assert request["tools"] == TOOLS and request["model"] == "claude-opus-5-5"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    grouped = request["messages"][-1]["content"]
    assert len(grouped) == 5 and all(r["type"] == "tool_result" for r in grouped)
    assert [bool(r.get("is_error")) for r in grouped] == [False, False, False, True, True]
    # Nothing but hashed identifiers ever reaches the model.
    sent = json.dumps(model.requests[-1]["messages"], default=str)
    raw_numbers = [a["account"] for a in live["truth"]["mule_accounts"]]
    assert not any(number in sent for number in raw_numbers)

    hold = conn.execute("SELECT * FROM holds WHERE case_id = %s", (case["case_id"],)).fetchone()
    assert hold["account"] == case["account"] and hold["status"] in ("requested", "active")
    assert not conn.execute("SELECT 1 FROM holds WHERE account = %s", (stranger,)).fetchone()

    pack = conn.execute("SELECT * FROM evidence_packs WHERE case_id = %s", (case["case_id"],)).fetchone()
    canonical = json.dumps(pack["pack"], sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == pack["hash"]
    assert pack["pack"]["audit_chain"]["valid"] and pack["pack"]["trail"]

    draft = conn.execute("SELECT * FROM str_drafts WHERE case_id = %s", (case["case_id"],)).fetchone()
    assert draft["status"] == "draft" and draft["filed_by"] is None
    assert draft["body"]["amount_stolen_kobo"] == pack["pack"]["case"]["stolen_kobo"] > 0

    run = conn.execute("SELECT * FROM agent_runs WHERE run_id = %s", (result["run_id"],)).fetchone()
    assert run["outcome"] == "completed" and len(run["steps"]) == 10
    logged = conn.execute(
        "SELECT count(*) AS n FROM audit_log WHERE kind = 'agent_tool_call' AND body LIKE %s",
        (f'%"run_id":{result["run_id"]},%',)).fetchone()["n"]
    assert logged == 10
    state = audit.verify(conn)
    assert state["valid"] and state["entries"] > before


def test_agent_cannot_see_past_its_clock(live, case):
    conn = live["conn"]
    result, failed = Toolbox(conn, case["case_id"], case["now"]).call("trace_funds", {})
    early = json.loads(result)
    assert not failed and early["left_the_system_naira"] == 0
    assert all(m["at"] <= case["now"].isoformat() for m in early["movements"])
    later = json.loads(Toolbox(conn, case["case_id"], case["now"] + timedelta(days=2)).call("trace_funds", {})[0])
    assert len(later["movements"]) > len(early["movements"])
    conn.commit()


def test_a_refusal_ends_the_run_cleanly(live, case):
    result = investigate(live["conn"], case["case_id"], now=case["now"], client=ScriptedModel([lambda r: None]))
    assert result["outcome"] == "refused" and result["steps"] == []


def test_holds_lapse_unless_a_human_confirms(live, case):
    conn, client = live["conn"], live["client"]
    accounts = [r["account"] for r in conn.execute(
        """SELECT account FROM accounts a WHERE account_type = 'personal'
           AND NOT EXISTS (SELECT 1 FROM holds h WHERE h.account = a.account) LIMIT 3""")]
    now = case["now"]
    first, second, third = (
        holds.place(conn, account=a, amount_kobo=50_000, reason="test", source="analyst", now=now)
        for a in accounts)
    assert holds.place(conn, account=accounts[0], amount_kobo=80_000, reason="again", source="analyst",
                       now=now)["already_held"]

    # A bank gets ten minutes before it can be chased, and again after each reminder.
    with pytest.raises(holds.HoldError):
        holds.remind(conn, first["hold_id"], now + timedelta(minutes=1))
    assert holds.remind(conn, first["hold_id"], now + timedelta(minutes=11))["reminders"] == 1
    with pytest.raises(holds.HoldError):
        holds.remind(conn, first["hold_id"], now + timedelta(minutes=15))

    holds.review(conn, second["hold_id"], "confirm", "Ada Obi", now + timedelta(hours=1))
    holds.review(conn, third["hold_id"], "release", "Ada Obi", now + timedelta(hours=1))
    with pytest.raises(holds.HoldError):
        holds.review(conn, third["hold_id"], "confirm", "Ada Obi", now + timedelta(hours=2))
    with pytest.raises(holds.HoldError):
        holds.review(conn, first["hold_id"], "confirm", " ", now)

    holds.expire(conn, now + timedelta(hours=25))
    status = {h["hold_id"]: h["status"] for h in conn.execute(
        "SELECT hold_id, status FROM holds WHERE hold_id = ANY(%s)",
        ([first["hold_id"], second["hold_id"], third["hold_id"]],))}
    assert status == {first["hold_id"]: "expired", second["hold_id"]: "requested", third["hold_id"]: "released"}
    conn.commit()

    # A reduction can be recommended, never below traced stolen money, and only a human applies it.
    held = conn.execute("SELECT * FROM holds WHERE account = %s", (case["account"],)).fetchone()
    conn.execute("UPDATE holds SET status = 'active', held_kobo = requested_kobo + 500000 WHERE hold_id = %s",
                 (held["hold_id"],))
    floor = holds.traced_in_account(conn, case["account"])
    assert floor > 0
    with pytest.raises(holds.HoldError):
        holds.recommend_reduction(conn, held["hold_id"], floor - 1, "too low", now)
    holds.recommend_reduction(conn, held["hold_id"], floor, "Only this much is traced as stolen.", now)
    conn.commit()
    path = f"/v1/holds/{held['hold_id']}/reduce"
    theirs = {"X-API-Key": f"key-{held['bank_id']}"}
    stranger = {"X-API-Key": f"key-{next(b for b in live['banks'] if b != held['bank_id'])}"}
    at = now.isoformat()
    assert client.post(path, json={"reviewer": "Ada Obi", "at": at}, headers=stranger).status_code == 422
    reduced = client.post(path, json={"reviewer": "Ada Obi", "at": at}, headers=theirs).json()
    assert reduced["held_kobo"] == floor and reduced["recommended_kobo"] is None
    assert client.post(path, json={"reviewer": "Ada Obi", "at": at}, headers=theirs).status_code == 422

    # The dashboard's view of the case agrees with the trace and shows its holds.
    detail = client.get(f"/v1/cases/{case['case_id']}", headers=live["key"]).json()
    assert detail["stolen_kobo"] == detail["exited_kobo"] + detail["in_accounts_kobo"] > 0
    assert detail["hops"] and detail["accounts"] and detail["runs"][0]["outcome"] in ("completed", "refused")
    assert any(h["account"] == case["account"] for h in detail["holds"])
    listed = {c["case_id"]: c for c in client.get("/v1/cases", headers=live["key"]).json()}
    assert listed[case["case_id"]]["stolen_kobo"] == detail["stolen_kobo"]
    assert client.get("/v1/me", headers=live["key"]).json()["bank"] == live["banks"][0]

    # Only the account's own bank may review, through the API.
    bank = conn.execute("SELECT bank_id FROM holds WHERE hold_id = %s", (second["hold_id"],)).fetchone()["bank_id"]
    other = next(b for b in live["banks"] if b != bank)
    body = {"decision": "release", "reviewer": "Ada Obi"}
    assert client.post(f"/v1/holds/{second['hold_id']}/review", json=body,
                       headers={"X-API-Key": f"key-{other}"}).status_code == 422
    released = client.post(f"/v1/holds/{second['hold_id']}/review", json=body, headers={"X-API-Key": f"key-{bank}"})
    assert released.status_code == 200 and released.json()["status"] == "released"
    assert audit.verify(conn)["valid"]
