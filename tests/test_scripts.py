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


def test_step_times_kept_as_count_total_and_maximum():
    from scripts.benchmark import BenchmarkZasz

    bot = BenchmarkZasz()
    assert bot.step_stats(100) == {}
    samples = [0.004, 0.012, 0.002, 0.030]
    for seconds in samples:
        bot.add_step_time(seconds)
    stats = bot.step_stats(game_loop=8)
    assert stats["frame_ms"] == round(1000 * sum(samples) / 8, 2)
    assert stats["avg_step_ms"] == round(1000 * sum(samples) / len(samples), 2)
    assert stats["max_step_ms"] == 30.0


def test_scenario_registry_holds_classes_and_sets_nothing_up(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    from scenarios import ALL
    from scenarios.decisions import CustomDecisions

    assert all(isinstance(s, type) for s in ALL)
    assert len({s.name for s in ALL}) == len(ALL)
    CustomDecisions()  # building it doesn't make its recording folder yet
    assert list(tmp_path.iterdir()) == []


def test_ladder_zip_keeps_only_the_ladders_binaries():
    from scripts.create_ladder_zip import wanted

    assert wanted("ares-sc2/sc2_helper/sc2_helper.cpython-312-x86_64-linux-gnu.so")
    assert wanted("map_analyzer/cext/mapanalyzerext.so")  # untagged: loads on any version
    assert wanted("bot/army.py")
    for other in (
        "ares-sc2/sc2_helper/sc2_helper.cpython-311-x86_64-linux-gnu.so",
        "ares-sc2/sc2_helper/sc2_helper.cpython-313-x86_64-linux-gnu.so",
        "ares-sc2/sc2_helper/sc2_helper.cp312-win_amd64.pyd",
        "ares-sc2/sc2_helper/sc2_helper.cpython-312-darwin.so",
        "ares-sc2/sc2_helper/test.py",
        "cython_extensions/geometry.pyx",
    ):
        assert not wanted(other), other


def test_ladder_zip_picks_the_linux_x86_64_312_wheel():
    import pytest

    from scripts.create_ladder_zip import pick_wheel

    def wheel(tags: str) -> dict:
        return {"filename": f"cython_extensions_sc2-0.18.0-{tags}.whl"}

    arm = wheel("cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64")
    x86 = wheel("cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64")
    other_python = wheel("cp313-cp313-manylinux2014_x86_64")
    assert pick_wheel([arm, other_python, x86]) is x86
    with pytest.raises(SystemExit, match="no cp312"):
        pick_wheel([arm, other_python])


def test_first_differing_order():
    from scripts.compare_orders import first_difference

    assert first_difference(["a", "b"], ["a", "b"]) is None
    assert first_difference(["a", "b"], ["a", "c"]) == 1
    assert first_difference(["a", "b"], ["a"]) == 1  # one log ended early


def order_run(path, logs: dict[str, list[str]], complete: bool = True, end: bool = True):
    """An order-log directory as run_scenarios writes it: one log per label, and a manifest."""
    import json

    path.mkdir()
    for label, lines in logs.items():
        body = lines + ([f"# end PASS loop=10 commands={len(lines)}"] if end else [])
        (path / f"{label}.orders").write_text("".join(line + "\n" for line in body))
    (path / "manifest.json").write_text(json.dumps({"complete": complete, "runs": dict.fromkeys(logs, "PASS")}))
    return path


def test_order_comparison_needs_complete_evidence(tmp_path):
    from scripts.compare_orders import compare

    log = {"fight_winnable[top]": ["2 1 ZERGLING ATTACK #5"]}
    assert compare(tmp_path / "missing", tmp_path / "also_missing")  # no directories
    a, b = order_run(tmp_path / "a", log), order_run(tmp_path / "b", log)
    assert compare(a, b, only=["fight_winnable"]) == []
    assert compare(a, b)  # the other repeatable scenarios are in neither run
    assert compare(a, order_run(tmp_path / "c", log, complete=False), only=["fight_winnable"])
    assert compare(a, order_run(tmp_path / "d", log, end=False), only=["fight_winnable"])  # a crashed game
    assert compare(a, order_run(tmp_path / "e", {}), only=["fight_winnable"])  # missing on one side
    other = order_run(tmp_path / "f", {"fight_winnable[top]": ["2 1 ZERGLING ATTACK #6"]})
    assert compare(a, other, only=["fight_winnable"]) == ["fight_winnable[top]: differs at line 1"]
    empty_a, empty_b = order_run(tmp_path / "g", {}), order_run(tmp_path / "h", {})
    assert compare(empty_a, empty_b, everything=True) == ["nothing to compare"]


def test_benchmark_games_seed_our_side_from_the_game_seed(monkeypatch):
    import os
    import random
    import uuid

    import scripts.benchmark as benchmark

    monkeypatch.setattr(uuid, "uuid4", uuid.uuid4)  # seeding replaces it: restored after the test
    monkeypatch.setattr(benchmark, "play_game", lambda *args: random.random())
    args = ("Map", "Terran", "Hard", 1200, {}, 7, None, "replay")
    assert benchmark.play_game_in_worker(args) == random.Random(7).random()
    assert benchmark.FIELDS[-3:] == ["jobs", "host", "python"]
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    from scripts import run_meta

    run_meta.repeatable_workers()
    assert os.environ["PYTHONHASHSEED"] == "0"
