"""Fraud scenarios and their ground truth.

Each case is a theft followed by a plan: a tree of mule accounts that forward the
money and finally cash it out. The plan runs on the shared event queue, so every
step works with the balance the account really has at that moment.

Taint rule: money moved by a fraud step is counted as stolen money first. A
recruited mule's own everyday spending is treated as spending their own money.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import calibration as cal
from .config import Config
from .ledger import Ledger
from .normal import DEVICE_CHANNELS, device_for
from .world import DAY, Account, World, kobo, lognormal, pick


@dataclass
class Ring:
    id: str
    mules: list[Account] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    first_use: int = 0


@dataclass
class Node:
    """One mule in a case's plan, and what it does with the money it receives."""
    account: Account
    action: str                                  # "forward" or "cashout"
    children: list[tuple["Node", float]] = field(default_factory=list)
    keep: float = 0.0                            # share kept as commission
    mix_own: float = 0.0                         # own money added on top, as a share
    recruited: bool = False


@dataclass
class Case:
    id: str
    scenario: str
    method: str
    channel: str
    target: int
    t0: int
    ring: Ring
    root: Node | None = None
    victim: Account | None = None
    stolen: int = 0
    theft_ids: list[str] = field(default_factory=list)
    tainted_txs: list[dict] = field(default_factory=list)
    cash_outs: list[dict] = field(default_factory=list)
    tainted: dict[str, int] = field(default_factory=dict)
    mules: dict[str, dict] = field(default_factory=dict)
    report_id: str | None = None
    reported_at: str | None = None


