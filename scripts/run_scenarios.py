"""Run the staged scenarios in `scenarios/` and report which pass.

    uv run python scripts/run_scenarios.py                  # all of them, 4 at a time
    uv run python scripts/run_scenarios.py tank_dive -v     # one scenario, with its game log
    uv run python scripts/run_scenarios.py --repeat 5       # each 5 times, to catch flaky ones
    uv run python scripts/run_scenarios.py --list

Exits with 1 if any scenario fails, so it can gate a commit. Replays of failed scenarios are kept in
replays/scenarios/<run>/.
"""

import _thread
import argparse
import io
import multiprocessing
import sys
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Same paths as run.py: our bot, then ares (`ares-sc2/src`) and its `sc2_helper` (`ares-sc2`)
sys.path[:0] = [str(ROOT), str(ROOT / "ares-sc2" / "src"), str(ROOT / "ares-sc2")]

from loguru import logger
from sc2 import maps
from sc2.data import Difficulty, Race
from sc2.main import run_game
from sc2.player import Bot, Computer

from scenarios import ALL, ScenarioBot

REPLAYS_DIR = ROOT / "replays" / "scenarios"
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


def run_scenario(name: str, replay: str, watchdog: float | None = None) -> Report:
    """Play one scenario in this process (a worker); its log is captured, not printed."""
    logger.remove()
    log = io.StringIO()
    logger.add(log, level="INFO", colorize=sys.stdout.isatty())
    scenario = next(s for s in ALL if s.name == name)
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
            realtime=False,
            save_replay_as=replay,
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
    return Report(name, status, message, game_time, time.monotonic() - started, log.getvalue())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="Scenarios to run (default: all)")
    parser.add_argument("--jobs", type=int, default=4, help="Scenarios to run at once")
    parser.add_argument("--repeat", type=int, default=1, help="Run each scenario this many times")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print every scenario's game log")
    parser.add_argument("--list", action="store_true", help="List the scenarios and exit")
    parser.add_argument(
        "--watchdog",
        type=float,
        default=None,
        help="Real seconds before a stuck scenario (e.g. StarCraft II crashed) is reported as an ERROR "
        f"(default: time limit x {WATCHDOG_FACTOR} + {WATCHDOG_MARGIN})",
    )
    args = parser.parse_args()

    if args.list:
        for s in ALL:
            print(f"{s.name:<24} {s.map:<18} {s.description}")
        return

    known = {s.name for s in ALL}
    if unknown := [n for n in args.names if n not in known]:
        parser.error(f"unknown scenario(s): {', '.join(unknown)} (see --list)")
    names = args.names or [s.name for s in ALL]
    runs = [(name, i) for name in names for i in range(1, args.repeat + 1)]

    run_dir = REPLAYS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    label = {run: run[0] if args.repeat == 1 else f"{run[0]}#{run[1]}" for run in runs}
    replays = {run: run_dir / f"{label[run]}.SC2Replay" for run in runs}
    reports: list[Report] = []
    started = time.monotonic()

    jobs = max(1, min(args.jobs, len(runs)))
    # One fresh process per scenario (python-sc2 keeps per-process state); reported in order
    with ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"), max_tasks_per_child=1) as pool:
        futures = {run: pool.submit(run_scenario, run[0], str(replays[run]), args.watchdog) for run in runs}
        for run, future in futures.items():
            try:
                report = future.result()
            except Exception as e:  # the worker process itself died
                report = Report(run[0], "ERROR", f"worker died: {type(e).__name__}: {e}", 0, 0, traceback.format_exc())
            reports.append(report)
            if args.verbose or report.status != "PASS":
                sys.stdout.write(report.log)
            print(
                f"{report.status:<5} {label[run]:<24} {report.message} "
                f"({report.game_time:.0f}s game, {report.real_time:.0f}s real)",
                flush=True,
            )
            if report.status == "PASS":
                replays[run].unlink(missing_ok=True)

    if not any(run_dir.iterdir()):
        run_dir.rmdir()
    failures = [r for r in reports if r.status != "PASS"]
    print(f"\n{len(reports) - len(failures)}/{len(reports)} passed in {time.monotonic() - started:.0f}s")
    if failures:
        if run_dir.exists():
            print(f"Replays of the failures: {run_dir.relative_to(ROOT)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
