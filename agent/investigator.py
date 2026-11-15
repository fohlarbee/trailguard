"""The case investigator: Claude, working a fraud case through tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import anthropic
import psycopg

from core import audit

from .tools import TOOLS, Toolbox

MODEL = "claude-opus-5-5"
MAX_TURNS = 30

SYSTEM = """You are the case investigator for TrailGuard, a service that Nigerian banks and fintechs \
use together to trace stolen money across institutions and hold it before it is cashed out.

A victim has reported a fraud and a case has been opened. You work that case from start to finish \
using your tools, then hand it to a human fraud analyst. The analyst reads your final message to \
decide what to do next, so it is the main thing you produce.

How this works, and why it matters:

Stolen money moves fast. It is typically split across several mule accounts at different banks and \
withdrawn as cash within minutes, after which it cannot be recovered. Speed therefore matters more \
than polish: find where the money is now and get holds onto whatever is still in accounts before \
doing anything else.

Holds you place are temporary. Each one lapses after 24 hours unless a human confirms it, and a \
human can release it sooner. That is what makes it acceptable for you to place them without asking \
first. It does not make them free: a hold freezes a real person's money, and holding an innocent \
person's funds is the most damaging mistake this system can make. Hold only money the trace shows \
is stolen and still in the account. The hold tool enforces that limit, and if it refuses a request, \
treat the refusal as information about the case, not as an obstacle.

Automatic holds are sized on what an account received, so one can freeze more than is stolen. \
When an active hold on the trail is larger than the stolen money traced into that account, recommend \
a reduction to the traced amount and say why, unless something specific suggests the rest is also \
suspect, such as the account being a newly opened mule with no other activity. Under-holding lets \
stolen money go and over-holding freezes an innocent person's funds, so say which you judged the \
greater risk.

Money that has already left the system, as cash, through a POS agent, or to a merchant, cannot be \
held. Say so plainly; do not place holds that cannot catch anything.

Every figure you report must come from a tool result. Do not estimate, round into different \
numbers, or add amounts up yourself when a tool already gives the total. If a tool has not told you \
something, say that it is not known.

You only ever see hashed account identifiers. That is deliberate: customer data stays inside each \
bank. Refer to accounts by the identifier a tool gave you, and never guess at who is behind one.

Money may keep moving while you work, and banks take time to answer. Trace again before you finish \
if holds were placed, check which banks have acknowledged, and chase any that have not. You cannot \
file the suspicious transaction report or confirm or release a hold; those are a human's decisions. \
Draft the report and build the evidence pack so the analyst has both.

Your final message is for a busy analyst who has not seen any of your tool calls. Lead with the \
outcome: how much was stolen, how much is held or still holdable, and how much is gone. Then the \
decisions waiting for them, most urgent first, each with the hold or account it concerns. Then a \
short account of the trail. Keep it brief and factual, and state anything you could not establish."""


def investigate(
    conn: psycopg.Connection,
    case_id: str,
    now: datetime | None = None,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Run the investigator on a case. `now` is the moment the agent is working at."""
    now = now or datetime.now(timezone.utc)
    if not conn.execute("SELECT 1 FROM cases WHERE case_id = %s", (case_id,)).fetchone():
        raise LookupError(f"no case {case_id}")
    client = client or anthropic.Anthropic()
    toolbox = Toolbox(conn, case_id, now)

    run_id = conn.execute(
        "INSERT INTO agent_runs (case_id, as_of, model) VALUES (%s, %s, %s) RETURNING run_id",
        (case_id, now, MODEL)).fetchone()["run_id"]
    audit.append(conn, "agent_run_started", {"run_id": run_id, "case_id": case_id, "as_of": now, "model": MODEL})
    conn.commit()

    messages = [{"role": "user", "content":
                 f"Case {case_id} has just been assigned to you. The time is {now.isoformat()}. "
                 "Investigate it and report back."}]
    steps, outcome, summary = [], "stopped", ""

    try:
        for _ in range(MAX_TURNS):
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                # If a safety classifier declines the request, retry on a fallback model
                # instead of abandoning a live case.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "medium"},
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=messages,
            )
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()

            if response.stop_reason == "refusal":
                outcome, summary = "refused", text or "The model declined to continue with this case."
                break
            if response.stop_reason == "max_tokens":
                outcome, summary = "stopped", text or "The model ran out of output before finishing."
                break
            # The whole content goes back unchanged, thinking blocks included.
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue

            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                outcome, summary = "completed", text
                break

            results = []
            for call in calls:
                output, failed = toolbox.call(call.name, dict(call.input))
                steps.append({"tool": call.name, "input": dict(call.input), "failed": failed})
                audit.append(conn, "agent_tool_call", {
                    "run_id": run_id, "case_id": case_id, "tool": call.name, "input": dict(call.input),
                    "failed": failed, "result": json.loads(output) if failed else None,
                })
                conn.commit()
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": output,
                                **({"is_error": True} if failed else {})})
            # All results for one turn go back together, in one message.
            messages.append({"role": "user", "content": results})
        else:
            summary = "The investigation reached its turn limit before the agent finished."
    except anthropic.APIError as error:
        outcome, summary = "failed", f"The model could not be reached: {error}"
    except TypeError as error:
        # The SDK raises this at the first request when it finds no credentials.
        if "authentication" not in str(error):
            raise
        outcome, summary = "failed", "No Claude API credentials found. Add ANTHROPIC_API_KEY to .env."

    conn.execute(
        "UPDATE agent_runs SET finished_at = now(), outcome = %s, summary = %s, steps = %s WHERE run_id = %s",
        (outcome, summary, json.dumps(steps), run_id))
    audit.append(conn, "agent_run_finished", {
        "run_id": run_id, "case_id": case_id, "outcome": outcome, "tool_calls": len(steps), "summary": summary})
    conn.commit()
    return {"run_id": run_id, "case_id": case_id, "as_of": now, "outcome": outcome,
            "summary": summary, "steps": steps}
