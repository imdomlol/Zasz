import json

import pytest
from sc2.data import Result
from sc2.ids.unit_typeid import UnitTypeId

from bot.decisions import DECISION_NAMES, Build, Decision
from bot.observation import FEATURE_NAMES, Observation
from bot.recording import DecisionRecorder, iter_steps, read_records


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
    rec.flush()  # the game crashed: close() never ran
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


def test_records_after_close_are_ignored(tmp_path):
    path = tmp_path / "game.jsonl"
    rec = DecisionRecorder(path)
    rec.record(0, obs(0), Decision())
    rec.close(Result.Victory)
    before = path.read_text()
    rec.record(50, obs(1), Decision(build=Build.POOL))  # would reopen (and truncate) the file
    rec.close("Crash")  # a runner's cleanup after a normal end: nothing more to write
    assert path.read_text() == before
    assert read_records(path).result == "Victory"


def test_metadata_cannot_override_the_header(tmp_path):
    path = tmp_path / "game.jsonl"
    rec = DecisionRecorder(path, meta={"version": 99, "type": "x", "features": ["bogus"], "map": "M"})
    rec.record(0, obs(0), Decision())
    rec.close()
    header = read_records(path).header
    assert (header["type"], header["version"], header["features"], header["map"]) == ("header", 1, FEATURE_NAMES, "M")


def write_lines(path, *lines) -> None:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def header(**changes) -> dict:
    return {"type": "header", "version": 1, "features": FEATURE_NAMES, "decisions": DECISION_NAMES} | changes


def test_other_versions_and_unknown_features_are_errors(tmp_path):
    path = tmp_path / "game.jsonl"
    write_lines(path, header(version=2))
    with pytest.raises(ValueError, match="version"):
        read_records(path)
    write_lines(path, header(features=[*FEATURE_NAMES, "renamed_feature"]))
    with pytest.raises(ValueError, match="renamed_feature"):
        read_records(path)
    write_lines(path, header(features=FEATURE_NAMES[:3]))  # an older recording, fewer features: fine
    assert read_records(path).steps == []


def test_a_cut_off_last_line_is_an_error_unless_skipped(tmp_path):
    path = tmp_path / "game.jsonl"
    rec = DecisionRecorder(path)
    rec.record(0, obs(0), Decision())
    rec.record(22, obs(1), Decision())
    rec.flush()
    text = path.read_text()
    path.write_text(text[: len(text) - 10])  # killed mid-write
    with pytest.raises(ValueError, match=r"game\.jsonl:3"):
        read_records(path)
    assert [loop for loop, _, _ in read_records(path, skip_unfinished_line=True).steps] == [0]


def test_steps_stream_one_at_a_time(tmp_path):
    path = tmp_path / "game.jsonl"
    rec = DecisionRecorder(path)
    for loop in (0, 22, 44):
        rec.record(loop, obs(loop), Decision())
    rec.close()
    assert [loop for loop, _, _ in iter_steps(path)] == [0, 22, 44]


@pytest.mark.parametrize(
    "lines, problem",
    [
        ([], "empty"),
        ([header(decisions=[*DECISION_NAMES, "army.BOGUS"])], "unknown decisions"),
        ([header(features=None)], "no features list"),
        ([header(), header(version=99)], "second header"),
        ([header(), {"type": "end", "result": "Victory"}, {"type": "end", "result": "Tie"}], "after the end"),
        ([header(), {"type": "bogus"}], "unknown record type"),
        ([header(), {"loop": 0}], "unknown record type"),
        ([header(), {"type": "step", "loop": 0, "obs": {"renamed_feature": 1}, "decision": {}}], "unknown features"),
        ([header(), {"type": "step", "loop": 0, "obs": {}, "decision": {"attack": "yes"}}], "attack"),
    ],
)
def test_damaged_recordings_are_errors_with_their_line(tmp_path, lines, problem):
    path = tmp_path / "game.jsonl"
    write_lines(path, *lines)
    with pytest.raises(ValueError, match=problem):
        read_records(path)
