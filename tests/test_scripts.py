"""Offline tests for the benchmark and scenario runners' bookkeeping (`scripts/`): which games are worth
a replay, the ladder time limits, and what a scenario result is compared with. No StarCraft II needed."""

from scripts.benchmark import LADDER_FRAME_TIME_MS, LADDER_TIMEOUT_S, SLOW_WIN_FACTOR, is_notable, over_ladder_limits
from scripts.run_scenarios import compare


def game(**row):
    return {"result": "Victory", "game_time": 500, "frame_ms": 10.0, "max_step_ms": 150.0, "startup_s": 0.6, **row}


def test_within_the_ladder_limits():
    assert over_ladder_limits(game()) == []
    assert over_ladder_limits({"result": "Crash"}) == []  # not timed: nothing to check


def test_each_ladder_limit():
    assert len(over_ladder_limits(game(frame_ms=LADDER_FRAME_TIME_MS))) == 1
    assert len(over_ladder_limits(game(max_step_ms=1000 * LADDER_TIMEOUT_S))) == 1
    assert len(over_ladder_limits(game(startup_s=LADDER_TIMEOUT_S))) == 1


def test_losses_ties_crashes_and_slow_games_are_notable():
    for result in ("Defeat", "Tie", "Crash"):
        assert is_notable(game(result=result), [])
    assert is_notable(game(frame_ms=LADDER_FRAME_TIME_MS), [])


def test_a_win_is_notable_only_when_much_slower_than_usual():
    history = [500.0] * 5
    assert not is_notable(game(), history)
    assert is_notable(game(game_time=SLOW_WIN_FACTOR * 500 + 1), history)
    assert not is_notable(game(game_time=5000), history[:4])  # too little history to say what's usual


def scenario_row(run_id="now", commit="abc1234", **row):
    return {
        "scenario": "tank_dive",
        "side": "top",
        "seed": "1",
        "sc2_build": "75689",
        "status": "PASS",
        "army_lost": "150",
        "damage_taken": "307",
        "game_time": "6.8",
        "commit": commit,
        "run_id": run_id,
        **row,
    }


def test_compared_with_the_last_earlier_run_of_the_same_game():
    history = [
        scenario_row(run_id="old", commit="1111111", army_lost="300"),
        scenario_row(run_id="earlier", commit="2222222", army_lost="200"),
        scenario_row(run_id="earlier", commit="2222222", seed="2", army_lost="999"),  # another game
        scenario_row(run_id="earlier", commit="2222222", sc2_build="97563", army_lost="999"),  # another build
    ]
    line = compare(scenario_row(), history)
    assert "vs 2222222" in line and "army lost 200 -> 150" in line and "game time 6.8s (same)" in line


def test_the_same_commit_is_compared_but_not_the_same_run():
    assert "vs abc1234" in compare(scenario_row(), [scenario_row(run_id="earlier")])
    assert compare(scenario_row(), [scenario_row(run_id="now")]) == ""


def test_rows_from_before_run_ids_still_compare():
    old = scenario_row(commit="1111111")
    del old["run_id"]
    assert "vs 1111111" in compare(scenario_row(), [old])


def test_random_starts_arent_compared():
    assert compare(scenario_row(seed=""), [scenario_row(run_id="earlier", seed="")]) == ""


def test_a_status_change_is_shown():
    assert "FAIL -> PASS" in compare(scenario_row(), [scenario_row(run_id="earlier", status="FAIL")])
