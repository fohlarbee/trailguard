"""Run configuration: loaded from TOML, with optional overrides from the CLI."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).parent / "default.toml"


@dataclass
class Config:
    seed: int = 7
    start: datetime = field(default_factory=lambda: datetime.fromisoformat("2026-10-05T00:00:00+01:00"))
    days: int = 7
    banks: int = 4
    fintechs: int = 2
    customers: int = 5000
    payday: int = 3
    fraud_cases: int = 40
    scenario_weights: dict[str, float] = field(default_factory=dict)

    @property
    def duration(self) -> int:
        """Length of the simulated period in seconds."""
        return self.days * 86400


def load_config(path: Path | None = None, **overrides) -> Config:
    """Load the default config, layer a user config on top, then apply overrides.

    Overrides with a value of None are ignored, so CLI flags can be passed straight through.
    """
    raw = tomllib.loads(DEFAULT_CONFIG.read_text())
    if path is not None:
        user = tomllib.loads(Path(path).read_text())
        for section, values in user.items():
            raw.setdefault(section, {}).update(values)

    cfg = Config(
        seed=raw["run"]["seed"],
        start=datetime.fromisoformat(raw["run"]["start"]),
        days=raw["run"]["days"],
        banks=raw["world"]["banks"],
        fintechs=raw["world"]["fintechs"],
        customers=raw["world"]["customers"],
        payday=raw["world"]["payday"],
        fraud_cases=raw["fraud"]["cases"],
        scenario_weights=dict(raw["fraud"]["scenario_weights"]),
    )
    for key, value in overrides.items():
        if value is not None:
            setattr(cfg, key, value)

    if cfg.start.tzinfo is None:
        raise ValueError("run.start must include a UTC offset, e.g. +01:00")
    if cfg.days < 1 or cfg.customers < 200 or cfg.banks + cfg.fintechs < 2 or cfg.fintechs < 1:
        raise ValueError("need days >= 1, customers >= 200, at least one fintech and two institutions")
    cfg.payday = min(cfg.payday, cfg.days - 1)
    return cfg
