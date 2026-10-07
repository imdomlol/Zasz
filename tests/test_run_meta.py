"""Run labels and result CSVs (`scripts/run_meta.py`) and benchmark planning (`scripts/benchmark.py`)."""

import subprocess
from collections import Counter

import pytest

from scripts import run_meta
from scripts.benchmark import plan_matchups, print_summary


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / "bot").mkdir()
    (tmp_path / "bot" / "x.py").write_text("a = 1\n")
    (tmp_path / "benchmarks").mkdir()
    (tmp_path / "benchmarks" / "x.csv").write_text("a\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


def short_head(repo):
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_clean_checkout_is_the_commit(repo):
    assert run_meta.current_commit(repo) == short_head(repo)


def test_packed_refs_still_give_the_commit(repo):
    git(repo, "pack-refs", "--all")
    assert run_meta.current_commit(repo) == short_head(repo)


def test_worktree_gives_its_commit(repo, tmp_path_factory):
    other = tmp_path_factory.mktemp("worktree") / "wt"
    git(repo, "worktree", "add", "-q", str(other))
    assert run_meta.current_commit(other) == short_head(repo)


def test_appended_results_dont_make_it_dirty(repo):
    (repo / "benchmarks" / "x.csv").write_text("a\n1\n")
    (repo / "benchmarks" / "new.csv").write_text("b\n")
    assert run_meta.current_commit(repo) == short_head(repo)


def test_changed_or_new_code_is_dirty(repo):
    (repo / "bot" / "x.py").write_text("a = 2\n")
    assert run_meta.current_commit(repo) == short_head(repo) + "-dirty"
    git(repo, "checkout", "--", "bot/x.py")
    (repo / "bot" / "y.py").write_text("b = 1\n")
    assert run_meta.current_commit(repo) == short_head(repo) + "-dirty"


def test_outside_git_is_none(tmp_path):
    assert run_meta.current_commit(tmp_path) == "none"


def test_run_ids_differ_within_a_second():
    assert len({run_meta.new_run_id() for _ in range(20)}) > 1


def test_workers_inherit_the_run_labels(monkeypatch):
    for name in run_meta.ENV.values():
        monkeypatch.delenv(name, raising=False)
    meta = run_meta.start_run(75689)
    assert meta["sc2_build"] == "75689"
    assert run_meta.inherited() == meta


def write(path, lines):
    path.write_text("".join(line + "\n" for line in lines))


def test_new_columns_go_at_the_end_and_old_rows_keep_their_values(tmp_path):
    path = tmp_path / "r.csv"
    write(path, ["timestamp,commit", "t1,abc"])
    rows = run_meta.ensure_header(path, ["timestamp", "commit", "run_id"])
    assert rows == [{"timestamp": "t1", "commit": "abc"}]
    assert run_meta.read_rows(path) == (
        [{"timestamp": "t1", "commit": "abc", "run_id": ""}],
        ["timestamp", "commit", "run_id"],
    )
    assert path.read_text().splitlines() == ["timestamp,commit,run_id", "t1,abc"]  # the old row as it was
    assert not path.with_name("r.csv.tmp").exists()


def test_a_missing_file_gets_a_header(tmp_path):
    path = tmp_path / "r.csv"
    assert run_meta.ensure_header(path, ["timestamp", "run_id"]) == []
    assert path.read_text().splitlines() == ["timestamp,run_id"]


def test_columns_are_never_dropped(tmp_path):
    path = tmp_path / "r.csv"
    write(path, ["timestamp,old", "t1,x"])
    with pytest.raises(SystemExit):
        run_meta.ensure_header(path, ["timestamp"])
    assert path.read_text() == "timestamp,old\nt1,x\n"


def test_a_header_left_mid_file_by_a_merge_is_skipped(tmp_path):
    path = tmp_path / "r.csv"
    write(path, ["timestamp,commit,run_id", "t1,abc,r1", "timestamp,commit", "t2,def"])
    rows, header = run_meta.read_rows(path)
    assert header == ["timestamp", "commit", "run_id"]
    assert [r["timestamp"] for r in rows] == ["t1", "t2"]


def test_at_least_rejects_small_counts():
    parse = run_meta.at_least(1)
    assert parse("3") == 3
    with pytest.raises(Exception, match="at least 1"):
        parse("0")


MAPS = ["A", "B", "C"]
RACES = ["Terran", "Zerg", "Protoss"]


@pytest.mark.parametrize("maps", [["A"], ["A", "B"], MAPS, ["A", "B", "C", "D", "E", "F"], list("ABCDEFG")])
def test_a_full_cycle_plays_every_map_race_pair_once(maps):
    plan = plan_matchups(maps, RACES, len(maps) * len(RACES), seed=5)
    assert sorted(plan) == sorted((m, r) for m in maps for r in RACES)


def test_every_stretch_of_games_has_each_race_once():
    plan = plan_matchups(list("ABCDEFG"), RACES, 21, seed=1)
    for start in range(0, 21, 3):
        assert sorted(r for _, r in plan[start : start + 3]) == sorted(RACES)


def test_longer_runs_repeat_pairs_evenly():
    counts = Counter(plan_matchups(MAPS, RACES, 18, seed=2))
    assert set(counts.values()) == {2} and len(counts) == 9


def test_same_seed_same_plan_other_seed_other_order():
    assert plan_matchups(list("ABCDEFG"), RACES, 6, seed=3) == plan_matchups(list("ABCDEFG"), RACES, 6, seed=3)
    plans = {tuple(plan_matchups(list("ABCDEFG"), RACES, 21, seed=s)) for s in range(10)}
    assert len(plans) > 1


def test_summary_of_no_games_doesnt_divide_by_zero(capsys):
    print_summary([])
    assert "0/0" in capsys.readouterr().out


def test_merged_segments_keep_every_value_in_either_order(tmp_path):
    """Two machines' copies union-merged: the newer header can come before or after the older one."""
    for lines in (
        ["timestamp,commit", "t1,abc", "timestamp,commit,run_id", "t2,def,new-run"],
        ["timestamp,commit,run_id", "t2,def,new-run", "timestamp,commit", "t1,abc"],
    ):
        path = tmp_path / "r.csv"
        write(path, lines)
        rows, columns = run_meta.read_rows(path)
        assert columns == ["timestamp", "commit", "run_id"]
        assert {r["timestamp"]: r["run_id"] for r in rows} == {"t1": "", "t2": "new-run"}
        assert len(run_meta.ensure_header(path, ["timestamp", "commit", "run_id", "jobs"])) == 2


def test_a_header_that_isnt_an_extension_stops_the_run(tmp_path):
    path = tmp_path / "r.csv"
    write(path, ["timestamp,commit", "t1,abc", "timestamp,run_id", "t2,r2"])
    with pytest.raises(SystemExit):
        run_meta.read_rows(path)
