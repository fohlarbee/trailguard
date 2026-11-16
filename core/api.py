"""The ingest and case API.

Run locally with:  uv run --env-file .env uvicorn core.api:app --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

import anthropic

from agent.investigator import investigate

from . import audit, clock, db, holds, scoring, tracer, views
from .ingest import IngestError, store_events

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Bring the database up to the current schema. Every statement in it is safe to
    # repeat, so this also upgrades a database created by an earlier version.
    with db.connect() as conn:
        db.init_schema(conn)
    yield


app = FastAPI(title="TrailGuard", version="0.1.0", lifespan=lifespan)


def connection() -> Iterator[psycopg.Connection]:
    with db.connect() as conn:
        yield conn


def institution(x_api_key: str = Header()) -> str:
    """The institution an API key belongs to."""
    bank = db.api_keys().get(x_api_key)
    if not bank:
        raise HTTPException(401, "unknown API key")
    return bank


class EventBatch(BaseModel):
    events: list[dict]


@app.post("/v1/events")
def post_events(batch: EventBatch, bank: str = Depends(institution),
                conn: psycopg.Connection = Depends(connection)) -> dict:
    try:
        return store_events(conn, bank, batch.events)
    except IngestError as error:
        raise HTTPException(422, str(error)) from error


@app.get("/v1/me")
def me(bank: str = Depends(institution)) -> dict:
    return {"bank": bank, "now": clock.now(), "simulated_clock": clock.simulated()}


@app.get("/v1/cases")
def list_cases(bank: str = Depends(institution), conn: psycopg.Connection = Depends(connection)) -> list[dict]:
    """Cases reported so far, newest first, with current figures."""
    return views.case_list(conn, clock.now())


@app.get("/v1/cases/{case_id}")
def get_case(case_id: str, bank: str = Depends(institution),
             conn: psycopg.Connection = Depends(connection)) -> dict:
    """The case as it stands now: trail, accounts, holds, agent runs and paperwork."""
    try:
        return views.case_detail(conn, case_id, clock.now(), bank)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@app.post("/v1/cases/{case_id}/trace")
def trace_case(case_id: str, as_of: datetime | None = None, bank: str = Depends(institution),
               conn: psycopg.Connection = Depends(connection)) -> dict:
    """Trace the case with everything known so far (or up to as_of) and store the result."""
    try:
        trail = tracer.trace(conn, case_id, as_of=as_of or clock.now())
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    tracer.save(conn, trail)
    return trail.to_dict()


@app.get("/v1/alerts")
def list_alerts(bank: str = Depends(institution), conn: psycopg.Connection = Depends(connection)) -> list[dict]:
    """Accounts flagged by the mule scorer, most recent first."""
    return conn.execute("SELECT * FROM alerts WHERE first_flagged_at <= %s ORDER BY first_flagged_at DESC",
                        (clock.now(),)).fetchall()


@app.get("/v1/accounts/{account}/score")
def get_score(account: str, as_of: datetime | None = None, bank: str = Depends(institution),
              conn: psycopg.Connection = Depends(connection)) -> dict:
    try:
        return scoring.score_account(conn, account, as_of=as_of or clock.now())
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error


class Acknowledgement(BaseModel):
    held_kobo: int
    at: datetime | None = None


class Review(BaseModel):
    decision: str
    reviewer: str
    at: datetime | None = None


def _now(at: datetime | None) -> datetime:
    return at or clock.now()


@app.get("/v1/holds")
def list_holds(status: str | None = None, unanswered: bool = False, bank: str = Depends(institution),
               conn: psycopg.Connection = Depends(connection)) -> list[dict]:
    """Holds on the calling institution's own accounts, as they stand now.

    unanswered=true returns every hold request the institution has not yet answered,
    whatever the clock says. A connector uses it to pick up its work.
    """
    if unanswered:
        return holds.listing(conn, bank_id=bank, status="requested")
    return holds.listing(conn, bank_id=bank, status=status, now=clock.now())


@app.post("/v1/holds/{hold_id}/ack")
def acknowledge_hold(hold_id: int, body: Acknowledgement, bank: str = Depends(institution),
                     conn: psycopg.Connection = Depends(connection)) -> dict:
    """The account's bank reports how much it actually held."""
    try:
        return holds.acknowledge(conn, hold_id, bank, body.held_kobo, _now(body.at))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except holds.HoldError as error:
        raise HTTPException(422, str(error)) from error


@app.post("/v1/holds/{hold_id}/review")
def review_hold(hold_id: int, body: Review, bank: str = Depends(institution),
                conn: psycopg.Connection = Depends(connection)) -> dict:
    """A person at the account's bank confirms or releases a hold."""
    try:
        if conn.execute("SELECT bank_id FROM holds WHERE hold_id = %s", (hold_id,)).fetchone() not in (
                None, {"bank_id": bank}):
            raise holds.HoldError("only the account's own bank can review a hold")
        return holds.review(conn, hold_id, body.decision, body.reviewer, _now(body.at))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except holds.HoldError as error:
        raise HTTPException(422, str(error)) from error


class Reduction(BaseModel):
    reviewer: str
    at: datetime | None = None


@app.post("/v1/holds/{hold_id}/reduce")
def reduce_hold(hold_id: int, body: Reduction, bank: str = Depends(institution),
                conn: psycopg.Connection = Depends(connection)) -> dict:
    """A person at the account's bank accepts the investigator's recommended reduction."""
    try:
        if conn.execute("SELECT bank_id FROM holds WHERE hold_id = %s", (hold_id,)).fetchone() not in (
                None, {"bank_id": bank}):
            raise holds.HoldError("only the account's own bank can reduce a hold")
        return holds.reduce(conn, hold_id, body.reviewer, _now(body.at))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except holds.HoldError as error:
        raise HTTPException(422, str(error)) from error


@app.post("/v1/cases/{case_id}/investigate")
def investigate_case(case_id: str, as_of: datetime | None = None, bank: str = Depends(institution),
                     conn: psycopg.Connection = Depends(connection)) -> dict:
    """Have the agent work the case. Takes as long as the investigation does."""
    try:
        return investigate(conn, case_id, now=as_of or clock.now())
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except anthropic.AnthropicError as error:
        raise HTTPException(503, f"the agent could not start: {error}") from error


@app.get("/v1/cases/{case_id}/evidence")
def latest_evidence(case_id: str, bank: str = Depends(institution),
                    conn: psycopg.Connection = Depends(connection)) -> dict:
    pack = conn.execute(
        "SELECT * FROM evidence_packs WHERE case_id = %s ORDER BY pack_id DESC LIMIT 1", (case_id,)).fetchone()
    if not pack:
        raise HTTPException(404, "no evidence pack has been built for this case")
    return pack


@app.get("/v1/audit/verify")
def verify_audit(bank: str = Depends(institution), conn: psycopg.Connection = Depends(connection)) -> dict:
    return audit.verify(conn)
