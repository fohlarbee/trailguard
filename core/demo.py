"""The live demo: one theft, played out on a fast clock in front of an audience.

A small simulator run is loaded up to a few minutes before the theft. From there
the player sends each institution's events as the clock reaches them, exactly as
the banks' connectors would, and acts as the banks: it answers hold requests at
once, and a bank refuses any payment the account cannot cover once held money is
set aside. That is what makes a hold stop the money in the demo.

The clock runs fast while money moves, and drops to real time while the
investigator works, so the agent is shown at its true speed.

Starting the demo empties the database. Everything in it is synthetic.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from connector.hashing import hash_event, hash_value
from simulator.config import load_config
from simulator.output import read_jsonl
from simulator.run import generate
from simulator.schema import event_banks, event_time

from . import clock, db, holds
from .ingest import store_events

SEED = 8      # a social-engineering theft through a recruited customer's account, across three institutions
SCENARIO = "commingled"
SIZE = {"days": 1, "customers": 1000, "fraud_cases": 1}
FAST = 10.0                                 # simulated seconds per real second while stolen money moves
SKIP = 40.0                                 # and through quiet stretches, so the audience is not kept waiting
QUIET = timedelta(seconds=60)               # a stretch is quiet if nothing in the story happens this soon
LEAD = timedelta(minutes=2)                 # the demo starts this long before the theft
TICK = 0.25                                 # real seconds between looks at the clock
DATA = Path("data/demo")


def _ts(event: dict) -> datetime:
    return datetime.fromisoformat(event_time(event)).astimezone(timezone.utc)


def build_run(seed: int = SEED, data: Path = DATA, scenario: str = SCENARIO) -> Path:
    """The demo's simulator run, generated once and reused."""
    run = Path(data) / f"{scenario}-{seed}"
    if not (run / "ground_truth.json").exists():
        generate(load_config(seed=seed, scenario_weights={scenario: 1.0}, **SIZE), run)
    return run


@dataclass
class Script:
    """What the demo will play, prepared from a run before the clock starts."""
    run: Path
    accounts: list[dict]                     # hashed snapshot of accounts that exist before the run
    events: list[dict]                       # hashed events in time order
    opening: dict[str, int]                  # hashed account -> opening balance
    case: dict                               # the run's fraud case (ground truth, for the narrator)
    theft_at: datetime
    start_at: datetime
    end_at: datetime
    moments: list[datetime]                  # when the story moves: each stolen-money transfer, and the report

    def pace(self, now: datetime) -> float:
        upcoming = [m for m in self.moments if m >= now]
        return FAST if upcoming and upcoming[0] - now <= QUIET else SKIP

    @classmethod
    def load(cls, run: Path, salt: str) -> "Script":
        truth = json.loads((run / "ground_truth.json").read_text())
        case = truth["cases"][0]
        raw = read_jsonl(run / "events.jsonl")
        opened_in_run = {e["account_number"] for e in raw if e["type"] == "account_opened"}
        all_accounts = read_jsonl(run / "accounts.jsonl")
        fields = ("type", "account_number", "bank_id", "bvn", "phone", "device_id",
                  "kyc_tier", "account_type", "opened_at")
        snapshot = [hash_event({f: a[f] for f in fields}, salt)
                    for a in all_accounts if a["account_number"] not in opened_in_run]
        opening = {hash_value(salt, "account", a["account_number"]): a["opening_balance_kobo"] for a in all_accounts}

        by_id = {e["transaction_id"]: e for e in raw if e["type"] == "transaction"}
        tainted = [by_id[t["transaction_id"]] for t in case["tainted_transactions"] if t["transaction_id"] in by_id]
        theft_at = min(_ts(by_id[t]) for t in case["theft_transaction_ids"])
        moments = sorted([_ts(e) for e in tainted] + [datetime.fromisoformat(case["reported_at"])])
        return cls(run=run, accounts=snapshot, events=[hash_event(e, salt) for e in raw], opening=opening,
                   case=case, theft_at=theft_at, start_at=theft_at - LEAD, end_at=moments[-1],
                   moments=moments)


@dataclass
class State:
    phase: str = "idle"                       # idle, preparing, playing, investigating, finished, failed
    error: str | None = None
    started_real: datetime | None = None
    case_id: str | None = None
    agent_outcome: str | None = None
    first_audit_id: int = 0
    blocked: list[dict] = field(default_factory=list)   # payments the banks refused because of a hold
    scenario: dict = field(default_factory=dict)


