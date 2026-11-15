"""The connector a bank runs inside its own network.

It does three things:

  1. Reads the bank's events (from a file the bank appends to, or from a local
     HTTP endpoint the bank posts to), hashes every identifier, and sends them to
     TrailGuard. Raw identifiers never leave the bank.
  2. Collects hold requests for the bank's accounts, turns the hashed account back
     into the real account number using a lookup it keeps locally, and hands the
     request to the bank's fraud desk (a webhook or a file).
  3. Passes the bank's answer (how much it held) back to TrailGuard.

All connections to TrailGuard are outbound. The connector needs no write access
to any bank system.
"""

from __future__ import annotations

import json
import sqlite3
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import BaseModel

from .hashing import hash_event, hash_value

# Event fields holding an account at this bank, and the field naming that account's bank.
_OWN_ACCOUNTS = (("account_number", "bank_id"), ("sender_account", "sender_bank"),
                 ("receiver_account", "receiver_bank"), ("victim_account", "reporting_bank"))


@dataclass
class Config:
    bank_id: str
    url: str
    api_key: str
    salt: str
    mode: str = "live"                    # "live", or "shadow": score and log, deliver no holds
    source: str = "file"                  # "file" or "http"
    source_path: Path | None = None
    holds_deliver: str = "file"           # "file" or "webhook"
    holds_path: Path | None = None
    holds_webhook: str | None = None
    poll_seconds: float = 10.0
    batch: int = 500
    listen: str = "127.0.0.1:8088"
    state: Path = Path("connector-state.db")


def load_config(path: Path, env: dict[str, str]) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    tg, source, deliver = raw["trailguard"], raw.get("source", {}), raw.get("holds", {})
    local = raw.get("local", {})

    def secret(name: str) -> str:
        variable = tg.get(f"{name}_env", f"TRAILGUARD_{name.upper()}")
        if not env.get(variable):
            raise ValueError(f"environment variable {variable} is not set")
        return env[variable]

    config = Config(
        bank_id=raw["bank"]["id"], url=tg["url"], api_key=secret("api_key"), salt=secret("salt"),
        mode=raw.get("mode", "live"),
        source=source.get("type", "file"),
        source_path=Path(source["path"]) if "path" in source else None,
        holds_deliver=deliver.get("deliver", "file"),
        holds_path=Path(deliver["path"]) if "path" in deliver else None,
        holds_webhook=deliver.get("webhook_url"),
        poll_seconds=deliver.get("poll_seconds", 10.0),
        batch=source.get("batch", 500),
        listen=local.get("listen", "127.0.0.1:8088"),
        state=Path(local.get("state", "connector-state.db")),
    )
    if config.mode not in ("live", "shadow"):
        raise ValueError('mode must be "live" or "shadow"')
    if config.source == "file" and not config.source_path:
        raise ValueError("[source] type = \"file\" needs a path")
    if config.holds_deliver == "file" and not config.holds_path:
        raise ValueError("[holds] deliver = \"file\" needs a path")
    if config.holds_deliver == "webhook" and not config.holds_webhook:
        raise ValueError("[holds] deliver = \"webhook\" needs a webhook_url")
    return config


