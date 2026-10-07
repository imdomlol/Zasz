"""What a benchmark or scenario run records about itself, so its rows say exactly which code ran.

Every row in `benchmarks/*.csv` carries:

- `commit`: the short commit, with "-dirty" if anything but the result CSVs differs from it (the runs
  append to those themselves, so they don't count). "none" outside a git checkout.
- `sc2_build`: the StarCraft II build the games ran on (Windows and Linux builds don't compare).
- `run_id`: one per invocation, so rows of the same run can be told from earlier ones on the same
  commit.

The parent process works these out once (`start_run`) and passes them to its game workers through
the environment (`inherited`), so all of a run's rows agree.

Also here: reading and upgrading the result CSVs, which several machines append to (union-merged).
"""

import argparse
import csv
import os
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Written by the runs themselves: changes here don't make the code dirty
GENERATED = "benchmarks"
ENV = {"commit": "ZASZ_RUN_COMMIT", "sc2_build": "ZASZ_SC2_BUILD", "run_id": "ZASZ_RUN_ID"}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def current_commit(root: Path = ROOT) -> str:
    """Short commit, with "-dirty" if any file but the result CSVs is changed or untracked."""
    try:
        commit = git(root, "rev-parse", "--short", "HEAD")
        dirty = git(root, "status", "--porcelain", "--", ".", f":!{GENERATED}")
    except (OSError, subprocess.CalledProcessError):
        return "none"
    return commit + ("-dirty" if dirty else "")


def sc2_build() -> int:
    """The StarCraft II build python-sc2 will launch (its newest installed version)."""
    from sc2.paths import Paths

    builds = [int(p.name[4:]) for p in (Paths.BASE / "Versions").glob("Base*") if p.name[4:].isdigit()]
    if not builds:
        raise RuntimeError(f"no StarCraft II versions found in {Paths.BASE / 'Versions'}")
    return max(builds)


def new_run_id() -> str:
    """Start time plus a random suffix: two runs started in the same second still differ."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def start_run(build: int | str) -> dict[str, str]:
    """This run's labels, also put in the environment for the game workers it starts."""
    meta = {"commit": current_commit(), "sc2_build": str(build), "run_id": new_run_id()}
    for key, name in ENV.items():
        os.environ[name] = meta[key]
    return meta


def inherited() -> dict[str, str]:
    """The labels `start_run` gave this worker's run; worked out here if it wasn't started that way."""
    if all(name in os.environ for name in ENV.values()):
        return {key: os.environ[name] for key, name in ENV.items()}
    return {"commit": current_commit(), "sc2_build": "", "run_id": ""}


def at_least(minimum: int):
    """argparse type: an int no smaller than `minimum`."""

    def parse(text: str) -> int:
        value = int(text)
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}, got {value}")
        return value

    return parse


def read_rows(path: Path) -> tuple[list[dict], list[str]]:
    """A result CSV's rows and columns. Several machines append to these files and git union-merges them,
    so a file can hold header lines from different versions, in any order (one machine added a column,
    another didn't yet). Columns are only ever added at the end, so every header is the start of the
    longest one: each row is read by position against that, and a row from before a column existed just
    has it blank. Rows are dicts over those columns; header lines are skipped."""
    if not path.exists():
        return [], []
    with path.open(newline="") as f:
        lines = [line for line in csv.reader(f) if line]
    if not lines:
        return [], []
    first = lines[0][0]  # e.g. "timestamp": no data row starts with the column's own name
    headers = [line for line in lines if line[0] == first]
    columns = max(headers, key=len)
    if bad := [h for h in headers if columns[: len(h)] != h]:
        sys.exit(f"{path}: header {','.join(bad[0])} isn't the start of {','.join(columns)}; merge it by hand")
    rows = []
    for number, line in enumerate(lines[1:], start=2):
        if line[0] == first:
            continue
        if len(line) > len(columns):
            sys.exit(f"{path}: row {number} has {len(line)} fields, the longest header {len(columns)}")
        rows.append({name: line[i] if i < len(line) else "" for i, name in enumerate(columns)})
    return rows, columns


def ensure_header(path: Path, fields: list[str]) -> list[dict]:
    """Make the CSV's header `fields` (creating the file if needed) and return its rows.

    New columns go at the end, so older rows (and older copies of the file on other machines) still
    parse; they read as blank. Only the first line is rewritten: old rows stay byte for byte as they were,
    so a union merge with another machine's copy doesn't duplicate them. The new file is written next to
    the original and swapped in, so an interrupted upgrade leaves the original intact. Never drops a
    column."""
    rows, columns = read_rows(path)
    if not path.exists() or not columns:
        path.parent.mkdir(exist_ok=True)
        with path.open("w", newline="") as f:
            csv.writer(f).writerow(fields)
        return []
    with path.open(newline="") as f:
        header = next(csv.reader(f))
    if header == fields and columns == fields:
        return rows
    if dropped := [c for c in columns if c not in fields]:
        sys.exit(f"{path}: the new columns would drop {', '.join(dropped)}; upgrade it by hand")
    if fields[: len(columns)] != columns:
        sys.exit(f"{path}: columns {','.join(columns)} aren't the start of {','.join(fields)}; upgrade it by hand")
    with path.open(newline="") as f:
        text = f.read()
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", newline="") as f:
        csv.writer(f).writerow(fields)
        f.write(text.split("\n", 1)[1] if "\n" in text else "")
    os.replace(temp, path)
    return rows
