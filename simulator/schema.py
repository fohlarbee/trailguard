"""The event contract.

These are the records a bank sends to TrailGuard. The simulator writes them, and
ingest and the connector will read them, so this file is the single definition.

Identifiers here are raw, as a bank holds them. Hashing happens in the connector.
Amounts are integers in kobo (1 naira = 100 kobo) to avoid rounding errors.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Channel = Literal["mobile", "web", "ussd", "pos", "atm"]
AccountType = Literal["personal", "business", "pos_agent"]


class AccountOpened(BaseModel):
    type: Literal["account_opened"] = "account_opened"
    account_number: str
    bank_id: str
    bvn: str
    phone: str
    device_id: str
    kyc_tier: int
    account_type: AccountType
    opened_at: str


class Transaction(BaseModel):
    type: Literal["transaction"] = "transaction"
    transaction_id: str
    session_ref: str
    timestamp: str
    sender_bank: str
    sender_account: str
    # Both receiver fields are None for a cash withdrawal at an ATM.
    receiver_bank: str | None
    receiver_account: str | None
    amount_kobo: int
    channel: Channel
    device_id: str | None


class FraudReport(BaseModel):
    type: Literal["fraud_report"] = "fraud_report"
    report_id: str
    reported_at: str
    reporting_bank: str
    victim_account: str
    transaction_ids: list[str]


Event = AccountOpened | Transaction | FraudReport


def event_time(event: dict) -> str:
    """The timestamp of an event record, whichever type it is."""
    return event.get("timestamp") or event.get("reported_at") or event["opened_at"]


def event_banks(event: dict) -> list[str]:
    """The institutions that can see an event: each bank sees only its own side."""
    if event["type"] == "transaction":
        banks = [event["sender_bank"]]
        if event["receiver_bank"] and event["receiver_bank"] != event["sender_bank"]:
            banks.append(event["receiver_bank"])
        return banks
    if event["type"] == "fraud_report":
        return [event["reporting_bank"]]
    return [event["bank_id"]]
