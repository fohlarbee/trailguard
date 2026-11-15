"""The fund-trail tracer.

Given a reported theft, follow the stolen money account by account. This is plain
deterministic code: the same case and the same data always give the same trail,
which matters because the result is used as evidence.

The rule, applied to transactions in time order:

  * An account that has received stolen money holds a tainted balance.
  * Each later payment out of that account carries stolen money first, up to the
    tainted balance it holds at that moment.
  * The trail ends where money leaves the monitored system: a cash withdrawal, a
    POS agent, a merchant account, or an institution that is not connected.

Three limits keep the trail from spreading into ordinary activity:

  * window: a payment only counts if it happens soon after the account last
    received stolen money. Mules move money on within minutes; an account's
    spending days later is more likely its own.
  * floor: a payment carrying only a trivial amount of stolen money is ignored.
  * max_hops: the trail is not followed beyond this depth.
"""

from __future__ import annotations

import heapq
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

from . import audit

DEFAULT_WINDOW_S = 2 * 3600
DEFAULT_FLOOR_SHARE = 0.005        # of the stolen amount
DEFAULT_FLOOR_KOBO = 1_000 * 100   # and never less than N1,000
DEFAULT_MAX_HOPS = 8

# Account types where the trail stops, and the label given to the stop.
ENDPOINTS = {"pos_agent": "pos_agent", "business": "merchant"}


@dataclass
class Hop:
    seq: int
    hop: int
    session_ref: str
    transaction_id: str
    ts: datetime
    sender_bank: str
    sender_account: str
    receiver_bank: str | None
    receiver_account: str | None
    amount_kobo: int
    tainted_kobo: int
    channel: str
    endpoint: str | None      # cash, pos_agent, merchant, outside_network, or None


@dataclass
class Holding:
    account: str
    bank_id: str | None
    hop: int
    tainted_kobo: int


@dataclass
class Trail:
    case_id: str
    as_of: datetime | None
    stolen_kobo: int = 0
    hops: list[Hop] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)

    @property
    def exited_kobo(self) -> int:
        """Stolen money that has already left the monitored system."""
        return sum(h.tainted_kobo for h in self.hops if h.endpoint)

    @property
    def holdable_kobo(self) -> int:
        """Stolen money still sitting in accounts, where a hold could catch it."""
        return sum(h.tainted_kobo for h in self.holdings)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "as_of": self.as_of,
            "stolen_kobo": self.stolen_kobo,
            "exited_kobo": self.exited_kobo,
            "holdable_kobo": self.holdable_kobo,
            "hops": [asdict(h) for h in self.hops],
            "holdings": [asdict(h) for h in self.holdings],
        }


_TX_COLUMNS = """session_ref, transaction_id, ts, sender_bank, sender_account,
                 receiver_bank, receiver_account, amount_kobo, channel"""


