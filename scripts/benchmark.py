"""Play Zasz against the built-in AI across maps and races, and report how it did.

Run this before and after a change to catch regressions:

    uv run python scripts/benchmark.py                        # 6 games vs Hard
    uv run python scripts/benchmark.py --games 12 --difficulty VeryHard --races Terran
    uv run python scripts/benchmark.py --games 30 --jobs 6    # 6 games at a time

Each game's stats are appended to benchmarks/results.csv (gitignored). With --jobs, games run in parallel
but their logs and results are reported in matchup order, exactly as a sequential run would.

Step time is measured the way the AI Arena ladder measures it (the bot's own time per step, not
time spent waiting on SC2) and checked against the ladder's limits (LADDER_* below): a game over a
limit keeps its replay and the run exits with 1. Parallel games slow each other down, so a game
over the limit with --jobs > 1 is worth rerunning with --jobs 1.

A game that crashes is recorded as "Crash" and the rest of the run carries on. Replays go to
replays/benchmarks/<run>/; by default only notable ones are kept (see --replays), plus a .log next to
each crash with its traceback and game log.
"""

import argparse
import csv
import io
import itertools
import multiprocessing
import random
import re
import statistics
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Same paths as run.py: our bot, then ares (`ares-sc2/src`) and its `sc2_helper` (`ares-sc2`)
sys.path[:0] = [str(ROOT), str(ROOT / "ares-sc2" / "src"), str(ROOT / "ares-sc2")]

from loguru import logger
from sc2 import maps
from sc2.data import Difficulty, Race, Result
from sc2.main import run_game
from sc2.paths import Paths
from sc2.player import Bot, Computer

from bot import ZaszBot

RESULTS_FILE = ROOT / "benchmarks" / "results.csv"
REPLAYS_DIR = ROOT / "replays" / "benchmarks"
# A win counts as slow (its replay is kept) past this multiple of the usual win time at that difficulty
SLOW_WIN_FACTOR = 1.5
SLOW_WIN_MIN_HISTORY = 5
# AI Arena limits, from its match controller (aiarena/sc2-ai-match-controller, checked 2026-10-01):
# a bot that takes longer than this to answer loses the game by timeout (enforced)
LADDER_TIMEOUT_S = 30
# `max_frame_time`: average bot time per game loop. Configured there but not enforced (yet); the
# average is reported with every ladder result, so stay well under it
LADDER_FRAME_TIME_MS = 40
FIELDS = [
    "timestamp",
    "commit",
    "map",
    "opponent_race",
    "difficulty",
    "result",
    "game_time",
    "idle_worker_time",
    "collected_minerals",
    "killed_value",
    "lost_value",
    "max_drones",
    "frame_ms",
    "avg_step_ms",
    "max_step_ms",
    "startup_s",
    "replay",
    "error",
]


