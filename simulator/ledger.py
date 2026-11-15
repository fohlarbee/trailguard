"""Balances and the time-ordered event queue.

Everything that happens in a run is scheduled on one queue and executed in time
order against live balances. That is what lets a mule forward only what it has
actually received.
"""

from __future__ import annotations

import heapq
from datetime import datetime, timedelta
from typing import Callable

from .schema import AccountOpened, Event, FraudReport, Transaction
from .world import Account, World


class Ledger:
    def __init__(self, world: World, start: datetime):
        self.world = world
        self.start = start
        self.balances: dict[str, int] = {}
        self.events: list[Event] = []
        self._queue: list[tuple[int, int, Callable, tuple]] = []
        self._seq = 0
        self._tx_count = 0
        self._report_count = 0

    def iso(self, t: int) -> str:
        return (self.start + timedelta(seconds=t)).isoformat()

    # ------------------------------------------------------------------ queue

    def schedule(self, t: int, fn: Callable, *args) -> None:
        """Run fn(t, *args) at simulated second t. Ties run in scheduling order."""
        self._seq += 1
        heapq.heappush(self._queue, (int(t), self._seq, fn, args))

    def run(self) -> None:
        while self._queue:
            t, _, fn, args = heapq.heappop(self._queue)
            fn(t, *args)

    # ---------------------------------------------------------------- actions

    def open_account(self, account: Account) -> None:
        """Make an account usable. Accounts opened during the run also emit an event."""
        self.balances[account.number] = account.opening_balance
        if account.opened_t >= 0:
            self.events.append(self.account_record(account))

    def account_record(self, account: Account) -> AccountOpened:
        return AccountOpened(
            account_number=account.number,
            bank_id=account.bank_id,
            bvn=account.bvn,
            phone=account.phone,
            device_id=account.device_id,
            kyc_tier=account.kyc_tier,
            account_type=account.account_type,
            opened_at=self.iso(account.opened_t),
        )

    def is_open(self, number: str) -> bool:
        return number in self.balances

    def balance(self, number: str) -> int:
        return self.balances.get(number, 0)

    def transfer(
        self,
        t: int,
        sender: str,
        receiver: str | None,
        amount: int,
        channel: str,
        device_id: str | None,
    ) -> Transaction | None:
        """Move money now. Returns None if the transfer cannot happen.

        receiver=None is a cash withdrawal: the money leaves the banking system.
        """
        if amount <= 0 or sender == receiver:
            return None
        if self.balances.get(sender, -1) < amount:
            return None
        if receiver is not None and receiver not in self.balances:
            return None

        self.balances[sender] -= amount
        if receiver is not None:
            self.balances[receiver] += amount

        self._tx_count += 1
        sender_bank = self.world.accounts[sender].bank_id
        stamp = (self.start + timedelta(seconds=t)).strftime("%y%m%d%H%M%S")
        tx = Transaction(
            transaction_id=f"TX{self._tx_count:09d}",
            session_ref=f"{self.world.institutions[sender_bank].code}{stamp}{self._tx_count:012d}",
            timestamp=self.iso(t),
            sender_bank=sender_bank,
            sender_account=sender,
            receiver_bank=self.world.accounts[receiver].bank_id if receiver else None,
            receiver_account=receiver,
            amount_kobo=amount,
            channel=channel,
            device_id=device_id,
        )
        self.events.append(tx)
        return tx

    def report_fraud(self, t: int, victim: str, transaction_ids: list[str]) -> FraudReport:
        self._report_count += 1
        report = FraudReport(
            report_id=f"RPT{self._report_count:06d}",
            reported_at=self.iso(t),
            reporting_bank=self.world.accounts[victim].bank_id,
            victim_account=victim,
            transaction_ids=transaction_ids,
        )
        self.events.append(report)
        return report
