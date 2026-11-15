"""The trained scorer: load it, score feature rows, turn scores into tiers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np

from .features import FEATURES

DEFAULT_PATH = Path("models/mule_scorer.joblib")


@dataclass
class Scorer:
    model: object
    high: float                 # score at or above which an account is flagged
    medium: float
    info: dict

    def score(self, rows: list[list[float]]) -> np.ndarray:
        if not rows:
            return np.zeros(0)
        return self.model.predict_proba(np.asarray(rows, dtype=float))[:, 1]

    def tier(self, score: float) -> str:
        return "high" if score >= self.high else "medium" if score >= self.medium else "low"

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": self.model, "high": self.high, "medium": self.medium,
                     "features": FEATURES, "info": self.info}, path)


def model_path() -> Path:
    return Path(os.environ.get("TRAILGUARD_MODEL", DEFAULT_PATH))


def load(path: Path | None = None) -> Scorer:
    saved = joblib.load(path or model_path())
    if saved["features"] != FEATURES:
        raise RuntimeError("the saved model was trained on a different feature list; retrain it")
    return Scorer(saved["model"], saved["high"], saved["medium"], saved["info"])


@lru_cache(maxsize=4)
def _cached(path: str, modified: float) -> Scorer:
    return load(Path(path))


def current() -> Scorer | None:
    """The model in use, or None if none has been trained yet."""
    path = model_path()
    if not path.exists():
        return None
    return _cached(str(path), path.stat().st_mtime)