class BenchmarkZasz(ZaszBot):
    """ZaszBot that records end-of-game stats, including its step time.

    Step time is measured like the ladder does: the bot's own time between getting an observation
    and asking for the next step (parsing the observation, ares, on_step, sending actions), minus
    any time spent waiting for SC2 to answer a request.
    """

    def __init__(self):
        super().__init__()
        self.stats: dict = {}
        self.max_drones = 0
        self.step_times: list[float] = []  # seconds of bot time per step
        self.startup_seconds = 0.0
        self._startup_started = 0.0
        self._sc2_wait = 0.0  # total seconds spent waiting for SC2
        self._mark: float | None = None  # when the last step finished
        self._mark_wait = 0.0  # _sc2_wait at that moment

    async def on_before_start(self) -> None:
        # Startup counts from here: on_before_start, python-sc2's first-step setup (expansion
        # locations, ramps) and on_start (ares builds its managers and grids) all happen before
        # the bot answers its first observation
        self._startup_started = time.perf_counter()
        await super().on_before_start()
        execute = self.client._execute

        async def timed_execute(**kwargs):
            started = time.perf_counter()
            try:
                return await execute(**kwargs)
            finally:
                self._sc2_wait += time.perf_counter() - started

        self.client._execute = timed_execute

    async def on_start(self) -> None:
        await super().on_start()
        self.startup_seconds = time.perf_counter() - self._startup_started - self._sc2_wait
        self._mark, self._mark_wait = time.perf_counter(), self._sc2_wait

    async def _after_step(self) -> int:
        result = await super()._after_step()  # ares runs its behaviors and sends actions in here
        now = time.perf_counter()
        if self._mark is not None:
            self.step_times.append((now - self._mark) - (self._sc2_wait - self._mark_wait))
        self._mark, self._mark_wait = now, self._sc2_wait
        return result

    async def on_step(self, iteration: int) -> None:
        await super().on_step(iteration)
        self.max_drones = max(self.max_drones, self.supply_workers)

    async def on_end(self, game_result: Result) -> None:
        await super().on_end(game_result)
        score = self.state.score
        self.stats = {
            "game_time": round(self.time),
            "idle_worker_time": score.idle_worker_time,
            "collected_minerals": score.collected_minerals,
            "killed_value": score.killed_value_units + score.killed_value_structures,
            "lost_value": score.lost_minerals_army + score.lost_minerals_economy,
            "max_drones": self.max_drones,
        }
        if self.step_times:
            total = sum(self.step_times)
            self.stats |= {
                # What the ladder reports: bot time per game loop (a step covers GameStep loops)
                "frame_ms": round(1000 * total / max(self.state.game_loop, 1), 2),
                "avg_step_ms": round(1000 * total / len(self.step_times), 2),
                "max_step_ms": round(1000 * max(self.step_times), 1),
                "startup_s": round(self.startup_seconds, 2),
            }


def installed_maps() -> list[str]:
    return sorted(p.stem for p in Path(Paths.MAPS).glob("*.SC2Map"))


def current_commit() -> str:
    head = ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            return (ROOT / ".git" / ref[5:]).read_text().strip()[:7]
        return ref[:7]
    except OSError:
        return "none"


def play_game(
    map_name: str, race: str, difficulty: str, time_limit: int, commit: str, replay: str
) -> tuple[dict, float, str]:
    """Play one game. Returns its row, real seconds taken and its log; a crash becomes a "Crash" row."""
    log = io.StringIO()
    # Same sink settings python-sc2 uses (sc2/main.py), so a replayed log matches a sequential run
    capture = logger.add(log, level="INFO", colorize=sys.stdout.isatty())
    bot = BenchmarkZasz()
    started = time.monotonic()
    row = {"commit": commit, "map": map_name, "opponent_race": race, "difficulty": difficulty}
    try:
        result = run_game(
            maps.get(map_name),
            [Bot(Race.Zerg, bot, name="Zasz"), Computer(Race[race], Difficulty[difficulty])],
            realtime=False,
            game_time_limit=time_limit,
            save_replay_as=replay,
        )
        row["result"] = result.name if isinstance(result, Result) else str(result)
    except Exception as e:
        row["result"] = "Crash"
        row["error"] = f"{type(e).__name__}: {e}"
        logger.error(traceback.format_exc())
    finally:
        logger.remove(capture)
    row = {"timestamp": datetime.now().isoformat(timespec="seconds"), **row, **bot.stats}
    return row, time.monotonic() - started, log.getvalue()


def play_game_in_worker(game_args: tuple) -> tuple[dict, float, str]:
    """play_game in a worker process: log only to the buffer, so the parent can print it in order."""
    logger.remove()
    return play_game(*game_args)


