"""Command line: python -m scorer <train|evaluate>.

    uv run python -m scorer train --train data/train-1 data/train-2 data/train-3 --validate data/train-4
    uv run python -m scorer evaluate data/run-7
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import model as model_store
from .evaluate import evaluate
from .train import train


def main() -> None:
    parser = argparse.ArgumentParser(prog="scorer")
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("train", help="train on simulator runs and save the model")
    t.add_argument("--train", nargs="+", type=Path, required=True, help="runs to learn from")
    t.add_argument("--validate", type=Path, required=True, help="a separate run used to set the threshold")
    t.add_argument("--out", type=Path, default=None)
    e = sub.add_parser("evaluate", help="grade the saved model on a run it has not seen")
    e.add_argument("run", type=Path)
    args = parser.parse_args()

    if args.command == "train":
        scorer = train(args.train, args.validate)
        path = args.out or model_store.model_path()
        scorer.save(path)
        print(f"Saved {path} (flag at score >= {scorer.high:.3f})")
    else:
        scorer = model_store.load()
        seen = set(scorer.info["trained_on"]) | {scorer.info["validated_on"]}
        if str(args.run) in seen:
            print(f"Warning: {args.run} was used to train or tune this model; the result will flatter it.")
        result = evaluate(args.run, lambda rows: scorer.score(rows.X), scorer.high)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
