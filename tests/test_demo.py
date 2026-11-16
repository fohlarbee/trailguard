"""The live demo: its clock, and the player standing in for the banks."""

from datetime import datetime, timedelta, timezone

import pytest

from agent.tools import Toolbox
from core import clock, db, demo
from tests.conftest import SALT, TEST_DB, require_database


def test_clock_runs_at_the_pace_it_is_given():
    start = datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc)
    try:
        clock.set_pace(10.0, at=start)
        assert clock.rate() == 10.0 and clock.simulated()
        elapsed = clock.now() - start
        assert timedelta(0) <= elapsed < timedelta(seconds=5)
        clock.set_pace(1.0)
        assert clock.rate() == 1.0
    finally:
        clock.clear()


def test_the_investigator_can_follow_a_moving_clock():
    times = iter([datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc), datetime(2026, 10, 5, 22, 5, tzinfo=timezone.utc)])
    toolbox = Toolbox(None, "CASE", datetime(2026, 1, 1, tzinfo=timezone.utc), clock=lambda: next(times))
    assert toolbox.now.minute == 0 and toolbox.now.minute == 5


@pytest.fixture(scope="module")
def played(trained, tmp_path_factory):
    """The demo theft played to its end without the agent, as fast as the database allows."""
    require_database()
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", db.DEV_DATABASE_URL.rsplit("/", 1)[0] + "/" + TEST_DB)
    patch.setenv("TRAILGUARD_MODEL", str(trained["path"]))
    run = demo.build_run(data=tmp_path_factory.mktemp("demo"))
    script = demo.Script.load(run, SALT)
    state = demo.State()
    with db.connect() as conn:
        db.reset(conn)
        player = demo.Player(conn, script, state)
        player.preload()
        opened, now = [], script.start_at
        while now < script.end_at:
            now += timedelta(seconds=5)
            opened += player.advance(now)
        yield {"conn": conn, "player": player, "state": state, "script": script, "opened": opened, "now": now}
    patch.undo()


def test_the_victim_reports_and_a_case_opens(played):
    assert len(played["opened"]) == 1
    assert played["opened"][0].startswith(played["script"].case["victim_bank"])


def test_the_story_moves_fast_only_when_something_is_about_to_happen(played):
    script = played["script"]
    assert script.pace(script.moments[0] - timedelta(seconds=30)) == demo.FAST
    assert script.pace(script.moments[0] - timedelta(minutes=10)) == demo.SKIP


def test_held_money_never_leaves_an_account(played):
    conn, player = played["conn"], played["player"]
    held = player._held(played["now"])
    assert held, "the demo theft should end with money under hold"
    for account, amount in held.items():
        assert player.balance[account] >= amount
    # Every payment the banks refused was out of an account under hold.
    assert all(player._held(b["at"]).get(b["account"]) for b in played["state"].blocked)
    stored = conn.execute("SELECT count(*) AS n FROM transactions").fetchone()["n"]
    assert stored > 0


def test_holds_are_answered_straight_away(played):
    assert played["conn"].execute("SELECT count(*) AS n FROM holds WHERE status = 'requested'").fetchone()["n"] == 0