def run_matchups(game_args: list[tuple], jobs: int):
    """Yield (row, real_time, log) per game, in matchup order."""
    if jobs == 1:
        for a in game_args:
            yield play_game(*a)
        return

    # One fresh process per game (python-sc2 keeps per-process state). Results are taken in submission
    # order, so a game that finishes early waits for the ones before it to be reported.
    with ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"), max_tasks_per_child=1) as pool:
        futures = [pool.submit(play_game_in_worker, a) for a in game_args]
        try:
            for a, future in zip(game_args, futures, strict=True):
                try:
                    row, real_time, log = future.result()
                except Exception as e:  # the worker process itself died
                    map_name, race, difficulty, _, commit, _ = a
                    row = {
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "commit": commit,
                        "map": map_name,
                        "opponent_race": race,
                        "difficulty": difficulty,
                        "result": "Crash",
                        "error": f"worker died: {type(e).__name__}: {e}",
                    }
                    real_time, log = 0.0, traceback.format_exc()
                sys.stdout.write(log)
                yield row, real_time, log
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise


def open_results() -> tuple[list[dict], bool]:
    """Past results, upgrading the file to the current columns if they changed. Returns (rows, new_file)."""
    if not RESULTS_FILE.exists():
        return [], True
    with RESULTS_FILE.open(newline="") as f:
        reader = csv.DictReader(f)
        history = list(reader)
        header = reader.fieldnames
    if header != FIELDS:
        with RESULTS_FILE.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(history)
    return history, False


def over_ladder_limits(row: dict) -> list[str]:
    """Which AI Arena limits this game broke (empty if none, or if it wasn't timed)."""
    over = []
    if row.get("frame_ms", 0) >= LADDER_FRAME_TIME_MS:
        over.append(f"avg {row['frame_ms']} ms per game loop >= {LADDER_FRAME_TIME_MS} ms")
    if row.get("max_step_ms", 0) >= 1000 * LADDER_TIMEOUT_S:
        over.append(f"a step took {row['max_step_ms'] / 1000:.1f} s >= {LADDER_TIMEOUT_S} s")
    if row.get("startup_s", 0) >= LADDER_TIMEOUT_S:
        over.append(f"startup took {row['startup_s']} s >= {LADDER_TIMEOUT_S} s")
    return over


def is_notable(row: dict, win_times: list[float]) -> bool:
    """Lost, tied, crashed, over a ladder time limit, or a win much slower than usual."""
    if row["result"] != "Victory" or over_ladder_limits(row):
        return True
    if len(win_times) < SLOW_WIN_MIN_HISTORY or "game_time" not in row:
        return False
    return row["game_time"] > SLOW_WIN_FACTOR * statistics.median(win_times)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--games", type=int, default=6)
    parser.add_argument("--difficulty", default="Hard", choices=[d.name for d in Difficulty])
    parser.add_argument("--races", nargs="+", default=["Terran", "Zerg", "Protoss"])
    parser.add_argument("--maps", nargs="+", default=None, help="Defaults to every installed map")
    parser.add_argument("--time-limit", type=int, default=20 * 60, help="Game seconds before calling it a tie")
    parser.add_argument(
        "--jobs", type=int, default=1, help="Games to run at once (each uses ~1 CPU core and ~1.5 GB of RAM)"
    )
    parser.add_argument(
        "--replays",
        default="notable",
        choices=["notable", "all", "none"],
        help="Replays to keep: notable = losses, ties, crashes and unusually slow wins (default)",
    )
    args = parser.parse_args()

    map_names = args.maps or installed_maps()
    # Cycle races evenly; shuffle maps so short runs still see variety
    random.shuffle(map_names)
    matchups = list(itertools.islice(zip(itertools.cycle(map_names), itertools.cycle(args.races)), args.games))

    RESULTS_FILE.parent.mkdir(exist_ok=True)
    history, new_file = open_results()
    win_times = [
        float(r["game_time"])
        for r in history
        if r["difficulty"] == args.difficulty and r["result"] == "Victory" and r["game_time"]
    ]
    commit = current_commit()
    run_dir = REPLAYS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    game_args = [
        (
            map_name,
            race,
            args.difficulty,
            args.time_limit,
            commit,
            str(run_dir / f"{i:02d}_{race}_{map_name}.SC2Replay"),
        )
        for i, (map_name, race) in enumerate(matchups, 1)
    ]
    rows = []

    with RESULTS_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()

        jobs = max(1, min(args.jobs, len(matchups)))
        for i, (row, real_time, log) in enumerate(run_matchups(game_args, jobs), 1):
            replay = Path(game_args[i - 1][-1])
            if row["result"] == "Crash":
                replay.with_suffix(".log").write_text(re.sub(r"\x1b\[[0-9;]*m", "", log))  # drop colors
            if args.replays == "all" or (args.replays == "notable" and is_notable(row, win_times)):
                if replay.exists():
                    row["replay"] = str(replay.relative_to(ROOT))
            else:
                replay.unlink(missing_ok=True)
            if row["result"] == "Victory" and "game_time" in row:
                win_times.append(row["game_time"])

            writer.writerow(row)
            f.flush()
            rows.append(row)
            logger.success(
                f"[{i}/{len(matchups)}] {row['result']} vs {row['opponent_race']} on {row['map']} "
                f"({row.get('game_time', '?')}s game, {real_time:.0f}s real"
                + (f", step avg {row['avg_step_ms']} ms / max {row['max_step_ms']} ms" if "max_step_ms" in row else "")
                + ")"
                + (f" - {row['error']}" if row.get("error") else "")
            )

    if not any(run_dir.iterdir()):
        run_dir.rmdir()
    print_summary(rows, jobs)
    if over := [r for r in rows if over_ladder_limits(r)]:
        print(f"\n{len(over)} game(s) over the AI Arena time limits:")
        for r in over:
            print(f"  {r['map']} vs {r['opponent_race']}: {'; '.join(over_ladder_limits(r))}")
        sys.exit(1)


