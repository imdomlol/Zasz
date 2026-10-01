"""Record (observation, decision) pairs from games, e.g. as training data for a model.

One JSON object per line:

    {"type": "header", "version": 1, "features": [...], "decisions": [...], ...meta}
    {"type": "step", "loop": 448, "obs": {...}, "decision": {...}}
    ...
    {"type": "end", "result": "Victory"}

A step is written once per `every_loops` game loops and whenever the decision changes, so
decision switches are never missed. `read_records` turns a file back into objects.
"""

from __future__ import annotations

import json
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

    def record(self, loop: int, obs: Observation, decision: Decision) -> None:
        due = self._last_loop is None or loop - self._last_loop >= self.every_loops
        if not due and decision == self._last_decision:
            return
        self._write({"type": "step", "loop": loop, "obs": obs.to_dict(), "decision": decision.to_dict()})
        self._last_loop, self._last_decision = loop, decision

    def close(self, result: Result | str | None = None) -> None:
        if self._file is None:
            return
        self._write({"type": "end", "result": getattr(result, "name", result)})
        self._file.close()
        self._file = None

    def _write(self, line: dict) -> None:
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("w")
            header = {"type": "header", "version": VERSION, "features": FEATURE_NAMES, "decisions": DECISION_NAMES}
            self._file.write(json.dumps(header | self.meta) + "\n")
        self._file.write(json.dumps(line) + "\n")


@dataclass
class Recording:
    header: dict
    steps: list[tuple[int, Observation, Decision]]  # (game loop, observation, decision)
    result: str | None  # None if the game didn't finish (e.g. a crash)


def read_records(path: str | Path) -> Recording:
    header, steps, result = {}, [], None
    with Path(path).open() as f:
        for raw in f:
            line = json.loads(raw)
            if line["type"] == "header":
                header = line
            elif line["type"] == "step":
                steps.append((line["loop"], Observation.from_dict(line["obs"]), Decision.from_dict(line["decision"])))
            elif line["type"] == "end":
                result = line["result"]
    return Recording(header, steps, result)
