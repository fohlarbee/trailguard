"""Settings and database access."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA = Path(__file__).parent / "schema.sql"
DEV_DATABASE_URL = "postgresql://trailguard:trailguard@localhost:5433/trailguard"
TABLES = ["agent_runs", "str_drafts", "evidence_packs", "holds", "alerts", "trail_holdings", "trail_hops", "case_transactions", "cases", "transactions", "accounts", "audit_log"]


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEV_DATABASE_URL)


def salt() -> str:
    value = os.environ.get("TRAILGUARD_SALT")
    if not value:
        raise RuntimeError("TRAILGUARD_SALT is not set")
    return value


def api_keys() -> dict[str, str]:
    """Map of API key to institution ID, from TRAILGUARD_API_KEYS=BANK:KEY,BANK:KEY."""
    pairs = [p for p in os.environ.get("TRAILGUARD_API_KEYS", "").split(",") if p.strip()]
    return {key.strip(): bank.strip() for bank, key in (p.split(":", 1) for p in pairs)}


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url(), row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text())
    conn.commit()


def reset(conn: psycopg.Connection) -> None:
    """Empty every table. For tests and for reloading a simulator run."""
    init_schema(conn)
    conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY")
    conn.commit()
