"""The investigator's tools.

Each tool is plain code acting on the database. The model chooses which to call
and with what arguments; it never computes a trail, a score or an amount itself.
A Toolbox is bound to one case and one moment in time, so the agent cannot act on
another case or see anything that happens after its clock.

Tool results contain only hashed identifiers. Money is reported in naira.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime

import psycopg

from core import evidence, holds, scoring, tracer

# Sent to the model. `strict` makes the API guarantee the arguments match the schema.
def _tool(name: str, description: str, properties: dict | None = None) -> dict:
    properties = properties or {}
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": properties,
                         "required": list(properties), "additionalProperties": False},
    }


TOOLS = [
    _tool("get_case",
          "Read the case: who reported it, when, the transactions the victim reported, and any "
          "earlier trace, holds, evidence packs or report drafts already recorded for it."),
    _tool("trace_funds",
          "Follow the stolen money from the reported transactions, account by account, using every "
          "transaction known up to now, and store the result. Returns each movement of stolen money, "
          "where it left the banking system, and how much is still sitting in which accounts. "
          "Run it again later to pick up money that has moved since."),
    _tool("score_accounts",
          "Get the mule-risk score, tier and reasons for accounts, as of now. Use it on accounts in "
          "the trail to judge how confident a hold is.",
          {"accounts": {"type": "array", "items": {"type": "string"},
                        "description": "Hashed account identifiers, as returned by other tools."}}),
    _tool("find_ring",
          "Find accounts linked to an account by a shared registered device or BVN, with any alert or "
          "open hold on each. Use it to see whether a trail account belongs to a wider ring.",
          {"account": {"type": "string", "description": "Hashed account identifier."}}),
    _tool("place_hold",
          "Ask an account's bank to hold stolen money. The hold is temporary: it lapses after 24 hours "
          "unless a human confirms it, and a human can release it at any time. Only accounts that the "
          "latest trace shows are still holding this case's stolen money can be held, and only up to "
          "that amount. If the account already has an open hold, that hold is linked to this case.",
          {"account": {"type": "string", "description": "Hashed account identifier from the trace."},
           "amount_naira": {"type": "number", "description": "Amount to hold, at most what the trace shows."},
           "reason": {"type": "string", "description": "One or two sentences a bank officer can act on."}}),
    _tool("list_holds",
          "List the holds linked to this case with their status: requested (bank has not answered), "
          "active (bank holds money, with the amount), declined, released or expired."),
    _tool("remind_bank",
          "Chase a bank that has not acknowledged a hold. Only works on holds still in 'requested', and "
          "only once the bank has had 10 minutes since the hold was placed or since the last reminder.",
          {"hold_id": {"type": "integer", "description": "The hold to chase."}}),
    _tool("recommend_hold_reduction",
          "Recommend that a bank holds less on an active hold, when it is freezing more than the traces "
          "show is stolen. Automatic holds are sized on what an account received, which can include "
          "money that is not stolen. Nothing is released by this: a person at the bank decides. The "
          "amount cannot go below the stolen money traced into the account across all cases.",
          {"hold_id": {"type": "integer", "description": "An active hold on this case's trail."},
           "amount_naira": {"type": "number", "description": "The amount the hold should be reduced to."},
           "reason": {"type": "string", "description": "Why the rest can be released, for the bank officer."}}),
    _tool("build_evidence_pack",
          "Assemble the evidence pack for the case from the stored trail, alerts, holds and audit "
          "entries, and seal it with a hash. Trace the case first."),
    _tool("draft_str",
          "Draft the suspicious transaction report. The facts and figures are filled in from the "
          "database; you supply the narrative. The draft is not filed: a human reviews and files it.",
          {"narrative": {"type": "string",
                         "description": "A factual account of what happened, for a compliance officer."}}),
]


def naira(kobo: int | None) -> float | None:
    return None if kobo is None else round(kobo / 100, 2)


class Toolbox:
    def __init__(self, conn: psycopg.Connection, case_id: str, now: datetime,
                 clock: Callable[[], datetime] | None = None):
        """`now` is when the agent works. With `clock`, each tool call reads the time from it instead."""
        self.conn, self.case_id, self._now, self._clock = conn, case_id, now, clock

    @property
    def now(self) -> datetime:
        return self._clock() if self._clock else self._now

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Run a tool. Returns (result as JSON text, whether it failed)."""
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return json.dumps({"error": f"unknown tool {name}"}), True
        try:
            with self.conn.transaction():
                result = handler(**arguments)
        except (LookupError, ValueError, RuntimeError) as error:
            return json.dumps({"error": str(error)}), True
        return json.dumps(result, default=str), False

    # ------------------------------------------------------------------ tools

    def _get_case(self) -> dict:
        facts = evidence.case_facts(self.conn, self.case_id, self.now)
        case = facts["case"]
        return {
            "case_id": self.case_id,
            "now": self.now,
            "reported_at": case["reported_at"],
            "minutes_since_report": round((self.now - case["reported_at"]).total_seconds() / 60, 1),
            "reporting_bank": case["reporting_bank"],
            "victim_account": case["victim_account"],
            "reported_transactions": [
                {"transaction_id": t["transaction_id"], "at": t["ts"], "amount_naira": naira(t["amount_kobo"]),
                 "to_account": t["receiver_account"], "to_bank": t["receiver_bank"], "channel": t["channel"]}
                for t in facts["reported_transactions"]],
            "already_traced": bool(facts["trail"]),
            "holds_on_case": len(facts["holds"]),
            "evidence_packs": self.conn.execute(
                "SELECT count(*) AS n FROM evidence_packs WHERE case_id = %s", (self.case_id,)).fetchone()["n"],
            "report_drafts": self.conn.execute(
                "SELECT count(*) AS n FROM str_drafts WHERE case_id = %s", (self.case_id,)).fetchone()["n"],
        }

    def _trace_funds(self) -> dict:
        trail = tracer.trace(self.conn, self.case_id, as_of=self.now)
        tracer.save(self.conn, trail)
        flagged = {r["account"] for r in self.conn.execute(
            "SELECT account FROM alerts WHERE account = ANY(%s) AND first_flagged_at <= %s",
            ([h.account for h in trail.holdings], self.now))}
        held = {h["account"]: h for h in holds.listing(
            self.conn, accounts=[h.account for h in trail.holdings], now=self.now) if h["status"] in holds.OPEN}
        return {
            "as_of": self.now,
            "stolen_naira": naira(trail.stolen_kobo),
            "left_the_system_naira": naira(trail.exited_kobo),
            "still_in_accounts_naira": naira(trail.holdable_kobo),
            "movements": [
                {"hop": h.hop, "at": h.ts, "transaction_id": h.transaction_id,
                 "from_account": h.sender_account, "from_bank": h.sender_bank,
                 "to_account": h.receiver_account, "to_bank": h.receiver_bank,
                 "stolen_naira": naira(h.tainted_kobo), "sent_naira": naira(h.amount_kobo),
                 "channel": h.channel, "left_system_via": h.endpoint}
                for h in trail.hops],
            "accounts_still_holding_stolen_money": [
                {"account": h.account, "bank": h.bank_id, "hop": h.hop, "stolen_naira": naira(h.tainted_kobo),
                 "flagged_by_scorer": h.account in flagged,
                 "open_hold_id": held[h.account]["hold_id"] if h.account in held else None}
                for h in trail.holdings],
        }

    def _score_accounts(self, accounts: list[str]) -> dict:
        scored = []
        for account in accounts[:25]:
            try:
                result = scoring.score_account(self.conn, account, as_of=self.now)
            except LookupError:
                scored.append({"account": account, "error": "not at a connected institution"})
                continue
            scored.append({key: result[key] for key in ("account", "bank_id", "score", "tier", "reasons")})
        return {"as_of": self.now, "accounts": scored}

    def _find_ring(self, account: str) -> dict:
        info = self.conn.execute("SELECT device, bvn FROM accounts WHERE account = %s", (account,)).fetchone()
        if not info:
            raise LookupError(f"account {account} is not at a connected institution")
        linked = self.conn.execute(
            """SELECT a.account, a.bank_id, a.opened_at, a.device = %(device)s AS same_device,
                      a.bvn = %(bvn)s AS same_bvn, al.first_flagged_at, h.hold_id
               FROM accounts a
               LEFT JOIN alerts al ON al.account = a.account AND al.first_flagged_at <= %(now)s
               LEFT JOIN holds h ON h.account = a.account AND h.status IN ('requested', 'active')
                    AND h.placed_at <= %(now)s AND (h.review IS NOT NULL OR h.expires_at > %(now)s)
               WHERE (a.device = %(device)s OR a.bvn = %(bvn)s) AND a.account <> %(account)s
                 AND a.opened_at <= %(now)s
               ORDER BY a.opened_at LIMIT 25""",
            {"device": info["device"], "bvn": info["bvn"], "account": account, "now": self.now},
        ).fetchall()
        return {"account": account, "linked_accounts": [
            {"account": r["account"], "bank": r["bank_id"], "opened_at": r["opened_at"],
             "shares": [name for name, shared in (("device", r["same_device"]), ("bvn", r["same_bvn"])) if shared],
             "flagged_by_scorer": r["first_flagged_at"] is not None, "open_hold_id": r["hold_id"]}
            for r in linked]}

    def _place_hold(self, account: str, amount_naira: float, reason: str) -> dict:
        # Policy enforced here, not left to the model: only money the trace shows.
        holding = self.conn.execute(
            "SELECT tainted_kobo FROM trail_holdings WHERE case_id = %s AND account = %s",
            (self.case_id, account)).fetchone()
        if not holding:
            raise ValueError("the latest trace does not show this account holding stolen money from this "
                             "case, so it cannot be held; run trace_funds if the money may have moved")
        amount = round(amount_naira * 100)
        if amount > holding["tainted_kobo"]:
            raise ValueError(f"the trace shows N{holding['tainted_kobo'] / 100:,.2f} of stolen money in "
                             "this account; a hold cannot exceed that")
        hold = holds.place(self.conn, account=account, amount_kobo=amount, reason=reason,
                           source="agent", now=self.now, case_id=self.case_id)
        return self._hold_view(hold) | {"already_held": hold["already_held"]}

    def _list_holds(self) -> dict:
        accounts = [r["account"] for r in self.conn.execute(
            "SELECT account FROM trail_holdings WHERE case_id = %s", (self.case_id,))]
        return {"as_of": self.now, "holds": [self._hold_view(h) for h in holds.listing(
            self.conn, case_id=self.case_id, accounts=accounts, now=self.now)]}

    def _remind_bank(self, hold_id: int) -> dict:
        on_case = {h["hold_id"] for h in self._list_holds()["holds"]}
        if hold_id not in on_case:
            raise LookupError(f"hold {hold_id} is not part of this case")
        return self._hold_view(holds.remind(self.conn, hold_id, self.now))

    def _recommend_hold_reduction(self, hold_id: int, amount_naira: float, reason: str) -> dict:
        if hold_id not in {h["hold_id"] for h in self._list_holds()["holds"]}:
            raise LookupError(f"hold {hold_id} is not part of this case")
        return self._hold_view(holds.recommend_reduction(
            self.conn, hold_id, round(amount_naira * 100), reason, self.now))

    def _build_evidence_pack(self) -> dict:
        return evidence.build_pack(self.conn, self.case_id, self.now)

    def _draft_str(self, narrative: str) -> dict:
        return evidence.draft_str(self.conn, self.case_id, narrative, self.now)

    def _hold_view(self, hold: dict) -> dict:
        return {
            "hold_id": hold["hold_id"], "account": hold["account"], "bank": hold["bank_id"],
            "status": hold["status"], "requested_naira": naira(hold["requested_kobo"]),
            "held_naira": naira(hold["held_kobo"]), "placed_by": hold["source"], "placed_at": hold["placed_at"],
            "expires_at": hold["expires_at"], "reminders_sent": hold["reminders"],
            "human_review": hold["review"],
            "stolen_traced_in_account_naira": naira(holds.traced_in_account(self.conn, hold["account"])),
            "recommended_reduction_to_naira": naira(hold.get("recommended_kobo")),
            "minutes_waiting_for_bank": (round((self.now - hold["placed_at"]).total_seconds() / 60, 1)
                                         if hold["status"] == "requested" else None),
            "can_be_chased_from": ((hold["last_reminded_at"] or hold["placed_at"]) + holds.REMIND_WAIT
                                   if hold["status"] == "requested" else None),
        }