class FraudEngine:
    def __init__(self, cfg: Config, world: World, ledger: Ledger, rng: random.Random):
        self.cfg, self.world, self.ledger, self.rng = cfg, world, ledger, rng
        self.cases: list[Case] = []
        self.rings: list[Ring] = []
        self.used_victims: set[str] = set()
        self.recruited: set[str] = set()
        self.agents = world.in_segment("pos_agent")
        self.exits = world.in_segment("exit_merchant")
        self._setup()

    # ------------------------------------------------------------------ setup

    def _setup(self) -> None:
        rng, cfg = self.rng, self.cfg

        times = []
        for _ in range(cfg.fraud_cases):
            if rng.random() < cal.FRAUD_ODD_HOUR_SHARE:
                hour = rng.choice(cal.ODD_HOURS)
            else:
                hour = rng.choices(range(24), weights=cal.HOUR_WEIGHTS)[0]
            t0 = rng.randrange(cfg.days) * DAY + hour * 3600 + rng.randrange(3600)
            # Leave room for mule accounts to exist before they are first used.
            times.append(max(t0, 2 * 3600 + rng.randrange(3600)))
        times.sort()

        rings = [Ring(f"RING{i + 1:03d}") for i in range(max(2, cfg.fraud_cases // 3))]
        used: list[Ring] = []
        for i, t0 in enumerate(times):
            ring = rng.choice(rings)
            if ring not in used:
                used.append(ring)
                ring.first_use = t0
            scenario = pick(rng, cfg.scenario_weights)
            if scenario == "insider":
                method, channel = "insider", "web"
            else:
                method = ("social_engineering" if rng.random() < cal.SOCIAL_ENGINEERING_SHARE
                          else "account_takeover")
                channel = pick(rng, cal.FRAUD_CHANNEL_WEIGHTS)
            naira = lognormal(rng, *cal.FRAUD_AMOUNT[channel])
            target = kobo(min(max(naira, cal.FRAUD_AMOUNT_MIN), cal.FRAUD_AMOUNT_MAX))
            self.cases.append(Case(f"CASE{i + 1:04d}", scenario, method, channel, target, t0, ring))

        self.rings = sorted(used, key=lambda r: r.id)
        for ring in self.rings:
            self._populate(ring)
        for case in self.cases:
            case.root = self._plan(case)
            self.ledger.schedule(case.t0, self._theft, case)

    def _populate(self, ring: Ring) -> None:
        """Open the ring's mule accounts shortly before the ring is first used."""
        rng, world = self.rng, self.world
        ring.devices = [world.new_device(rng) for _ in range(cal.RING_SHARED_DEVICES)]
        for _ in range(rng.randint(*cal.RING_SIZE_RANGE)):
            kind = "fintech" if rng.random() < cal.MULE_FINTECH_SHARE else "bank"
            bank = rng.choice(world.banks(kind) or world.banks())
            # Some mules are one person holding accounts at several institutions.
            twins = [m for m in ring.mules if m.bank_id != bank]
            if twins and rng.random() < cal.MULE_SHARED_BVN_SHARE:
                twin = rng.choice(twins)
                owner, bvn, phone = twin.owner_id, twin.bvn, twin.phone
            else:
                owner, bvn, phone = world.new_owner(), world.new_bvn(rng), world.new_phone(rng)
            shared = rng.random() < cal.MULE_SHARED_DEVICE_SHARE
            ring.mules.append(world.add_account(
                rng, bank_id=bank, owner_id=owner, bvn=bvn, phone=phone,
                device_id=rng.choice(ring.devices) if shared else world.new_device(rng),
                kyc_tier=1, account_type="personal",
                opened_t=ring.first_use - rng.uniform(3600, cal.MULE_MAX_AGE_DAYS * DAY),
                opening_balance=kobo(rng.uniform(0, 5_000)), segment="mule",
            ))

    def _plan(self, case: Case) -> Node:
        rng = self.rng
        pool = rng.sample(case.ring.mules, len(case.ring.mules))

        def keep() -> float:
            return rng.uniform(*cal.MULE_KEEP_RANGE)

        def cashout() -> Node:
            return Node(pool.pop(), "cashout", keep=keep())

        def split(node: Node, count: int, extra_hop_chance: float = 0.0) -> Node:
            children = []
            for _ in range(count):
                if not pool:
                    break
                child = cashout()
                if pool and rng.random() < extra_hop_chance:
                    child.action = "forward"
                    child.children = [(cashout(), 1.0)]
                children.append(child)
            cuts = [rng.uniform(0.6, 1.4) for _ in children]
            node.children = [(child, cut / sum(cuts)) for child, cut in zip(children, cuts)]
            return node

        if case.scenario == "fast_cashout":
            return cashout()
        if case.scenario == "fan_out":
            return split(Node(pool.pop(), "forward", keep=keep()), rng.randint(2, 6), 0.25)
        if case.scenario == "insider":
            return split(Node(pool.pop(), "forward", keep=keep()), rng.randint(2, 3))
        if case.scenario == "layering":
            root = node = Node(pool.pop(), "forward", keep=keep())
            for _ in range(rng.randint(2, 4) - 2):
                nxt = Node(pool.pop(), "forward", keep=keep())
                node.children = [(nxt, 1.0)]
                node = nxt
            node.children = [(cashout(), 1.0)]
            return root
        # commingled: the first hop is an established customer recruited as a mule.
        candidates = [
            a for a in self.world.in_segment("salaried", "trader")
            if a.number not in self.recruited and a.opened_t < -180 * DAY
            and kobo(50_000) <= a.opening_balance <= kobo(1_000_000)
        ]
        recruit = rng.choice(candidates)
        self.recruited.add(recruit.number)
        root = Node(recruit, "forward", keep=rng.uniform(0.05, 0.15),
                    mix_own=rng.uniform(0.1, 0.5), recruited=True)
        return split(root, rng.randint(1, 2))

    # -------------------------------------------------------------- execution

    def _hop_delay(self) -> int:
        delay = lognormal(self.rng, cal.HOP_DELAY_MEDIAN_S, cal.HOP_DELAY_SIGMA)
        return int(min(max(delay, cal.HOP_DELAY_MIN_S), cal.HOP_DELAY_MAX_S))

    def _theft(self, t: int, case: Case) -> None:
        rng, ledger = self.rng, self.ledger
        segments = ("dormant",) if case.method == "insider" else ("salaried", "trader", "student")
        pool = [
            a for a in self.world.in_segment(*segments)
            if a.number not in self.used_victims and a.number not in self.recruited
            and ledger.is_open(a.number)
        ]
        if not pool:
            return
        able = [a for a in pool if ledger.balance(a.number) >= case.target * 1.1]
        if able:
            victim, amount = rng.choice(able), case.target
        else:
            # Nobody holds that much right now: take most of the richest account instead.
            victim = max(pool, key=lambda a: ledger.balance(a.number))
            amount = int(ledger.balance(victim.number) * 0.9)
        if amount < kobo(1_000):
            return
        case.victim = victim
        self.used_victims.add(victim.number)

        if case.channel not in DEVICE_CHANNELS:
            device = None
        elif case.method == "social_engineering":
            device = victim.device_id          # the victim was tricked into sending it
        elif case.method == "account_takeover":
            device = case.ring.devices[0]      # the fraudster's own device
        else:
            device = f"TERM-{victim.bank_id}-{rng.randrange(100):03d}"   # a staff terminal

        if rng.random() < 0.3:
            first = int(amount * rng.uniform(0.5, 0.7))
            last_t = t + rng.randint(60, 240)
            self._steal(t, case, first, device)
            ledger.schedule(last_t, self._steal, case, amount - first, device)
        else:
            last_t = t
            self._steal(t, case, amount, device)
        ledger.schedule(last_t + self._hop_delay(), self._act, case, case.root, 1)

        delay = lognormal(rng, cal.REPORT_DELAY_MEDIAN_S, cal.REPORT_DELAY_SIGMA)
        delay = min(max(delay, cal.REPORT_DELAY_MIN_S), cal.REPORT_DELAY_MAX_S)
        ledger.schedule(t + int(delay), self._report, case)

    def _steal(self, t: int, case: Case, amount: int, device: str | None) -> None:
        root = case.root.account.number
        tx = self.ledger.transfer(t, case.victim.number, root, amount, case.channel, device)
        if tx:
            case.stolen += amount
            case.theft_ids.append(tx.transaction_id)
            case.tainted[root] = case.tainted.get(root, 0) + amount
            case.tainted_txs.append({"transaction_id": tx.transaction_id, "tainted_kobo": amount, "hop": 1})

    def _report(self, t: int, case: Case) -> None:
        if case.theft_ids:
            report = self.ledger.report_fraud(t, case.victim.number, list(case.theft_ids))
            case.report_id, case.reported_at = report.report_id, report.reported_at

    def _act(self, t: int, case: Case, node: Node, hop: int) -> None:
        """A mule has received money at this hop and now moves it on."""
        held = case.tainted.get(node.account.number, 0)
        if held <= 0:
            return
        role = "cash_out" if node.action == "cashout" else ("first_hop" if hop == 1 else "layer")
        case.mules[node.account.number] = {
            "account": node.account.number, "bank": node.account.bank_id,
            "hop": hop, "role": role, "recruited": node.recruited,
        }
        moving = int(held * (1 - node.keep))
        if node.action == "forward":
            for child, share in node.children:
                t += self.rng.randint(5, 90)
                self.ledger.schedule(t, self._send, case, node, child, int(moving * share), hop)
            return

        if moving > kobo(cal.CASH_EXIT_THRESHOLD_NAIRA):
            self._cash(t, case, node, "merchant_exit", moving, hop)
            return
        mode = self.rng.choice(["atm", "pos_agent"])
        limit = kobo(cal.ATM_MAX_NAIRA if mode == "atm" else cal.POS_AGENT_MAX_NAIRA)
        while moving > 0:
            chunk = min(moving, limit)
            t += self.rng.randint(40, 150)
            self.ledger.schedule(t, self._cash, case, node, mode, chunk, hop)
            moving -= chunk

    def _send(self, t: int, case: Case, node: Node, child: Node, amount: int, hop: int) -> None:
        src = node.account.number
        tainted = min(amount, case.tainted.get(src, 0))
        total = min(tainted + int(tainted * node.mix_own), self.ledger.balance(src))
        channel = self.rng.choice(["mobile", "mobile", "mobile", "web", "ussd"])
        tx = self.ledger.transfer(t, src, child.account.number, total, channel,
                                  device_for(node.account, channel))
        if not tx:
            return
        tainted = min(tainted, total)
        case.tainted[src] -= tainted
        case.tainted[child.account.number] = case.tainted.get(child.account.number, 0) + tainted
        case.tainted_txs.append({"transaction_id": tx.transaction_id, "tainted_kobo": tainted, "hop": hop + 1})
        self.ledger.schedule(t + self._hop_delay(), self._act, case, child, hop + 1)

    def _cash(self, t: int, case: Case, node: Node, mode: str, amount: int, hop: int) -> None:
        """Take money out of the banking system: ATM, POS agent, or an exit merchant."""
        src = node.account.number
        amount = min(amount, case.tainted.get(src, 0), self.ledger.balance(src))
        if mode == "atm":
            amount -= amount % kobo(1_000)      # ATMs dispense whole notes
            tx = self.ledger.transfer(t, src, None, amount, "atm", None)
        elif mode == "pos_agent":
            tx = self.ledger.transfer(t, src, self.rng.choice(self.agents).number, amount, "pos", None)
        else:
            channel = self.rng.choice(["mobile", "web"])
            tx = self.ledger.transfer(t, src, self.rng.choice(self.exits).number, amount, channel,
                                      node.account.device_id)
        if not tx:
            return
        case.tainted[src] -= amount
        case.tainted_txs.append({"transaction_id": tx.transaction_id, "tainted_kobo": amount, "hop": hop + 1})
        case.cash_outs.append({
            "transaction_id": tx.transaction_id, "type": mode,
            "account": tx.receiver_account, "bank": tx.receiver_bank, "tainted_kobo": amount,
        })

    # ----------------------------------------------------------- ground truth

    def ground_truth(self, lookalikes: list[dict]) -> dict:
        cases = []
        for case in self.cases:
            if not case.theft_ids:
                continue
            cases.append({
                "case_id": case.id,
                "scenario": case.scenario,
                "method": case.method,
                "channel": case.channel,
                "ring_id": case.ring.id,
                "victim_account": case.victim.number,
                "victim_bank": case.victim.bank_id,
                "stolen_kobo": case.stolen,
                "theft_transaction_ids": case.theft_ids,
                "report_id": case.report_id,
                "reported_at": case.reported_at,
                "mule_accounts": sorted(case.mules.values(), key=lambda m: (m["hop"], m["account"])),
                "tainted_transactions": case.tainted_txs,
                "cash_out_points": case.cash_outs,
                # Stolen money still sitting in mule accounts when the run ended.
                "residual": [{"account": acct, "tainted_kobo": amount}
                             for acct, amount in case.tainted.items() if amount > 0],
            })

        handled: dict[str, dict] = {}
        for case in cases:
            for mule in case["mule_accounts"]:
                handled.setdefault(mule["account"], {
                    "account": mule["account"], "bank": mule["bank"], "recruited": mule["recruited"],
                })
        return {
            "cases": cases,
            "mule_accounts": sorted(handled.values(), key=lambda m: m["account"]),
            "rings": [{"ring_id": r.id, "accounts": [m.number for m in r.mules],
                       "shared_devices": r.devices} for r in self.rings],
            "lookalikes": lookalikes,
        }