class Player:
    """Sends events as the clock reaches them, and plays the banks' side of each hold."""

    def __init__(self, conn: psycopg.Connection, script: Script, state: State):
        self.conn, self.script, self.state = conn, script, state
        self.balance: dict[str, int] = {}
        self.position = 0

    # -------------------------------------------------------------- the banks

    def _held(self, now: datetime) -> dict[str, int]:
        rows = self.conn.execute(
            """SELECT account, sum(held_kobo) AS held FROM holds
               WHERE status = 'active' AND placed_at <= %s AND (review IS NOT NULL OR expires_at > %s)
               GROUP BY account""", (now, now)).fetchall()
        return {row["account"]: int(row["held"]) for row in rows}

    def _allowed(self, event: dict, held: dict[str, int]) -> bool:
        """Would the sender's bank let this payment through? Held money cannot be spent."""
        if event["type"] == "account_opened":
            self.balance.setdefault(event["account_number"], self.script.opening.get(event["account_number"], 0))
            return True
        if event["type"] != "transaction":
            return True
        sender, receiver, amount = event["sender_account"], event["receiver_account"], event["amount_kobo"]
        available = self.balance.get(sender, 0) - held.get(sender, 0)
        if available < amount:
            return False
        self.balance[sender] = self.balance.get(sender, 0) - amount
        if receiver is not None:
            self.balance[receiver] = self.balance.get(receiver, 0) + amount
        return True

    def answer_holds(self, now: datetime) -> None:
        """Each bank freezes what it can at once: the request, or the balance if that is less."""
        for hold in holds.listing(self.conn, status="requested"):
            held = max(0, min(hold["requested_kobo"], self.balance.get(hold["account"], 0)))
            holds.acknowledge(self.conn, hold["hold_id"], hold["bank_id"], held, max(now, hold["placed_at"]))
        self.conn.commit()

    # ------------------------------------------------------------- the feeds

    def _send(self, bank: str, events: list[dict]) -> dict:
        return store_events(self.conn, bank, events)

    def preload(self) -> None:
        """Everything before the demo starts, sent in hourly batches like a catch-up."""
        for account in self.script.accounts:
            self.balance[account["account_number"]] = self.script.opening.get(account["account_number"], 0)
        by_bank: dict[str, list[dict]] = {}
        for account in self.script.accounts:
            by_bank.setdefault(account["bank_id"], []).append(account)
        for bank, rows in sorted(by_bank.items()):
            self._send(bank, rows)

        hour = None
        batch: dict[str, list[dict]] = {}
        held: dict[str, int] = {}
        events = self.script.events
        while self.position < len(events) and _ts(events[self.position]) < self.script.start_at:
            event = events[self.position]
            this_hour = _ts(event).replace(minute=0, second=0, microsecond=0)
            if hour is not None and this_hour != hour:
                self._flush(batch, this_hour)
                held = self._held(this_hour)
            hour = this_hour
            if self._allowed(event, held):
                for bank in event_banks(event):
                    batch.setdefault(bank, []).append(event)
            self.position += 1
        self._flush(batch, self.script.start_at)

    def _flush(self, batch: dict[str, list[dict]], now: datetime) -> None:
        for bank, rows in sorted(batch.items()):
            self._send(bank, rows)
        batch.clear()
        self.answer_holds(now)

    def advance(self, now: datetime) -> list[str]:
        """Send every event due by `now`, one at a time. Returns the IDs of cases opened."""
        opened = []
        events = self.script.events
        while self.position < len(events) and _ts(events[self.position]) <= now:
            event = events[self.position]
            self.position += 1
            at = _ts(event)
            held = self._held(at)
            if not self._allowed(event, held):
                # Payments that fail only because money never arrived are not news; held money is.
                if held.get(event["sender_account"]):
                    seq = self.conn.execute("SELECT COALESCE(max(id), 0) AS id FROM audit_log").fetchone()["id"]
                    self.state.blocked.append({"at": at, "seq": seq, "account": event["sender_account"],
                                               "bank": event["sender_bank"], "amount_kobo": event["amount_kobo"]})
                continue
            for bank in event_banks(event):
                counts = self._send(bank, [event])
                if counts["alerts_raised"]:
                    self.answer_holds(at)
                if counts["cases_opened"]:
                    opened.append(f"{event['reporting_bank']}-{event['report_id']}")
        self.answer_holds(now)
        return opened

    @property
    def finished(self) -> bool:
        return self.position >= len(self.script.events)


