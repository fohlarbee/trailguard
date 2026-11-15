"""A simulated bank's side of a hold.

A real bank's connector would pass each hold request to the bank's own systems and
report back what was held. Here the bank is a simulator run, so the answer is
worked out from the run: the hold catches whatever the account's balance was at
the moment the hold was placed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx

from simulator.output import read_jsonl

from .hashing import hash_value


class SimulatedBank:
    def __init__(self, run: Path, bank: str, salt: str):
        self.bank = bank
        self.opening: dict[str, tuple[datetime, int]] = {}
        self.raw: dict[str, str] = {}
        for account in read_jsonl(Path(run) / "accounts.jsonl"):
            if account["bank_id"] == bank:
                number = account["account_number"]
                self.opening[number] = (datetime.fromisoformat(account["opened_at"]),
                                        account["opening_balance_kobo"])
                self.raw[hash_value(salt, "account", number)] = number
        self.moves: dict[str, list[tuple[datetime, int]]] = {}
        for event in read_jsonl(Path(run) / "banks" / bank / "events.jsonl"):
            if event["type"] != "transaction":
                continue
            when = datetime.fromisoformat(event["timestamp"])
            if event["sender_bank"] == bank:
                self.moves.setdefault(event["sender_account"], []).append((when, -event["amount_kobo"]))
            if event["receiver_bank"] == bank:
                self.moves.setdefault(event["receiver_account"], []).append((when, event["amount_kobo"]))

    def balance_at(self, hashed_account: str, when: datetime) -> int:
        number = self.raw[hashed_account]
        opened_at, balance = self.opening[number]
        if when < opened_at:
            return 0
        return balance + sum(amount for at, amount in self.moves.get(number, []) if at <= when)

    def answer_holds(self, client: httpx.Client, key: str, delay: timedelta = timedelta(seconds=30)) -> dict:
        """Acknowledge every hold waiting on this bank. Returns counts and the total held."""
        totals = {"answered": 0, "active": 0, "declined": 0, "held_kobo": 0}
        headers = {"X-API-Key": key}
        waiting = client.get("/v1/holds", params={"unanswered": "true"}, headers=headers)
        waiting.raise_for_status()
        for hold in waiting.json():
            placed_at = datetime.fromisoformat(hold["placed_at"])
            held = max(0, min(hold["requested_kobo"], self.balance_at(hold["account"], placed_at)))
            response = client.post(f"/v1/holds/{hold['hold_id']}/ack", headers=headers,
                                   json={"held_kobo": held, "at": (placed_at + delay).isoformat()})
            response.raise_for_status()
            totals["answered"] += 1
            totals["active" if held else "declined"] += 1
            totals["held_kobo"] += held
        return totals
