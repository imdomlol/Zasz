"""Record (observation, decision) pairs from games, e.g. as training data for a model.

One JSON object per line:

    {"type": "header", "version": 1, "features": [...], "decisions": [...], ...meta}
    {"type": "step", "loop": 448, "obs": {...}, "decision": {...}}
    ...
    {"type": "end", "result": "Victory"}

A step is written once per `every_loops` game loops and whenever the decision changes, so
decision switches are never missed. `read_records` turns a file back into objects (`iter_steps`
streams the steps of a long one). Both check the header: a recording from another version, or with
feature names this code doesn't know, is an error rather than silently read as zeros.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bot.decisions import DECISION_NAMES, Decision
from bot.observation import FEATURE_NAMES, Observation

if TYPE_CHECKING:
    from sc2.data import Result

VERSION = 1


class DecisionRecorder:
    def __init__(self, path: str | Path, every_loops: int = 22, meta: dict | None = None):
        self.path = Path(path)
        self.every_loops = every_loops  # 22 loops = 1 game second
        self.meta = meta or {}
        self._file = None
        self._last_loop: int | None = None
        self._last_decision: Decision | None = None
        self._closed = False  # after close(), further records are ignored (they'd truncate the file)

    def record(self, loop: int, obs: Observation, decision: Decision) -> None:
        due = self._last_loop is None or loop - self._last_loop >= self.every_loops
        if not due and decision == self._last_decision:
            return
        self._write({"type": "step", "loop": loop, "obs": obs.to_dict(), "decision": decision.to_dict()})
        self._last_loop, self._last_decision = loop, decision

    def flush(self) -> None:
        """Make everything recorded so far readable, e.g. to check a recording mid-game."""
        if self._file is not None:
            self._file.flush()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self, result: Result | str | None = None) -> None:
        """Write the end record (the game's result) and close the file. Only the first call counts,
        so a runner can call `close("Crash")` after a game that may or may not have ended normally."""
        if self._closed:
            return
        if self._file is not None:
            self._write({"type": "end", "result": getattr(result, "name", result)})
            self._file.close()
            self._file = None
        self._closed = True

    def _write(self, line: dict) -> None:
        if self._closed:
            return
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("w")
            header = {"type": "header", "version": VERSION, "features": FEATURE_NAMES, "decisions": DECISION_NAMES}
            # The structural keys win over caller metadata with the same name
            self._file.write(json.dumps(self.meta | header) + "\n")
        self._file.write(json.dumps(line) + "\n")


@dataclass
class Recording:
    header: dict
    steps: list[tuple[int, Observation, Decision]]  # (game loop, observation, decision)
    result: str | None  # None if the game didn't finish and nobody closed the recording ("Crash" in benchmarks)


def read_records(path: str | Path, skip_unfinished_line: bool = False) -> Recording:
    """The whole recording. `skip_unfinished_line`: ignore a last line cut off mid-write (a game
    killed while recording); otherwise that is an error, like any other unreadable line."""
    header, steps, result = {}, [], None
    for kind, value in _records(path, skip_unfinished_line):
        if kind == "header":
            header = value
        elif kind == "step":
            steps.append(value)
        else:
            result = value
    return Recording(header, steps, result)


def iter_steps(path: str | Path, skip_unfinished_line: bool = False) -> Iterator[tuple[int, Observation, Decision]]:
    """The recording's steps one at a time, without holding them all (for large datasets)."""
    for kind, value in _records(path, skip_unfinished_line):
        if kind == "step":
            yield value


def _records(path: str | Path, skip_unfinished_line: bool) -> Iterator[tuple[str, object]]:
    """("header", dict), then ("step", (loop, Observation, Decision)) per step, then ("end", result) if the
    game finished: checked as they're read (the header's schema, the order of the records, each step's
    names and values). Errors name the file and line."""
    path = Path(path)
    ended = False
    number = 0
    with path.open() as f:
        for number, raw in enumerate(f, start=1):
            where = f"{path}:{number}"
            try:
                line = json.loads(raw)
            except json.JSONDecodeError as e:
                if skip_unfinished_line and not raw.endswith("\n"):
                    return  # the last line, cut off
                raise ValueError(f"{where}: not a JSON line ({e})") from e
            kind = line.get("type") if isinstance(line, dict) else None
            if number == 1:
                check_header(line, where)
                yield "header", line
            elif kind == "header":
                raise ValueError(f"{where}: a second header")
            elif ended:
                raise ValueError(f"{where}: a {kind!r} record after the end record")
            elif kind == "step":
                yield "step", _step(line, where)
            elif kind == "end":
                ended = True
                yield "end", line.get("result")
            else:
                raise ValueError(f"{where}: unknown record type {kind!r}")
    if number == 0:
        raise ValueError(f"{path}: empty (no header)")


def _step(line: dict, where: str) -> tuple[int, Observation, Decision]:
    try:
        unknown = [name for name in line["obs"] if name not in FEATURE_NAMES]
        if unknown:
            raise ValueError(f"unknown features {unknown}")
        return line["loop"], Observation.from_dict(line["obs"]), Decision.from_dict(line["decision"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{where}: bad step ({type(e).__name__}: {e})") from e


def check_header(header: dict, where: str = "header") -> None:
    """A recording this code can read: our version, and only feature and decision names it knows. Fewer
    names are fine (older recordings: missing features take their defaults, missing decision fields
    theirs); unknown ones mean a renamed or removed name, which would otherwise be read as zeros."""
    if not isinstance(header, dict) or header.get("type") != "header":
        raise ValueError(f"{where}: no header line")
    if header.get("version") != VERSION:
        raise ValueError(f"{where}: recording version {header.get('version')!r}, this code reads {VERSION}")
    for key, known in (("features", FEATURE_NAMES), ("decisions", DECISION_NAMES)):
        names = header.get(key)
        if not isinstance(names, list):
            raise ValueError(f"{where}: the header has no {key} list")
        if unknown := [name for name in names if name not in known]:
            raise ValueError(f"{where}: unknown {key} {unknown}")
