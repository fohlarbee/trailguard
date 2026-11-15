"""Command line: python -m core <init|reset|evaluate|seed-sandbox>."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import db
from .evaluate import evaluate


def main() -> None:
    parser = argparse.ArgumentParser(prog="core")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the tables if they do not exist")
    sub.add_parser("reset", help="empty every table")
    e = sub.add_parser("evaluate", help="grade the tracer against a loaded simulator run")
    e.add_argument("run", type=Path)
    e.add_argument("--cases", action="store_true", help="include the per-case rows")
    sb = sub.add_parser("seed-sandbox", help="fill a fresh service with synthetic activity")
    sb.add_argument("--url", default="http://127.0.0.1:8000")
    sb.add_argument("--data", type=Path, default=Path("data/sandbox"))
    args = parser.parse_args()

    if args.command == "seed-sandbox":
        from .sandbox import seed
        seed(args.url, args.data)
        return

    with db.connect() as conn:
        if args.command == "init":
            db.init_schema(conn)
            print("Schema ready.")
        elif args.command == "reset":
            db.reset(conn)
            print("All tables emptied.")
        else:
            result = evaluate(conn, args.run, db.salt())
            if not args.cases:
                del result["cases"]
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
