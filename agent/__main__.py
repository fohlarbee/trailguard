"""Command line: run the investigator on a case.

    uv run --env-file .env python -m agent investigate BNK01-RPT000003
    uv run --env-file .env python -m agent investigate BNK01-RPT000003 --minutes-after-report 10

Needs ANTHROPIC_API_KEY in the environment (add it to .env).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

import anthropic

from core import db

from .investigator import investigate


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent")
    sub = parser.add_subparsers(dest="command", required=True)
    i = sub.add_parser("investigate", help="work a case and print the handover")
    i.add_argument("case_id")
    when = i.add_mutually_exclusive_group()
    when.add_argument("--as-of", type=datetime.fromisoformat,
                      help="the moment the agent works at (ISO time with offset); default is now")
    when.add_argument("--minutes-after-report", type=float,
                      help="work at this many minutes after the victim's report, for replayed data")
    args = parser.parse_args()

    with db.connect() as conn:
        case = conn.execute("SELECT reported_at FROM cases WHERE case_id = %s", (args.case_id,)).fetchone()
        if not case:
            sys.exit(f"No case {args.case_id}.")
        now = args.as_of
        if args.minutes_after_report is not None:
            now = case["reported_at"] + timedelta(minutes=args.minutes_after_report)
        try:
            result = investigate(conn, args.case_id, now=now)
        except anthropic.AnthropicError as error:
            sys.exit(f"Could not start the agent: {error}")

    print(f"Run {result['run_id']} on {result['case_id']} as of {result['as_of'].isoformat()}: {result['outcome']}")
    for step in result["steps"]:
        print(f"  {'FAILED ' if step['failed'] else ''}{step['tool']}({step['input'] or ''})")
    print()
    print(result["summary"])


if __name__ == "__main__":
    main()