class Rejected(Exception):
    """TrailGuard answered, and refused the request."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"TrailGuard answered {status}: {detail}")
        self.status, self.detail = status, detail


def _check(response) -> None:
    if response.status_code >= 400:
        raise Rejected(response.status_code, response.text)


class Connector:
    def __init__(self, config: Config, client: httpx.Client | None = None):
        self.config = config
        self.client = client or httpx.Client(base_url=config.url, timeout=60)
        self.headers = {"X-API-Key": config.api_key}
        self.stats = {"events_sent": 0, "holds_delivered": 0, "holds_answered": 0, "last_error": None}
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (hash TEXT PRIMARY KEY, number TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS progress (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS holds (hold_id INTEGER PRIMARY KEY, delivered_at TEXT NOT NULL);
            """)

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.config.state)

    # ----------------------------------------------------------------- events

    def send(self, events: list[dict]) -> dict:
        """Hash and send events. Raises if TrailGuard does not accept them."""
        if not events:
            return {"received": 0}
        salt, bank = self.config.salt, self.config.bank_id
        response = self.client.post("/v1/events", json={"events": [hash_event(e, salt) for e in events]},
                                    headers=self.headers)
        _check(response)
        # Remember which hash belongs to which of this bank's accounts. This lookup
        # stays inside the bank and is what lets a hold request name a real account.
        own = {(hash_value(salt, "account", e[field]), e[field])
               for e in events for field, bank_field in _OWN_ACCOUNTS
               if e.get(field) and e.get(bank_field) == bank}
        with self._db() as db:
            db.executemany("INSERT OR IGNORE INTO accounts VALUES (?, ?)", sorted(own))
        self.stats["events_sent"] += len(events)
        return response.json()

    def pump_file(self) -> int:
        """Send any new lines in the source file. Returns how many events were sent.

        Progress is saved only after TrailGuard accepts a batch, so a crash or an
        outage resends rather than loses events. Resending is harmless.
        """
        path = self.config.source_path
        with self._db() as db:
            row = db.execute("SELECT value FROM progress WHERE name = 'offset'").fetchone()
        offset, sent = (row[0] if row else 0), 0
        with path.open("rb") as source:
            source.seek(offset)
            while True:
                batch, end = [], offset
                while len(batch) < self.config.batch:
                    line = source.readline()
                    if not line.endswith(b"\n"):
                        break                      # nothing more, or a line still being written
                    end += len(line)
                    if line.strip():
                        batch.append(json.loads(line))
                if end == offset:
                    return sent
                self.send(batch)
                offset, sent = end, sent + len(batch)
                with self._db() as db:
                    db.execute("INSERT OR REPLACE INTO progress VALUES ('offset', ?)", (offset,))

    # ------------------------------------------------------------------ holds

    def poll_holds(self) -> int:
        """Hand any new hold requests to the bank. Returns how many were delivered."""
        response = self.client.get("/v1/holds", params={"unanswered": "true"}, headers=self.headers)
        _check(response)
        delivered = 0
        for hold in response.json():
            with self._db() as db:
                seen = db.execute("SELECT 1 FROM holds WHERE hold_id = ?", (hold["hold_id"],)).fetchone()
                number = db.execute("SELECT number FROM accounts WHERE hash = ?", (hold["account"],)).fetchone()
            if seen or not number:
                continue
            request = {
                "hold_id": hold["hold_id"], "account_number": number[0],
                "amount_kobo": hold["requested_kobo"], "reason": hold["reason"],
                "placed_at": hold["placed_at"], "expires_at": hold["expires_at"],
                "shadow": self.config.mode == "shadow",
            }
            if self.config.mode == "shadow":
                # Shadow mode: record what would have been held, and act on nothing.
                self._append(self.config.state.with_suffix(".shadow.jsonl"), request)
            elif self.config.holds_deliver == "webhook":
                httpx.post(self.config.holds_webhook, json=request, timeout=30).raise_for_status()
            else:
                self._append(self.config.holds_path, request)
            with self._db() as db:
                db.execute("INSERT INTO holds VALUES (?, ?)",
                           (hold["hold_id"], datetime.now(timezone.utc).isoformat()))
            delivered += 1
        self.stats["holds_delivered"] += delivered
        return delivered

    def answer(self, hold_id: int, held_kobo: int) -> dict:
        """Tell TrailGuard how much the bank held for a hold request."""
        response = self.client.post(f"/v1/holds/{hold_id}/ack", json={"held_kobo": held_kobo},
                                    headers=self.headers)
        _check(response)
        self.stats["holds_answered"] += 1
        return response.json()

    @staticmethod
    def _append(path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(record) + "\n")

    # ------------------------------------------------------------------- loop

    def run_forever(self, stop=lambda: False) -> None:
        """Keep sending events and collecting holds. Backs off while TrailGuard is unreachable."""
        delay = 1.0
        while not stop():
            try:
                if self.config.source == "file":
                    self.pump_file()
                self.poll_holds()
                self.stats["last_error"], delay = None, 1.0
                time.sleep(self.config.poll_seconds)
            except (Rejected, httpx.HTTPError, OSError) as error:
                self.stats["last_error"] = str(error)
                time.sleep(delay)
                delay = min(delay * 2, 60.0)


class Events(BaseModel):
    events: list[dict]


class Answer(BaseModel):
    held_kobo: int


def local_app(connector: Connector):
    """The connector's local endpoint, for the bank's own systems only."""
    import threading
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, HTTPException

    stopping = threading.Event()

    @asynccontextmanager
    async def lifespan(app):
        worker = threading.Thread(target=connector.run_forever, args=(stopping.is_set,), daemon=True)
        worker.start()
        yield
        stopping.set()

    app = FastAPI(title="TrailGuard connector", lifespan=lifespan)

    def forward(call):
        try:
            return call()
        except Rejected as error:
            raise HTTPException(error.status, error.detail) from error
        except httpx.HTTPError as error:
            # 503 tells the bank's system to keep the events and try again.
            raise HTTPException(503, f"TrailGuard is not reachable: {error}") from error

    @app.get("/health")
    def health() -> dict:
        return {"bank": connector.config.bank_id, "mode": connector.config.mode, **connector.stats}

    @app.post("/events")
    def events(body: Events) -> dict:
        return forward(lambda: connector.send(body.events))

    @app.post("/holds/{hold_id}/answer")
    def answer(hold_id: int, body: Answer) -> dict:
        return forward(lambda: connector.answer(hold_id, body.held_kobo))

    return app
