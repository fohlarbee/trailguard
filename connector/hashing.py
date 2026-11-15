"""Hash identifiers before anything leaves the bank.

Every connector uses the same network salt, so the same account, BVN, phone or
device hashes to the same value at every bank. That is what lets TrailGuard link
one bank's hop to the next without seeing the real identifier.

Note: these identifiers are short numbers. Anyone holding the salt could hash
every possible value and reverse them, so this is pseudonymisation, not
anonymisation. The salt must be protected like a key.
"""

from __future__ import annotations

import hashlib
import hmac

# Event field -> kind of identifier. The kind is mixed into the hash so that, say,
# a phone number and a BVN with the same digits do not produce the same value.
FIELDS = {
    "account_number": "account",
    "sender_account": "account",
    "receiver_account": "account",
    "victim_account": "account",
    "bvn": "bvn",
    "phone": "phone",
    "device_id": "device",
}


def hash_value(salt: str, kind: str, value: str) -> str:
    digest = hmac.new(salt.encode(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()
    return digest[:32]


def hash_event(event: dict, salt: str) -> dict:
    """A copy of the event with every identifier replaced by its hash."""
    return {
        key: hash_value(salt, FIELDS[key], value) if key in FIELDS and value is not None else value
        for key, value in event.items()
    }
