"""Features that describe how an account is behaving at one moment.

A FeatureStream is fed transactions in time order. After each one it describes the
state of the accounts involved, using only what has happened up to that moment.
Training and live scoring both use this class, so a score means the same thing in
both, and nothing from the future can leak into a feature.

It works on any identifiers, raw or hashed: only equality between them matters.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

HOUR = timedelta(hours=1)
DAY = timedelta(hours=24)
ODD_HOURS = {22, 23, 0, 1, 2, 3, 4, 5}
CHANNELS = {"mobile": 0, "web": 1, "ussd": 2, "pos": 3, "atm": 4}
NAN = math.nan

FEATURES = [
    "account_age_days",        # how long the account has existed
    "kyc_tier",
    "device_shared",           # other accounts registered to the same device
    "bvn_accounts",            # accounts held under the same BVN
    "is_inflow",               # 1 if this event is money arriving, 0 if leaving
    "amount_naira",
    "prior_tx_count",          # transactions before this one, ever
    "amount_vs_prior_max_in",  # this amount relative to the largest earlier inflow
    "in_1h_count", "in_1h_naira", "out_1h_count", "out_1h_naira",
    "in_24h_count", "in_24h_naira", "out_24h_count", "out_24h_naira",
    "passthrough_1h",          # money out in the last hour as a share of money in
    "secs_since_last_inflow",
    "out_counterparties_1h",
    "in_counterparties_24h",
    "cashout_share_1h",        # share of the last hour's outflow that went to cash or POS
    "hour",
    "odd_hour",
    "cross_bank",
    "device_mismatch",         # the session's device is not the sender's registered device
    "channel",
]

I = {name: position for position, name in enumerate(FEATURES)}


@dataclass
class _State:
    recent: deque = field(default_factory=deque)   # (ts, inflow?, naira, counterparty, cash?)
    n_in: int = 0
    n_out: int = 0
    max_in: float = 0.0
    last_in: datetime | None = None


class FeatureStream:
    def __init__(
        self,
        accounts: dict[str, dict],
        track: set[str] | None = None,
        seed: dict[str, dict] | None = None,
        sharing: tuple[dict, dict] | None = None,
    ):
        """accounts: account -> {opened_at, kyc_tier, device, bvn, account_type}.

        track: accounts to describe (default: every personal account given).
        seed: lifetime totals for activity before the stream starts, as
              account -> {n_in, n_out, max_in, last_in}.
        sharing: (device -> sorted opening times, bvn -> sorted opening times), if
              `accounts` does not contain every account sharing a device or BVN.
        """
        self.accounts = accounts
        self.track = track if track is not None else {
            a for a, info in accounts.items() if info["account_type"] == "personal"}
        if sharing:
            self.by_device, self.by_bvn = sharing
        else:
            self.by_device, self.by_bvn = {}, {}
            for info in accounts.values():
                self.by_device.setdefault(info["device"], []).append(info["opened_at"])
                self.by_bvn.setdefault(info["bvn"], []).append(info["opened_at"])
            for times in (*self.by_device.values(), *self.by_bvn.values()):
                times.sort()
        self.states: dict[str, _State] = {}
        for account, totals in (seed or {}).items():
            self.states[account] = _State(n_in=totals["n_in"], n_out=totals["n_out"],
                                          max_in=totals["max_in"], last_in=totals["last_in"])

    def apply(self, tx: dict) -> list[tuple[str, list[float]]]:
        """Record a transaction and describe each tracked account it touched.

        tx needs: ts (datetime), sender_account, receiver_account, sender_bank,
        receiver_bank, amount_kobo, channel, device.
        """
        ts, naira = tx["ts"], tx["amount_kobo"] / 100
        sender, receiver = tx["sender_account"], tx["receiver_account"]
        cash = receiver is None or tx["channel"] == "pos"
        registered = self.accounts.get(sender, {}).get("device")
        mismatch = NAN if tx["device"] is None or registered is None else float(tx["device"] != registered)
        cross_bank = float(receiver is not None and tx["receiver_bank"] != tx["sender_bank"])

        out = []
        for account, inflow, other in ((sender, False, receiver), (receiver, True, sender)):
            if account is None or account not in self.track:
                continue
            state = self.states.setdefault(account, _State())
            row = self._describe(account, state, ts, naira, inflow, other, tx["channel"], cross_bank, mismatch)
            # The event itself counts towards the windows it falls in.
            state.recent.append((ts, inflow, naira, other, cash))
            if inflow:
                row[I["in_1h_count"]] += 1
                row[I["in_1h_naira"]] += naira
                row[I["in_24h_count"]] += 1
                row[I["in_24h_naira"]] += naira
                state.n_in += 1
                state.max_in = max(state.max_in, naira)
                state.last_in = ts
            else:
                self._add_outflow(row, state, ts, naira)
                state.n_out += 1
            out.append((account, row))
        return out

    def _describe(self, account, state, ts, naira, inflow, other, channel, cross_bank, mismatch) -> list[float]:
        info = self.accounts[account]
        while state.recent and ts - state.recent[0][0] > DAY:
            state.recent.popleft()

        in_1h = in_1h_n = out_1h = out_1h_n = in_24h = in_24h_n = out_24h = out_24h_n = 0.0
        in_cp = {other} if inflow else set()
        for when, was_inflow, amount, other, _ in state.recent:
            recent = ts - when <= HOUR
            if was_inflow:
                in_24h += amount
                in_24h_n += 1
                in_cp.add(other)
                if recent:
                    in_1h += amount
                    in_1h_n += 1
            else:
                out_24h += amount
                out_24h_n += 1
                if recent:
                    out_1h += amount
                    out_1h_n += 1

        return [
            (ts - info["opened_at"]).total_seconds() / 86400,
            float(info["kyc_tier"]),
            float(bisect_right(self.by_device.get(info["device"], []), ts) - 1),
            float(bisect_right(self.by_bvn.get(info["bvn"], []), ts)),
            float(inflow),
            naira,
            float(state.n_in + state.n_out),
            naira / state.max_in if inflow and state.max_in > 0 else NAN,
            in_1h_n, in_1h, out_1h_n, out_1h,
            in_24h_n, in_24h, out_24h_n, out_24h,
            out_1h / in_1h if in_1h > 0 else NAN,
            (ts - state.last_in).total_seconds() if state.last_in else NAN,
            NAN,                      # out_counterparties_1h, filled in below
            float(len(in_cp)),
            NAN,                      # cashout_share_1h, filled in below
            float(ts.hour),
            float(ts.hour in ODD_HOURS),
            cross_bank,
            mismatch,
            float(CHANNELS[channel]),
        ]

    def _add_outflow(self, row: list[float], state: _State, ts: datetime, naira: float) -> None:
        """Finish the outflow-side features once this outflow is in the window."""
        row[I["out_1h_count"]] += 1
        row[I["out_1h_naira"]] += naira
        row[I["out_24h_count"]] += 1
        row[I["out_24h_naira"]] += naira
        in_1h = row[I["in_1h_naira"]]
        out_1h = row[I["out_1h_naira"]]
        row[I["passthrough_1h"]] = out_1h / in_1h if in_1h > 0 else NAN
        others, cash_out = set(), 0.0
        for when, was_inflow, amount, other, cash in state.recent:
            if not was_inflow and ts - when <= HOUR:
                others.add(other)
                cash_out += amount if cash else 0.0
        row[I["out_counterparties_1h"]] = float(len(others))
        row[I["cashout_share_1h"]] = cash_out / out_1h if out_1h > 0 else NAN


def reasons(row: list[float]) -> list[str]:
    """Plain-language observations about a feature row, for analysts and the agent."""
    f = dict(zip(FEATURES, row))
    found = []
    if f["account_age_days"] < 30:
        found.append(f"account opened {f['account_age_days']:.0f} days ago")
    if f["device_shared"] >= 1:
        found.append(f"device registered to {f['device_shared']:.0f} other account(s)")
    if f["bvn_accounts"] >= 2:
        found.append(f"{f['bvn_accounts']:.0f} accounts under the same BVN")
    if f["kyc_tier"] <= 1:
        found.append("lowest KYC tier")
    if not math.isnan(f["passthrough_1h"]) and f["passthrough_1h"] >= 0.7 and f["in_1h_naira"] >= 20_000:
        found.append(f"moved out {min(f['passthrough_1h'], 1):.0%} of N{f['in_1h_naira']:,.0f} received in the last hour")
    if not f["is_inflow"] and not math.isnan(f["secs_since_last_inflow"]) and f["secs_since_last_inflow"] <= 900:
        found.append(f"paid out {f['secs_since_last_inflow'] / 60:.0f} minutes after receiving money")
    if f["out_counterparties_1h"] >= 3:
        found.append(f"paid {f['out_counterparties_1h']:.0f} different accounts within an hour")
    if not math.isnan(f["cashout_share_1h"]) and f["cashout_share_1h"] >= 0.7 and f["out_1h_naira"] >= 20_000:
        found.append("most of the last hour's outflow went to cash or a POS agent")
    if not math.isnan(f["amount_vs_prior_max_in"]) and f["amount_vs_prior_max_in"] >= 5:
        found.append(f"inflow {f['amount_vs_prior_max_in']:.0f}x larger than any before")
    if f["is_inflow"] and f["prior_tx_count"] == 0:
        found.append("first transaction on the account")
    if f["device_mismatch"] == 1:
        found.append("sent from a device not registered to the sender")
    if f["odd_hour"]:
        found.append("activity between 22:00 and 06:00")
    return found
