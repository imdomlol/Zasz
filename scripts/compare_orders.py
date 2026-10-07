"""Compare two order logs (`run_scenarios.py --order-log DIR`): the commands every scenario sent, line by line.

    uv run python scripts/compare_orders.py before/ after/                   # the repeatable scenarios
    uv run python scripts/compare_orders.py before/ after/ --only tank_dive  # just these
    uv run python scripts/compare_orders.py before/ after/ --all             # every scenario in both

By default only the scenarios that play out the same every run are compared (`scenarios.REPEATABLE`); the
others vary a little on identical code. For each, prints "same" or the first command that differs.

Exits with 1 unless there's complete evidence and it all matches: both runs finished (their manifest says
so), every expected scenario is in both, every log ends with its end line, and something was compared.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scripts.bootstrap  # noqa: F401 (our bot and ares on the import path)

# isort: split

from scenarios import REPEATABLE


def first_difference(a: list[str], b: list[str]) -> int | None:
    """Index of the first line that differs (or where one log ends early), None if they're the same."""
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


def scenario_of(label: str) -> str:
    """`tank_dive[top]#2` -> `tank_dive`."""
    return label.split("[")[0]


def load_run(path: Path) -> tuple[dict | None, str | None]:
    """The run's manifest, or why it can't be used."""
    if not path.is_dir():
        return None, f"{path}: not a directory"
    manifest = path / "manifest.json"
    if not manifest.exists():
        return None, f"{path}: no manifest.json (the run didn't finish, or isn't an order log)"
    data = json.loads(manifest.read_text())
    if not data.get("complete"):
        return None, f"{path}: the run isn't marked complete"
    return data, None


def compare(before: Path, after: Path, only: list[str] | None = None, everything: bool = False) -> list[str]:
    """Problems found (empty = the same, with evidence). Prints a line per scenario as it goes."""
    runs, problems = [], []
    for path in (before, after):
        data, problem = load_run(path)
        runs.append(data)
        if problem:
            problems.append(problem)
    if problems:
        return problems
    labels = [set(run["runs"]) for run in runs]
    if everything:
        wanted = sorted(labels[0] | labels[1])
    else:
        names = set(only or REPEATABLE)
        wanted = sorted(label for label in labels[0] | labels[1] if scenario_of(label) in names)
        missing = names - {scenario_of(label) for label in wanted}
        problems += [f"{name}: in neither run" for name in sorted(missing)]
    same = commands = 0
    for label in wanted:
        name = f"{label}.orders"
        logs = [path / name for path in (before, after)]
        if absent := [str(p) for p in logs if not p.exists()]:
            problems.append(f"{label}: no log in {', '.join(absent)}")
            continue
        a, b = (p.read_text().splitlines() for p in logs)
        if unfinished := [
            str(p) for p, lines in zip(logs, (a, b), strict=True) if not lines or not lines[-1].startswith("# end ")
        ]:
            problems.append(f"{label}: no end line in {', '.join(unfinished)} (the game didn't finish)")
            continue
        i = first_difference(a, b)
        if i is None:
            same += 1
            commands += len(a) - 1
            print(f"{label}: same ({len(a) - 1} commands)")
            continue
        problems.append(f"{label}: differs at line {i + 1}")
        print(f"{label}: differs at line {i + 1} of {len(a)} / {len(b)}")
        for side, lines in (("before", a), ("after", b)):
            for line in lines[max(0, i - 2) : i + 3]:
                print(f"  {side}: {line}")
    if not wanted:
        problems.append("nothing to compare")
    print(f"{same} of {len(wanted)} scenario runs the same, {commands} commands compared")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--only", nargs="+", metavar="SCENARIO", help="Compare just these scenarios")
    which.add_argument("--all", action="store_true", help="Compare every scenario in either run")
    args = parser.parse_args()
    problems = compare(args.before, args.after, args.only, args.all)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
