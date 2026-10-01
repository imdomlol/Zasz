import json

from sc2.data import Result
from sc2.ids.unit_typeid import UnitTypeId

from bot.decisions import DECISION_NAMES, Build, Decision
from bot.observation import FEATURE_NAMES, Observation
from bot.recording import DecisionRecorder, read_records


def obs(t: float) -> Observation:
    return Observation(game_time=t, drones=int(t), own_units={UnitTypeId.ZERGLING: 2})


def test_records_every_interval_and_every_change(tmp_path):
    path = tmp_path / "game.jsonl"
    rec = DecisionRecorder(path, every_loops=22, meta={"map": "TestMap"})
    same, other = Decision(drone_target=20), Decision(build=Build.POOL, drone_target=20)
    rec.record(0, obs(0), same)  # first step: always
    rec.record(10, obs(1), same)  # too soon, same decision: skipped
    rec.record(12, obs(2), other)  # decision changed: recorded
    rec.record(30, obs(3), other)  # too soon after loop 12: skipped
    rec.record(34, obs(4), other)  # 22 loops after loop 12: recorded
    rec.close(Result.Victory)

    recording = read_records(path)
    assert [loop for loop, _, _ in recording.steps] == [0, 12, 34]
    assert recording.steps[1][1] == obs(2) and recording.steps[1][2] == other
    assert recording.result == "Victory"
    assert recording.header["features"] == FEATURE_NAMES
    assert recording.header["decisions"] == DECISION_NAMES
    assert recording.header["map"] == "TestMap"


def test_unfinished_game_has_no_result(tmp_path):
    path = tmp_path / "crash.jsonl"
    rec = DecisionRecorder(path)
    rec.record(0, obs(0), Decision())
    rec._file.flush()  # the game crashed: close() never ran
    assert read_records(path).result is None


def test_nothing_written_without_steps(tmp_path):
    rec = DecisionRecorder(tmp_path / "empty.jsonl")
    rec.close(Result.Defeat)
    assert not (tmp_path / "empty.jsonl").exists()


def test_lines_are_plain_json(tmp_path):
    path = tmp_path / "game.jsonl"
    rec = DecisionRecorder(path)
    rec.record(0, obs(0), Decision(army={UnitTypeId.ZERGLING: 1.0}))
    rec.close("Tie")
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["type"] for line in lines] == ["header", "step", "end"]
    assert lines[1]["decision"]["army"] == {"ZERGLING": 1.0}
