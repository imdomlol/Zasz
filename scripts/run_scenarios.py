"""Run the staged scenarios in `scenarios/` and report which pass, and what they cost.

    uv run python scripts/run_scenarios.py                  # all of them, enemy main at the top
    uv run python scripts/run_scenarios.py --side both      # each from both sides
    uv run python scripts/run_scenarios.py tank_dive -v     # one scenario, with its game log
    uv run python scripts/run_scenarios.py efficiency_cramped_main --side both --replays all
    uv run python scripts/run_scenarios.py tank_dive --realtime   # watch it (Windows: game window)
    uv run python scripts/run_scenarios.py --random --repeat 5    # random starts, to catch flaky ones
    uv run python scripts/run_scenarios.py efficiency_cramped_main --seeds 8   # the same 8 games each time
    uv run python scripts/run_scenarios.py --list

Every run uses a fixed game seed for its side (`scenarios/seeds.py`), so it plays out the same way
each time and can be compared number for number. Each result is appended to
benchmarks/scenarios.csv (army lost, damage taken/dealt, time) with the commit and run id
(`scripts/run_meta.py`), and printed next to the last earlier run of the same scenario, side, seed and
StarCraft II build.

Exits with 1 if any scenario fails, so it can gate a commit. Replays of failed scenarios are kept in
replays/scenarios/<run>/ (--replays all keeps every one).
"""

import _thread
import argparse
import csv
import io
import multiprocessing
import os
import random
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scripts.bootstrap  # noqa: F401 (our bot and ares on the import path)

# isort: split

import numpy as np
from loguru import logger
from sc2 import maps
from sc2.data import Difficulty, Race
from sc2.main import run_game
from sc2.player import Bot, Computer

from scenarios import ALL, ScenarioBot, efficiency, seeds
from scripts import run_meta

REPLAYS_DIR = ROOT / "replays" / "scenarios"
RESULTS_FILE = ROOT / "benchmarks" / "scenarios.csv"
FIELDS = [
    "timestamp",
    "commit",
    "scenario",
    "side",
    "seed",
    "sc2_build",
    "status",
    "game_time",
    "units_lost",
    "army_lost",
    "damage_taken",
    "damage_dealt",
    "message",
    "run_id",  # new columns go at the end, so older rows still parse
]
# Watchdog: a scenario taking longer than time_limit * FACTOR + MARGIN real seconds is stuck
WATCHDOG_FACTOR = 3
WATCHDOG_MARGIN = 120


@dataclass
class Report:
    name: str
    status: str  # PASS, FAIL or ERROR
    message: str
    game_time: float
    real_time: float
    log: str
    stats: dict | None = None  # ScenarioBot.stats(): units/army lost, damage taken/dealt
    efficiency: dict | None = None  # an efficiency scenario's measurements, for efficiency.csv


def run_scenario(
    name: str,
    replay: str,
    watchdog: float | None = None,
    seed: int | None = None,
    realtime: bool = False,
    order_log: str | None = None,
) -> Report:
    """Play one scenario in this process (a worker); its log is captured, not printed."""
    if order_log:
        os.environ["ZASZ_ORDER_LOG"] = order_log  # read by ScenarioBot (this worker only)
    if seed is not None:
        make_repeatable(seed)
    logger.remove()
    log = io.StringIO()
    logger.add(log, level="INFO", colorize=sys.stdout.isatty())
    scenario = next(s for s in ALL if s.name == name)()
    bot = ScenarioBot(scenario)
    started = time.monotonic()
    # If StarCraft II crashes mid-game, python-sc2 waits on the dead connection forever and the
    # whole run hangs. A watchdog interrupts this worker after far more real time than the
    # scenario could need, so it's reported as an ERROR and the run carries on.
    deadline = watchdog or scenario.time_limit * WATCHDOG_FACTOR + WATCHDOG_MARGIN
    fired = threading.Event()

    def bark() -> None:
        fired.set()
        _thread.interrupt_main()

    watchdog = threading.Timer(deadline, bark)
    watchdog.daemon = True
    watchdog.start()
    try:
        run_game(
            maps.get(scenario.map),
            [Bot(Race.Zerg, bot, name="Zasz"), Computer(scenario.enemy_race, Difficulty.VeryEasy)],
            realtime=realtime,
            save_replay_as=replay,
            random_seed=seed,
        )
        if bot.outcome is None:
            status, message = "ERROR", "the game ended before the scenario was decided"
        else:
            status, message = ("PASS" if bot.outcome.passed else "FAIL"), bot.outcome.message
    except (KeyboardInterrupt, Exception) as e:
        status, message = "ERROR", f"{type(e).__name__}: {e}"
        logger.error(traceback.format_exc())
    finally:
        watchdog.cancel()
    if fired.is_set():
        # python-sc2 turns the interrupt into other errors (e.g. ConnectionAlreadyClosed)
        status = "ERROR"
        message = f"no result after {deadline:.0f}s real time (StarCraft II probably crashed or hung)"
        logger.error(message)
    game_time = bot.time if getattr(bot, "state", None) else 0.0
    real_time = time.monotonic() - started
    efficiency_row = getattr(scenario, "result_row", None)
    return Report(name, status, message, game_time, real_time, log.getvalue(), bot.stats(), efficiency_row)