def trace(
    conn: psycopg.Connection,
    case_id: str,
    as_of: datetime | None = None,
    window_s: int = DEFAULT_WINDOW_S,
    floor_share: float = DEFAULT_FLOOR_SHARE,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> Trail:
    """Trace a case using transactions up to as_of (default: everything known)."""
    case = conn.execute("SELECT * FROM cases WHERE case_id = %s", (case_id,)).fetchone()
    if not case:
        raise LookupError(f"no case {case_id}")

    trail = Trail(case_id, as_of)
    window = timedelta(seconds=window_s)
    until = as_of or datetime(9999, 1, 1, tzinfo=timezone.utc)

    thefts = conn.execute(
        f"""SELECT {_TX_COLUMNS} FROM transactions
            WHERE transaction_id IN (SELECT transaction_id FROM case_transactions WHERE case_id = %s)
              AND sender_account = %s AND ts <= %s""",
        (case_id, case["victim_account"], until),
    ).fetchall()
    trail.stolen_kobo = sum(tx["amount_kobo"] for tx in thefts)
    floor = max(DEFAULT_FLOOR_KOBO, int(trail.stolen_kobo * floor_share))

    tainted: dict[str, int] = {}             # stolen money each account currently holds
    depth: dict[str, int] = {}               # hop at which each account first received it
    last_inflow: dict[str, datetime] = {}
    banks: dict[str, str | None] = {}
    account_types: dict[str, str | None] = {}

    def account_type(account: str) -> str | None:
        if account not in account_types:
            row = conn.execute("SELECT account_type FROM accounts WHERE account = %s", (account,)).fetchone()
            account_types[account] = row["account_type"] if row else None
        return account_types[account]

    # Candidate payments, processed strictly in time order. The victim's own later
    # spending is never followed: only the reported transactions start a trail.
    queue: list[tuple[datetime, str, dict]] = []
    for tx in thefts:
        heapq.heappush(queue, (tx["ts"], tx["session_ref"], tx))

    def follow(account: str, since: datetime) -> None:
        """Queue every payment out of an account after it first received stolen money."""
        rows = conn.execute(
            f"""SELECT {_TX_COLUMNS} FROM transactions
                WHERE sender_account = %s AND ts > %s AND ts <= %s ORDER BY ts""",
            (account, since, until),
        ).fetchall()
        for row in rows:
            heapq.heappush(queue, (row["ts"], row["session_ref"], row))

    victim = case["victim_account"]
    while queue:
        ts, _, tx = heapq.heappop(queue)
        sender, receiver = tx["sender_account"], tx["receiver_account"]

        if sender == victim:
            carried, hop = tx["amount_kobo"], 1
        else:
            if ts - last_inflow[sender] > window:
                continue
            carried = min(tx["amount_kobo"], tainted.get(sender, 0))
            if carried < floor:
                continue
            tainted[sender] -= carried
            hop = depth[sender] + 1

        if receiver is None:
            endpoint = "cash"
        elif account_type(receiver) is None:
            endpoint = "outside_network"
        else:
            endpoint = ENDPOINTS.get(account_type(receiver))

        trail.hops.append(Hop(
            seq=len(trail.hops) + 1, hop=hop, session_ref=tx["session_ref"],
            transaction_id=tx["transaction_id"], ts=ts, sender_bank=tx["sender_bank"],
            sender_account=sender, receiver_bank=tx["receiver_bank"], receiver_account=receiver,
            amount_kobo=tx["amount_kobo"], tainted_kobo=carried, channel=tx["channel"], endpoint=endpoint,
        ))
        if endpoint or receiver == victim:
            continue

        tainted[receiver] = tainted.get(receiver, 0) + carried
        last_inflow[receiver] = ts
        banks[receiver] = tx["receiver_bank"]
        if receiver not in depth:
            depth[receiver] = hop
            if hop < max_hops:
                follow(receiver, ts)

    trail.holdings = [
        Holding(account, banks[account], depth[account], amount)
        for account, amount in sorted(tainted.items(), key=lambda item: (depth[item[0]], item[0]))
        if amount > 0
    ]
    return trail


def save(conn: psycopg.Connection, trail: Trail) -> None:
    """Replace the case's stored trail with this one, and record it in the audit log."""
    with conn.transaction():
        conn.execute("DELETE FROM trail_hops WHERE case_id = %s", (trail.case_id,))
        conn.execute("DELETE FROM trail_holdings WHERE case_id = %s", (trail.case_id,))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO trail_hops VALUES (%(case_id)s, %(seq)s, %(hop)s, %(session_ref)s,
                       %(transaction_id)s, %(ts)s, %(sender_bank)s, %(sender_account)s, %(receiver_bank)s,
                       %(receiver_account)s, %(amount_kobo)s, %(tainted_kobo)s, %(channel)s, %(endpoint)s)""",
                [{"case_id": trail.case_id, **asdict(h)} for h in trail.hops],
            )
            cur.executemany(
                "INSERT INTO trail_holdings VALUES (%(case_id)s, %(account)s, %(bank_id)s, %(hop)s, %(tainted_kobo)s)",
                [{"case_id": trail.case_id, **asdict(h)} for h in trail.holdings],
            )
        conn.execute(
            "UPDATE cases SET stolen_kobo = %s, traced_as_of = %s WHERE case_id = %s",
            (trail.stolen_kobo, trail.as_of, trail.case_id),
        )
        audit.append(conn, "case_traced", {
            "case_id": trail.case_id, "as_of": trail.as_of, "stolen_kobo": trail.stolen_kobo,
            "exited_kobo": trail.exited_kobo, "holdable_kobo": trail.holdable_kobo,
            "transactions": [h.transaction_id for h in trail.hops],
        })
