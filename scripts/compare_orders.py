"""Compare two order logs (`run_scenarios.py --order-log DIR`): the commands every scenario sent, line by line.

    uv run python scripts/compare_orders.py before/ after/

For each scenario in both, prints "same" or the first command that differs (with the lines around it).
Exits with 1 if any differ, or if a scenario is in only one of them.
"""

import argparse
import sys
from pathlib import Path


def first_difference(a: list[str], b: list[str]) -> int | None:
    """Index of the first line that differs (or where one log ends early), None if they're the same."""
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()

    before = {p.name: p for p in args.before.glob("*.orders")}
    after = {p.name: p for p in args.after.glob("*.orders")}
    differ = False
    for name in sorted(before.keys() | after.keys()):
        if name not in before or name not in after:
            print(f"{name}: only in {args.before if name in before else args.after}")
            differ = True
            continue
        a, b = before[name].read_text().splitlines(), after[name].read_text().splitlines()
        i = first_difference(a, b)
        if i is None:
            print(f"{name}: same ({len(a)} commands)")
            continue
        differ = True
        print(f"{name}: differs at command {i + 1} of {len(a)} / {len(b)}")
        for label, lines in (("before", a), ("after", b)):
            for line in lines[max(0, i - 2) : i + 3]:
                print(f"  {label}: {line}")
    sys.exit(1 if differ else 0)


if __name__ == "__main__":
    main()