def make_repeatable(seed: int) -> None:
    """Our side of a fixed-seed run: StarCraft II follows the game seed, but ares picks creep tumor
    spots with Python's `random` and names squads with `uuid4`, which made the same seed play out a
    little differently each time. Seed those from the game seed too (this worker process only)."""
    random.seed(seed)
    np.random.seed(seed)
    uuid.uuid4 = lambda: uuid.UUID(int=random.getrandbits(128), version=4)


def append_result(row: dict, path: Path = RESULTS_FILE, fields: list[str] = FIELDS) -> None:
    """One row, appended by this (parent) process only, so parallel scenarios never race for the file."""
    path.parent.mkdir(exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new:
            writer.writeheader()
        writer.writerow(row)


def compare(row: dict, history: list[dict]) -> str:
    """This run next to the last earlier run of the same scenario, side, seed and build."""
    if not row["seed"]:
        return ""  # random start: nothing exactly comparable
    same = [
        r
        for r in history
        if (r["scenario"], r["side"], r["seed"], r["sc2_build"])
        == (row["scenario"], row["side"], row["seed"], row["sc2_build"])
        and r.get("run_id") != row["run_id"]
    ]
    if not same:
        return ""
    prev = same[-1]

    def delta(field: str, unit: str = "") -> str:
        old, new = float(prev[field] or 0), float(row[field] or 0)
        if old == new:
            return f"{field.replace('_', ' ')} {new:g}{unit} (same)"
        return f"{field.replace('_', ' ')} {old:g}{unit} -> {new:g}{unit}"

    changed = "" if prev["status"] == row["status"] else f" {prev['status']} -> {row['status']};"
    return (
        f"      vs {prev['commit']}:{changed} {delta('army_lost')}, {delta('damage_taken')}, {delta('game_time', 's')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="Scenarios to run (default: all)")
    parser.add_argument("--jobs", type=run_meta.at_least(1), default=4, help="Scenarios to run at once")
    parser.add_argument(
        "--overlay", action="store_true", help="Draw what the army is doing in the game window (bot/overlay.py)"
    )
    parser.add_argument(
        "--slow", type=float, default=0, help="Play this many times slower than real time, to watch (e.g. 3)"
    )
    parser.add_argument("--repeat", type=run_meta.at_least(1), default=1, help="Run each scenario this many times")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print every scenario's game log")
    parser.add_argument("--list", action="store_true", help="List the scenarios and exit")
    parser.add_argument(
        "--side",
        default="top",
        choices=["top", "bottom", "both"],
        help="Which side the enemy main is on (left/right on maps laid out that way; default: top)",
    )
    parser.add_argument("--random", action="store_true", help="Random starts instead of the fixed seeds")
    parser.add_argument(
        "--seeds",
        type=run_meta.at_least(0),
        default=0,
        help="Play each scenario with game seeds 1..N instead of one fixed seed per side: the same N games "
        "every time (sides mixed by the seed), to compare versions over more than one game",
    )
    parser.add_argument(
        "--replays",
        default="failed",
        choices=["failed", "all", "none"],
        help="Replays to keep (default: failed)",
    )
    parser.add_argument(
        "--realtime", action="store_true", help="Play at normal speed, one at a time (to watch on Windows)"
    )
    parser.add_argument(
        "--watchdog",
        type=float,
        default=None,
        help="Real seconds before a stuck scenario (e.g. StarCraft II crashed) is reported as an ERROR "
        f"(default: time limit x {WATCHDOG_FACTOR} + {WATCHDOG_MARGIN})",
    )
    parser.add_argument(
        "--order-log",
        type=Path,
        help="Write every command each scenario sends to DIR/<scenario>.orders, to check that a refactor "
        "changes no order (scripts/compare_orders.py)",
    )
    args = parser.parse_args()
    if args.order_log:
        args.order_log.mkdir(parents=True, exist_ok=True)
    if args.overlay:
        os.environ["ZASZ_OVERLAY"] = "1"  # read by the game processes (bot/overlay.py)
    if args.slow:
        os.environ["ZASZ_SLOW"] = str(args.slow)  # read by the game processes (ScenarioBot.slow_down)

    if args.list:
        for s in ALL:
            print(f"{s.name:<24} {s.map:<18} {s.description}")
        return

    known = {s.name for s in ALL}
    if unknown := [n for n in args.names if n not in known]:
        parser.error(f"unknown scenario(s): {', '.join(unknown)} (see --list)")
    if repeated := sorted({n for n in args.names if args.names.count(n) > 1}):
        parser.error(f"scenario(s) named twice: {', '.join(repeated)} (use --repeat)")
    names = args.names or [s.name for s in ALL]
    by_name = {s.name: s for s in ALL}

    # (scenario, side, seed): the fixed seed for each side, looked up (or probed) per map
    sides = ["top", "bottom"] if args.side == "both" else [args.side]
    plays: list[tuple[str, str, int | None]] = []
    for name in names:
        if args.random:
            plays.append((name, "random", None))  # sides don't apply: StarCraft II picks
            continue
        if args.seeds:
            plays += [(name, f"seed {n}", n) for n in range(1, args.seeds + 1)]
            continue
        for side in sides:
            seed, side_name = seeds.seed_for(by_name[name].map, side)
            plays.append((name, side_name, seed))
    runs = [(play, i) for play in plays for i in range(1, args.repeat + 1)]

    # This run's commit, build and id, for its rows here and in efficiency.csv
    meta = run_meta.start_run(seeds.sc2_build())
    history = run_meta.ensure_header(RESULTS_FILE, FIELDS)
    run_meta.ensure_header(efficiency.RESULTS_FILE, efficiency.FIELDS)
    print(f"Run {meta['run_id']} on {meta['commit']} (StarCraft II build {meta['sc2_build']})", flush=True)
    run_dir = REPLAYS_DIR / meta["run_id"]
    run_dir.mkdir(parents=True)

    def label(run) -> str:
        (name, side, _), i = run
        return f"{name}[{side}]" + (f"#{i}" if args.repeat > 1 else "")

    replays = {run: run_dir / f"{label(run)}.SC2Replay" for run in runs}
    reports: list[Report] = []
    started = time.monotonic()

    jobs = 1 if args.realtime else max(1, min(args.jobs, len(runs)))
    # Workers hash strings the same way every run (set order), part of making a seed repeatable
    os.environ.setdefault("PYTHONHASHSEED", "0")
    # One fresh process per scenario (python-sc2 keeps per-process state); reported in order
    with ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"), max_tasks_per_child=1) as pool:
        futures = {
            run: pool.submit(
                run_scenario,
                run[0][0],
                str(replays[run]),
                args.watchdog,
                run[0][2],
                args.realtime,
                str(args.order_log / f"{label(run)}.orders") if args.order_log else None,
            )
            for run in runs
        }
        for run in list(futures):
            (name, side, seed), _ = run
            future = futures.pop(run)  # a reported scenario's log isn't kept around
            try:
                report = future.result()
            except Exception as e:  # the worker process itself died
                report = Report(name, "ERROR", f"worker died: {type(e).__name__}: {e}", 0, 0, traceback.format_exc())
            reports.append(report)
            if args.verbose or report.status != "PASS":
                sys.stdout.write(report.log)
            report.log = ""
            stats = report.stats or {}
            cost = (
                f"; lost {stats['units_lost']} units ({stats['army_lost']} resources), "
                f"damage taken {stats['damage_taken']}"
                if stats
                else ""
            )
            print(
                f"{report.status:<5} {label(run):<34} {report.message} "
                f"({report.game_time:.0f}s game, {report.real_time:.0f}s real{cost})",
                flush=True,
            )
            row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "commit": meta["commit"],
                "scenario": name,
                "side": side,
                "seed": seed if seed is not None else "",
                "sc2_build": meta["sc2_build"],
                "status": report.status,
                "game_time": round(report.game_time, 1),
                "message": report.message,
                "run_id": meta["run_id"],
                **stats,
            }
            if line := compare({k: str(v) for k, v in row.items()}, history):
                print(line, flush=True)
            append_result(row)
            if report.efficiency:
                append_result(
                    {
                        "timestamp": row["timestamp"],
                        "commit": meta["commit"],
                        **report.efficiency,
                        "sc2_build": meta["sc2_build"],
                        "run_id": meta["run_id"],
                        "seed": row["seed"],
                        "status": report.status,
                    },
                    efficiency.RESULTS_FILE,
                    efficiency.FIELDS,
                )
            if args.replays == "none" or (args.replays == "failed" and report.status == "PASS"):
                replays[run].unlink(missing_ok=True)

    if not any(run_dir.iterdir()):
        run_dir.rmdir()
    failures = [r for r in reports if r.status != "PASS"]
    print(f"\n{len(reports) - len(failures)}/{len(reports)} passed in {time.monotonic() - started:.0f}s")
    if run_dir.exists():
        print(f"Replays: {run_dir.relative_to(ROOT)}")
    print(f"Results added to {RESULTS_FILE.relative_to(ROOT)}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
