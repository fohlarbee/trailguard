"""Generate one run: build the world, queue everything, execute, write the files."""

from __future__ import annotations

import random
from pathlib import Path

from .config import Config
from .fraud import FraudEngine
from .ledger import Ledger
from .normal import schedule_normal
from .output import write_run
from .world import build_world


def generate(cfg: Config, out: Path) -> dict:
    """Run the simulation and write its output folder. Returns the summary."""
    rng = random.Random(cfg.seed)
    world = build_world(cfg, rng)
    ledger = Ledger(world, cfg.start)

    lookalikes = schedule_normal(cfg, world, ledger, rng)
    engine = FraudEngine(cfg, world, ledger, rng)

    # Accounts that predate the run are usable from the start; the rest open on time.
    for account in world.accounts.values():
        if account.opened_t < 0:
            ledger.open_account(account)
        else:
            ledger.schedule(account.opened_t, lambda t, a=account: ledger.open_account(a))

    ledger.run()
    return write_run(Path(out), cfg, world, ledger, engine.ground_truth(lookalikes))