# ------------------------------------------------------------------ the show

_state = State()
_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None


def state() -> State:
    return _state


def _save_pace(conn: psycopg.Connection, pace: tuple[datetime, datetime, float]) -> None:
    conn.execute(
        """INSERT INTO demo_clock (id, anchor, since, rate) VALUES (1, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET anchor = EXCLUDED.anchor, since = EXCLUDED.since, rate = EXCLUDED.rate""",
        pace)
    conn.commit()


def restore_clock(conn: psycopg.Connection) -> None:
    """After a restart, carry on at the demo's time if a demo was loaded. Runs at real speed."""
    row = conn.execute("SELECT anchor, since, rate FROM demo_clock WHERE id = 1").fetchone()
    if row:
        clock.restore(row["anchor"] + (datetime.now(timezone.utc) - row["since"]) * row["rate"],
                      datetime.now(timezone.utc), 1.0)


def _investigate(case_id: str, finished: threading.Event) -> None:
    from agent.investigator import investigate
    try:
        with db.connect() as conn:
            result = investigate(conn, case_id, now=clock.now(), clock=clock.now)
        _state.agent_outcome = result["outcome"]
    except Exception as error:                     # the show goes on without the agent
        _state.agent_outcome = f"failed: {error}"
    finally:
        finished.set()


def _run(seed: int) -> None:
    try:
        with db.connect() as conn:
            run = build_run(seed)
            script = Script.load(run, db.salt())
            case = script.case
            _state.scenario = {
                "stolen_kobo": case["stolen_kobo"], "victim_bank": case["victim_bank"],
                "method": case["method"], "channel": case["channel"],
                "theft_at": script.theft_at, "banks": sorted({m["bank"] for m in case["mule_accounts"]}),
            }
            db.reset(conn)
            player = Player(conn, script, _state)
            player.preload()
            _state.first_audit_id = conn.execute("SELECT COALESCE(max(id), 0) AS id FROM audit_log").fetchone()["id"]

            _save_pace(conn, clock.set_pace(script.pace(script.start_at), at=script.start_at))
            _state.phase = "playing"
            agent_done: threading.Event | None = None
            while not _stop.is_set():
                now = clock.now()
                if _state.phase == "playing" and clock.rate() != script.pace(now):
                    _save_pace(conn, clock.set_pace(script.pace(now)))
                for case_id in player.advance(now):
                    _state.case_id = case_id
                    if agent_done is None:
                        # The victim has reported: hand the case to the investigator, in real time.
                        _save_pace(conn, clock.set_pace(1.0))
                        _state.phase = "investigating"
                        agent_done = threading.Event()
                        threading.Thread(target=_investigate, args=(case_id, agent_done), daemon=True).start()
                if agent_done is not None and agent_done.is_set() and _state.phase == "investigating":
                    _state.phase = "playing"
                if now >= script.end_at and (agent_done is None or agent_done.is_set()):
                    break
                time.sleep(TICK)

            # Afterwards the clock runs at real speed, so the desk can work the holds.
            _save_pace(conn, clock.set_pace(1.0))
            _state.phase = "finished" if not _stop.is_set() else "idle"
    except Exception as error:
        _state.phase, _state.error = "failed", str(error)
        raise


def start(seed: int = SEED) -> State:
    """Empty the database and start the demo from the beginning."""
    global _thread, _state
    with _lock:
        stop()
        _stop.clear()
        _state = State(phase="preparing", started_real=datetime.now(timezone.utc))
        _thread = threading.Thread(target=_run, args=(seed,), daemon=True, name="trailguard-demo")
        _thread.start()
        return _state


def stop() -> None:
    if _thread and _thread.is_alive():
        _stop.set()
        _thread.join(timeout=30)


# ------------------------------------------------------------------ the feed

TOOL_WORDS = {
    "get_case": "Read the case",
    "trace_funds": "Traced the money across banks",
    "score_accounts": "Checked the risk scores of the accounts on the trail",
    "find_ring": "Looked for accounts linked by device or BVN",
    "place_hold": "Placed a hold",
    "list_holds": "Checked the holds",
    "remind_bank": "Chased a bank that had not answered",
    "recommend_hold_reduction": "Recommended a smaller hold",
    "build_evidence_pack": "Built the evidence pack",
    "draft_str": "Drafted the suspicious transaction report",
}


