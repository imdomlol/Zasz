"""Play Zasz against the built-in AI across maps and races, and report how it did.

Run this before and after a change to catch regressions:

    uv run python scripts/benchmark.py                        # 6 games vs Hard
    uv run python scripts/benchmark.py --games 12 --difficulty VeryHard --races Terran
    uv run python scripts/benchmark.py --games 30 --jobs 6    # 6 games at a time

Each game's stats are appended to benchmarks/results.csv (gitignored). With --jobs, games run in parallel
but their logs and results are reported in matchup order, exactly as a sequential run would.
"""

import argparse
import csv
import io
import itertools
import multiprocessing
import random
import sys
import time
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
]


class BenchmarkZasz(ZaszBot):
    """ZaszBot that records end-of-game stats."""

    def __init__(self):
        super().__init__()
        self.stats: dict = {}
        self.max_drones = 0

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


def play_game(map_name: str, race: str, difficulty: str, time_limit: int, commit: str) -> tuple[dict, float]:
    bot = BenchmarkZasz()
    started = time.monotonic()
    result = run_game(
        maps.get(map_name),
        [Bot(Race.Zerg, bot, name="Zasz"), Computer(Race[race], Difficulty[difficulty])],
        realtime=False,
        game_time_limit=time_limit,
    )
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "commit": commit,
        "map": map_name,
        "opponent_race": race,
        "difficulty": difficulty,
        "result": result.name if isinstance(result, Result) else str(result),
        **bot.stats,
    }
    return row, time.monotonic() - started


def play_game_buffered(game_args: tuple) -> tuple[dict, float, str]:
    """Run play_game in a worker process, capturing its log so the parent can print it in order."""
    log = io.StringIO()
    # Same sink settings python-sc2 uses (sc2/main.py), so the replayed log matches a sequential run
    logger.remove()
    logger.add(log, level="INFO", colorize=sys.stdout.isatty())
    row, real_time = play_game(*game_args)
    return row, real_time, log.getvalue()


def run_matchups(matchups: list[tuple[str, str]], args, commit: str, jobs: int):
    """Yield (row, real_time) per matchup, in matchup order."""
    game_args = [(map_name, race, args.difficulty, args.time_limit, commit) for map_name, race in matchups]
    if jobs == 1:
        for a in game_args:
            yield play_game(*a)
        return

    # One fresh process per game (python-sc2 keeps per-process state). `map` returns results in submission
    # order, so a game that finishes early waits for the ones before it to be reported.
    with ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"), max_tasks_per_child=1) as pool:
        try:
            for row, real_time, log in pool.map(play_game_buffered, game_args):
                sys.stdout.write(log)
                yield row, real_time
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise


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
    args = parser.parse_args()

    map_names = args.maps or installed_maps()
    # Cycle races evenly; shuffle maps so short runs still see variety
    random.shuffle(map_names)
    matchups = list(itertools.islice(zip(itertools.cycle(map_names), itertools.cycle(args.races)), args.games))

    RESULTS_FILE.parent.mkdir(exist_ok=True)
    new_file = not RESULTS_FILE.exists()
    commit = current_commit()
    rows = []

    with RESULTS_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()

        jobs = max(1, min(args.jobs, len(matchups)))
        for i, (row, real_time) in enumerate(run_matchups(matchups, args, commit, jobs), 1):
            writer.writerow(row)
            f.flush()
            rows.append(row)
            logger.success(
                f"[{i}/{len(matchups)}] {row['result']} vs {row['opponent_race']} on {row['map']} "
                f"({row.get('game_time', '?')}s game, {real_time:.0f}s real)"
            )

    print_summary(rows)


def print_summary(rows: list[dict]) -> None:
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
    print(f"Saved to {RESULTS_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
