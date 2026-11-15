"""Legitimate activity, including the look-alikes that resemble fraud but are not.

Decisions are drawn when each action runs, against the live balance, so an account
never spends money it does not have.
"""

from __future__ import annotations

import math
import random

from . import calibration as cal
from .config import Config
from .ledger import Ledger
from .world import DAY, Account, World, kobo, lognormal, pick

ATM_AMOUNTS = [2_000, 5_000, 10_000, 10_000, 20_000, 20_000, 40_000]
POS_AMOUNTS = [2_000, 5_000, 5_000, 10_000, 10_000, 20_000, 50_000]
DEVICE_CHANNELS = ("mobile", "web")


def poisson(rng: random.Random, mean: float) -> int:
    if mean <= 0:
        return 0
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def time_in_day(rng: random.Random, day: int) -> int:
    hour = rng.choices(range(24), weights=cal.HOUR_WEIGHTS)[0]
    return day * DAY + hour * 3600 + rng.randrange(3600)


def device_for(account: Account, channel: str) -> str | None:
    """Only app and web sessions carry a device ID; USSD, POS and ATM do not."""
    return account.device_id if channel in DEVICE_CHANNELS else None


def schedule_normal(cfg: Config, world: World, ledger: Ledger, rng: random.Random) -> list[dict]:
    """Queue all legitimate activity. Returns the labelled look-alike accounts."""
    people = world.in_segment("salaried", "trader", "student")
    businesses = world.in_segment("business")
    merchants = businesses + world.in_segment("exit_merchant")
    agents = world.in_segment("pos_agent")
    lookalikes: list[dict] = []

    def label(account: Account, kind: str) -> None:
        lookalikes.append({"account": account.number, "bank": account.bank_id, "kind": kind})

    # ------------------------------------------------------ everyday activity

    def everyday(t: int, own: list[Account], contacts: list[Account], near: list[Account]) -> None:
        src = rng.choice(own)
        balance = ledger.balance(src.number)
        kind = pick(rng, cal.NORMAL_KINDS)
        if kind == "p2p":
            others = [a for a in own if a is not src]
            dest = rng.choice(others) if others and rng.random() < 0.15 else rng.choice(contacts)
            amount, channel = kobo(lognormal(rng, *cal.P2P_AMOUNT)), pick(rng, cal.P2P_CHANNELS)
        elif kind == "merchant":
            dest = rng.choice(merchants)
            amount, channel = kobo(lognormal(rng, *cal.MERCHANT_AMOUNT)), pick(rng, cal.MERCHANT_CHANNELS)
        elif kind == "atm":
            dest, amount, channel = None, kobo(rng.choice(ATM_AMOUNTS)), "atm"
        else:
            dest, amount, channel = rng.choice(near), kobo(rng.choice(POS_AMOUNTS)), "pos"
        if amount <= balance:
            ledger.transfer(t, src.number, dest.number if dest else None, amount, channel,
                            device_for(src, channel))

    owners: dict[str, list[Account]] = {}
    for account in people:
        owners.setdefault(account.owner_id, []).append(account)

    for own in owners.values():
        contacts = rng.sample(people, 5)
        near = rng.sample(agents, 2)
        rate = cal.SEGMENTS[own[0].segment]["rate"]
        for day in range(cfg.days):
            for _ in range(poisson(rng, rate)):
                ledger.schedule(time_in_day(rng, day), everyday, own, contacts, near)

    # --------------------------------------------------------------- salaries

    def pay(t: int, payer: Account, payee: Account, amount: int, channel: str) -> None:
        ledger.transfer(t, payer.number, payee.number, amount, channel, device_for(payer, channel))

    payroll: dict[str, int] = {}
    payday_start = {b.number: cfg.payday * DAY + rng.randrange(8 * 3600, 11 * 3600) for b in businesses}
    for own in owners.values():
        if own[0].segment != "salaried":
            continue
        employer = rng.choice(businesses)
        salary = kobo(lognormal(rng, *cal.SALARY_AMOUNT))
        payroll[employer.number] = payroll.get(employer.number, 0) + salary
        ledger.schedule(payday_start[employer.number] + rng.randrange(1800), pay,
                        employer, own[0], salary, "web")
    for business in businesses:
        # Employers hold enough to cover their payroll.
        business.opening_balance += int(payroll.get(business.number, 0) * 1.3)

    # ------------------------------------- look-alike: business paying suppliers

    suppliers = world.in_segment("trader") + businesses
    for business in businesses:
        label(business, "business_batch_payer")
        for _ in range(rng.randint(1, 2)):
            t = rng.randrange(cfg.days) * DAY + rng.randrange(9 * 3600, 16 * 3600)
            for supplier in rng.sample(suppliers, rng.randint(5, 15)):
                t += rng.randint(3, 20)
                if supplier is not business:
                    ledger.schedule(t, pay, business, supplier,
                                    kobo(lognormal(rng, *cal.SUPPLIER_AMOUNT)), "web")

    # ----------------------------------- look-alike: savings-group collector

    def payout(t: int, collector: Account, member: Account, received: dict) -> None:
        amount = min(received["total"], ledger.balance(collector.number))
        ledger.transfer(t, collector.number, member.number, amount, "mobile", collector.device_id)

    def contribute(t: int, member: Account, collector: Account, amount: int, received: dict) -> None:
        if ledger.transfer(t, member.number, collector.number, amount, "mobile", member.device_id):
            received["total"] += amount

    established = [a for a in people if a.segment != "student" and a.opened_t < -365 * DAY]
    for collector in rng.sample(established, max(3, cfg.customers // 500)):
        collector.segment = "collector"
        label(collector, "savings_group_collector")
        members = [m for m in rng.sample(people, rng.randint(10, 25)) if m is not collector]
        contribution = kobo(rng.choice([5_000, 10_000, 20_000, 50_000]))
        start = rng.randrange(cfg.days) * DAY + rng.randrange(7 * 3600, 18 * 3600)
        received = {"total": 0}
        for member in members:
            ledger.schedule(start + rng.randrange(7200), contribute, member, collector, contribution, received)
        ledger.schedule(start + 7200 + rng.randrange(600, 3600), payout, collector, rng.choice(members), received)

    # ------------------------- look-alike: new account with one large inflow

    def fund(t: int, account: Account, amount: int) -> None:
        funders = [a for a in people if a.segment in ("salaried", "trader")
                   and ledger.balance(a.number) >= amount]
        if funders:
            funder = rng.choice(funders)
            ledger.transfer(t, funder.number, account.number, amount, "web", funder.device_id)

    def withdraw(t: int, account: Account) -> None:
        amount = kobo(rng.choice([20_000, 40_000, 50_000, 100_000]))
        if amount <= ledger.balance(account.number):
            ledger.transfer(t, account.number, None, amount, "atm", None)

    for _ in range(max(5, cfg.customers // 250)):
        opened_t = rng.uniform(-10 * DAY, cfg.duration * 0.6)
        account = world.add_account(
            rng, bank_id=rng.choice(world.banks()), owner_id=world.new_owner(), bvn=world.new_bvn(rng),
            phone=world.new_phone(rng), device_id=world.new_device(rng), kyc_tier=rng.choice([1, 2]),
            account_type="personal", opened_t=opened_t, opening_balance=kobo(rng.uniform(0, 5_000)),
            segment="new_account",
        )
        label(account, "new_account_large_inflow")
        funded_t = int(max(opened_t, 0) + rng.uniform(3600, cfg.duration * 0.3))
        amount = kobo(min(max(lognormal(rng, 1_200_000, 0.7), 300_000), 8_000_000))
        ledger.schedule(funded_t, fund, account, amount)
        # The money is then spent slowly, unlike a mule's.
        for _ in range(rng.randint(3, 8)):
            ledger.schedule(funded_t + rng.uniform(4 * 3600, 3 * DAY), withdraw, account)

    return lookalikes
