"""The static world: institutions, customers and their accounts."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from . import calibration as cal
from .config import Config

DAY = 86400


@dataclass
class Institution:
    id: str
    name: str
    kind: str   # "bank" or "fintech"
    code: str   # six-digit institution code used in session references


@dataclass
class Account:
    number: str
    bank_id: str
    owner_id: str
    bvn: str
    phone: str
    device_id: str
    kyc_tier: int
    account_type: str        # personal, business, pos_agent
    opened_t: int            # seconds relative to the run start; negative means before it
    opening_balance: int     # kobo
    # Internal label, never written to the event stream. One of the customer
    # segments, or: business, pos_agent, exit_merchant, collector, mule, new_account.
    segment: str


@dataclass
class World:
    institutions: dict[str, Institution] = field(default_factory=dict)
    accounts: dict[str, Account] = field(default_factory=dict)
    # Accounts grouped by owner, for customers who bank in more than one place.
    by_owner: dict[str, list[str]] = field(default_factory=dict)
    _used: set[str] = field(default_factory=set)
    _owner_count: int = 0

    # ------------------------------------------------------------ identifiers

    def _unique(self, rng: random.Random, digits: int, prefix: str = "") -> str:
        """A random numeric identifier that has not been issued before."""
        while True:
            value = prefix + "".join(str(rng.randrange(10)) for _ in range(digits - len(prefix)))
            if value not in self._used:
                self._used.add(value)
                return value

    def new_owner(self) -> str:
        self._owner_count += 1
        return f"C{self._owner_count:07d}"

    def new_bvn(self, rng: random.Random) -> str:
        return self._unique(rng, 11, "22")

    def new_phone(self, rng: random.Random) -> str:
        return self._unique(rng, 11, rng.choice(["080", "081", "070", "090", "091"]))

    def new_device(self, rng: random.Random) -> str:
        return "DEV-" + self._unique(rng, 12)

    # --------------------------------------------------------------- accounts

    def add_account(
        self,
        rng: random.Random,
        *,
        bank_id: str,
        owner_id: str,
        bvn: str,
        phone: str,
        device_id: str,
        kyc_tier: int,
        account_type: str,
        opened_t: int,
        opening_balance: int,
        segment: str,
    ) -> Account:
        account = Account(
            number=self._unique(rng, 10),
            bank_id=bank_id,
            owner_id=owner_id,
            bvn=bvn,
            phone=phone,
            device_id=device_id,
            kyc_tier=kyc_tier,
            account_type=account_type,
            opened_t=int(opened_t),
            opening_balance=int(opening_balance),
            segment=segment,
        )
        self.accounts[account.number] = account
        self.by_owner.setdefault(owner_id, []).append(account.number)
        return account

    def in_segment(self, *segments: str) -> list[Account]:
        return [a for a in self.accounts.values() if a.segment in segments]

    def banks(self, kind: str | None = None) -> list[str]:
        return [i.id for i in self.institutions.values() if kind is None or i.kind == kind]


# --------------------------------------------------------------------- helpers


def lognormal(rng: random.Random, median: float, sigma: float) -> float:
    return rng.lognormvariate(math.log(median), sigma)


def kobo(naira: float) -> int:
    """Whole naira, expressed in kobo."""
    return int(naira) * 100


def pick(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


# ----------------------------------------------------------------------- build


def build_world(cfg: Config, rng: random.Random) -> World:
    """Create institutions and every legitimate account. Mules are added by fraud.py."""
    world = World()

    for i in range(cfg.banks):
        world.institutions[f"BNK{i + 1:02d}"] = Institution(
            f"BNK{i + 1:02d}", f"Sim Bank {i + 1}", "bank", f"0000{i + 11:02d}"
        )
    for i in range(cfg.fintechs):
        world.institutions[f"FIN{i + 1:02d}"] = Institution(
            f"FIN{i + 1:02d}", f"Sim Fintech {i + 1}", "fintech", f"1000{i + 11:02d}"
        )
    everyone = world.banks()
    fintechs = world.banks("fintech")
    # Customers are spread unevenly: earlier institutions are larger.
    bank_weights = [1.0 / (rank + 1) ** 0.5 for rank in range(len(everyone))]

    # Ordinary customers.
    for _ in range(cfg.customers):
        segment = pick(rng, {name: spec["share"] for name, spec in cal.SEGMENTS.items()})
        spec = cal.SEGMENTS[segment]
        owner = world.new_owner()
        bvn, phone, device = world.new_bvn(rng), world.new_phone(rng), world.new_device(rng)
        home = rng.choices(everyone, weights=bank_weights)[0]
        age_days = min(max(lognormal(rng, 900, 1.0), 45), 6000)
        balance = lognormal(rng, *spec["balance"])
        tier = 1 if segment == "student" and rng.random() < 0.6 else rng.choice([2, 3, 3])
        has_second = segment != "dormant" and rng.random() < cal.SECOND_ACCOUNT_SHARE

        world.add_account(
            rng, bank_id=home, owner_id=owner, bvn=bvn, phone=phone, device_id=device,
            kyc_tier=tier, account_type="personal", opened_t=-age_days * DAY,
            opening_balance=kobo(balance * (0.7 if has_second else 1.0)), segment=segment,
        )
        if has_second:
            other = rng.choice([b for b in everyone if b != home])
            world.add_account(
                rng, bank_id=other, owner_id=owner, bvn=bvn, phone=phone, device_id=device,
                kyc_tier=tier, account_type="personal", opened_t=-rng.uniform(45, age_days) * DAY,
                opening_balance=kobo(balance * 0.3), segment=segment,
            )

    def organisation(segment: str, account_type: str, bank_id: str, balance: float) -> Account:
        return world.add_account(
            rng, bank_id=bank_id, owner_id=world.new_owner(), bvn=world.new_bvn(rng),
            phone=world.new_phone(rng), device_id=world.new_device(rng), kyc_tier=3,
            account_type=account_type, opened_t=-rng.uniform(200, 4000) * DAY,
            opening_balance=kobo(balance), segment=segment,
        )

    # Businesses: they take merchant payments, pay suppliers in batches, and some pay salaries.
    for _ in range(max(10, cfg.customers // 100)):
        organisation("business", "business", rng.choices(everyone, weights=bank_weights)[0],
                     lognormal(rng, 5_000_000, 1.0))

    # POS agents: where ordinary customers, and mules, turn balances into cash.
    for _ in range(max(10, cfg.customers // 80)):
        organisation("pos_agent", "pos_agent", rng.choice(fintechs), lognormal(rng, 300_000, 0.8))

    # Exit merchants: crypto and betting wallets. Ordinary customers pay them too.
    for _ in range(4):
        organisation("exit_merchant", "business", rng.choice(everyone), lognormal(rng, 20_000_000, 0.5))

    return world