def _naira(kobo: int) -> str:
    return f"₦{kobo / 100:,.0f}"


def _iso(value) -> str | None:
    return datetime.fromisoformat(str(value)).astimezone(timezone.utc).isoformat() if value else None


def feed(conn: psycopg.Connection, now: datetime) -> list[dict]:
    """What TrailGuard has seen and done since the demo began, newest first.

    Each entry says who acted (the scorer, a bank, the investigator), what happened,
    and an optional detail. The investigator's tool calls are grouped into one entry.
    """
    hold_accounts = {row["hold_id"]: row["account"] for row in conn.execute("SELECT hold_id, account FROM holds")}
    items: list[dict] = []
    investigator: dict | None = None

    def add(order, at, actor, tone, title, detail=None, steps=None):
        items.append({"id": f"e{order[0]}-{order[1]}-{order[2]}", "order": order, "at": _iso(at), "actor": actor,
                      "tone": tone, "title": title, "detail": detail, "steps": steps})
        return items[-1]

    for row in conn.execute("SELECT id, kind, body FROM audit_log WHERE id > %s ORDER BY id",
                            (_state.first_audit_id,)):
        p = json.loads(row["body"])["payload"]
        kind, order = row["kind"], (row["id"], 0, 0)
        if kind == "account_flagged":
            add(order, p["at"], "Mule scorer", "scorer", f"Flagged {p['bank_id']} account {p['account'][:6]}",
                "; ".join(p["reasons"][:3]).capitalize())
        elif kind == "hold_placed":
            by = "the investigator" if p["source"] == "agent" else "TrailGuard"
            add(order, p["at"], "Investigator" if p["source"] == "agent" else "TrailGuard", "hold",
                f"Asked {p['bank_id']} to hold {_naira(p['requested_kobo'])} in {p['account'][:6]}",
                "Placed automatically the moment the account was flagged" if by == "TrailGuard" else None)
        elif kind == "hold_acknowledged":
            account = hold_accounts.get(p["hold_id"], "")[:6]
            if p["held_kobo"]:
                add(order, p["at"], p["bank_id"], "bank", f"Froze {_naira(p['held_kobo'])} in {account}")
            else:
                add(order, p["at"], p["bank_id"], "quiet", f"Found nothing left to freeze in {account}")
        elif kind == "case_opened":
            add(order, p["reported_at"], p["reporting_bank"], "report", "Reported the theft",
                f"Case {p['case_id']} opened. The 30-minute response window starts now.")
        elif kind == "agent_run_started":
            investigator = add(order, p["as_of"], "Investigator", "agent", "Working the case", None, [])
        elif kind == "agent_tool_call" and investigator is not None:
            investigator["steps"].append(TOOL_WORDS.get(p["tool"], p["tool"]) + (" (failed)" if p["failed"] else ""))
        elif kind == "agent_run_finished" and investigator is not None:
            investigator["title"] = (f"Finished the case in {p['tool_calls']} steps" if p["outcome"] == "completed"
                                     else f"Stopped: {p['outcome']}")
            investigator["detail"] = "Its handover to the fraud desks is below."
        elif kind == "hold_reviewed":
            add(order, p["at"], p["reviewer"], "bank",
                f"{'Confirmed' if p['decision'] == 'confirm' else 'Released'} hold {p['hold_id']}")
    for i, blocked in enumerate(_state.blocked):
        add((blocked["seq"], 1, i), blocked["at"], blocked["bank"], "stop",
            f"Refused to let {_naira(blocked['amount_kobo'])} leave {blocked['account'][:6]}",
            "The money is under hold, so the transfer was declined.")
    items.sort(key=lambda item: item.pop("order"), reverse=True)
    return items


def status(conn: psycopg.Connection) -> dict:
    now = clock.now()
    return {
        "phase": _state.phase, "error": _state.error, "now": now, "rate": clock.rate(),
        "scenario": _state.scenario, "case_id": _state.case_id, "agent_outcome": _state.agent_outcome,
        "feed": feed(conn, now) if _state.phase != "idle" else [],
    }
