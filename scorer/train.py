"""Train the mule scorer on simulator runs."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from .data import Rows, build_rows
from .features import FEATURES
from .model import Scorer

# The flagging threshold is the lowest score at which, on a run the model has not
# seen, no more than this share of legitimate accounts would ever be flagged.
MAX_FALSE_FLAG_RATE = 0.001


def fit(runs: list[Path], drop: tuple[str, ...] = ()) -> HistGradientBoostingClassifier:
    """Train on one or more runs. `drop` blanks out features, for ablation tests."""
    X, y = [], []
    for run in runs:
        rows = build_rows(run)
        X.extend(rows.X)
        y.extend(rows.y)
    X = _blank(np.asarray(X, dtype=float), drop)
    model = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08, random_state=0)
    model.fit(X, np.asarray(y))
    return model


def _blank(X: np.ndarray, drop: tuple[str, ...]) -> np.ndarray:
    for name in drop:
        X[:, FEATURES.index(name)] = 0.0
    return X


def score_rows(model, rows: Rows, drop: tuple[str, ...] = ()) -> np.ndarray:
    return model.predict_proba(_blank(np.asarray(rows.X, dtype=float), drop))[:, 1]


def pick_threshold(scores: np.ndarray, rows: Rows) -> float:
    """Lowest threshold that keeps false flags on legitimate accounts within the limit."""
    mule_accounts = {a for a, label in zip(rows.accounts, rows.y) if label}
    worst: dict[str, float] = {}
    for account, score in zip(rows.accounts, scores):
        if account not in mule_accounts:
            worst[account] = max(worst.get(account, 0.0), float(score))
    ranked = sorted(worst.values(), reverse=True)
    allowed = int(len(ranked) * MAX_FALSE_FLAG_RATE)
    # Flag strictly above the highest legitimate score we are not prepared to flag.
    return min(max(ranked[allowed] + 1e-6, 0.5), 0.999)


def train(train_runs: list[Path], validation_run: Path) -> Scorer:
    model = fit(train_runs)
    validation = build_rows(validation_run)
    high = pick_threshold(score_rows(model, validation), validation)
    return Scorer(model, high=high, medium=max(high / 4, 0.05), info={
        "trained_on": [str(r) for r in train_runs],
        "validated_on": str(validation_run),
        "max_false_flag_rate": MAX_FALSE_FLAG_RATE,
    })
