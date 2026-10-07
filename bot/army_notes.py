"""What the army decided, for debugging: counts of zergling decisions (logged with `Army.debug`, which
the scenarios switch on), per-unit notes and per-target notes for the overlay (`bot.overlay`), and reach
map dumps (ZASZ_DUMP_REACH).

All of it is cleared every step (the notes) or read only for logs and drawing, with one exception:
`CHOKE_NOTES` doubles as this step's cache of choke verdicts (`bot.micro.choke_worth`)."""

import os
from collections import Counter

import numpy as np
from ares import AresBot
from sc2.position import Point2
from sc2.unit import Unit

from bot.targeting import ReachMap

# Debug overlay: this step's back-off lines (an enemy's position, how far from it we back off to),
# and each candidate target's priority (its targeting tier, 1 = first) and how many attackers it takes
BACK_OFF_CIRCLES: list[tuple[Point2, float]] = []


TARGET_NOTES: dict[int, tuple[int, int | None]] = {}  # enemy tag -> (priority, capacity)


CHOKE_NOTES: dict[int, tuple[float, float, bool]] = {}  # enemy tag -> (zerglings lost, worth, taken)


CHOKE_OPENERS: dict[int, str] = {}  # enemy tag -> the building to break first to open it up (overlay)


UNIT_NOTES: dict[int, str] = {}  # our unit tag -> its latest decision this step (overlay)


def note(units: list[Unit], text: str) -> None:
    """Record what these units were told to do, and why (the overlay shows it over each one)."""
    for u in units:
        UNIT_NOTES[u.tag] = text


def name_of(e: Unit) -> str:
    return f"{e.type_id.name.lower()}@{e.position.x:.0f},{e.position.y:.0f}"


COUNTS: Counter[str] = Counter()  # debug: zergling decisions since the last log (see Army.debug)


REACH_DUMPED: set[int] = set()


def dump_reach(ai: AresBot, reach: ReachMap, unit: Unit, fighters: list[Unit], enemies: list[Unit]) -> None:
    """Diagnostic (ZASZ_DUMP_REACH=path): save this reach map's view once per minute of game time, to see
    why enemy units count as unreachable."""
    path = os.environ.get("ZASZ_DUMP_REACH")
    minute = int(ai.time // 60)
    if not path or minute in REACH_DUMPED:
        return
    REACH_DUMPED.add(minute)
    np.savez(
        f"{path}-{int(ai.time)}s.npz",
        walkable=reach.walkable,
        reached=reach.reached,
        origin=np.array([reach.x0, reach.y0]),
        zergling=np.array([unit.position.x, unit.position.y]),
        fighters=np.array([[e.position.x, e.position.y, e.radius] for e in fighters]),
        structures=np.array([[e.position.x, e.position.y, e.radius] for e in enemies if e.is_structure]),
        ground=np.isfinite(ai.mediator.get_ground_grid).astype(np.uint8),
        pathing=np.array(ai.game_info.pathing_grid.data_numpy).astype(np.uint8),
    )