def print_step_time(rows: list[dict], jobs: int) -> None:
    """Worst step-time numbers of the run against the ladder limits."""
    timed = [r for r in rows if "frame_ms" in r]
    if not timed:
        return
    frame = max(r["frame_ms"] for r in timed)
    step = max(r["max_step_ms"] for r in timed) / 1000
    startup = max(r["startup_s"] for r in timed)

    def check(value: float, limit: float) -> str:
        return f"{100 * value / limit:.0f}% of the limit" + ("  <-- OVER" if value >= limit else "")

    print(f"Step time (worst game; measured with --jobs {jobs}):")
    print(f"  avg per game loop {frame:.2f} ms / {LADDER_FRAME_TIME_MS} ms: {check(frame, LADDER_FRAME_TIME_MS)}")
    print(f"  slowest step      {step:.2f} s / {LADDER_TIMEOUT_S} s: {check(step, LADDER_TIMEOUT_S)}")
    print(f"  startup           {startup:.2f} s / {LADDER_TIMEOUT_S} s: {check(startup, LADDER_TIMEOUT_S)}")
    if jobs > 1:
        print("  (parallel games slow each other down; rerun with --jobs 1 for exact numbers)")


def print_summary(rows: list[dict], jobs: int = 1) -> None:
    def win_rate(subset: list[dict]) -> str:
        wins = sum(r["result"] == "Victory" for r in subset)
        return f"{wins}/{len(subset)} ({100 * wins / len(subset):.0f}%)"

    print("\n=== Benchmark summary ===")
    print(f"Overall: {win_rate(rows)}")
    for key in ("opponent_race", "map"):
        for value in sorted({r[key] for r in rows}):
            print(f"  {value:<20} {win_rate([r for r in rows if r[key] == value])}")
    idle = [r["idle_worker_time"] for r in rows if "idle_worker_time" in r]
    if idle:
        print(f"Avg idle worker time: {sum(idle) / len(idle):.1f}")
    print_step_time(rows, jobs)
    crashes = [r for r in rows if r["result"] == "Crash"]
    if crashes:
        print(f"Crashes: {len(crashes)} (tracebacks in the .log next to each replay)")
    kept = [r["replay"] for r in rows if r.get("replay")]
    if kept:
        print(f"Replays kept ({len(kept)}):")
        for replay in kept:
            print(f"  {replay}")
    print(f"Saved to {RESULTS_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
