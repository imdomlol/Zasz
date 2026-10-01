"""Why an expansion can't be reached by ground, when it can't.

ares picks expansions from bases it could path to at game start (rocks and all; e.g. the expedition
gates on Torches AIE wall off a line of bases). During the game a base on that list can still become
unreachable. `ExpansionAccess` works out why:

- "rocks": only neutral rocks are in the way (breakable; the rocks are listed)
- "hostile": enemy structures we've seen are in the way (e.g. a wall-off)
- "walled": our own structures are in the way
- "unreachable": nothing we know of explains it

Answers are cached, and re-checked as soon as anything that can block a path changes (a structure
of ours or the enemy's appears or disappears, or rocks break), or after EXPANSION_RECHECK_SECONDS.
"""

from dataclasses import dataclass, field

import numpy as np
from ares import AresBot
from loguru import logger
from sc2.position import Point2
from sc2.units import Units

from bot.settings import EXPANSION_RECHECK_SECONDS


@dataclass
class Blockage:
    kind: str  # "rocks", "hostile", "walled" or "unreachable"
    checked_at: float
    rocks: Units | None = None  # the rocks to break, when kind == "rocks"
    structures: Units | None = field(default=None, repr=False)  # what's in the way otherwise


class ExpansionAccess:
    """Which expansions we can walk to right now, and why not when we can't."""

    def __init__(self) -> None:
        self.blocked: dict[Point2, Blockage] = {}
        self._checked: dict[Point2, float] = {}  # reachable expansions -> when we last checked
        self._blockers: int | None = None  # fingerprint of everything that can block a path
        self._blockers_loop = -1

    def is_reachable(self, ai: AresBot, location: Point2) -> bool:
        self._forget_if_blockers_changed(ai)
        last = self.blocked.get(location)
        last_time = last.checked_at if last else self._checked.get(location)
        if last_time is not None and ai.time - last_time < EXPANSION_RECHECK_SECONDS:
            return last is None

        blockage = self._find_blockage(ai, location)
        if blockage is None:
            if self.blocked.pop(location, None):
                logger.info(f"Expansion {location.rounded} is reachable again")
            self._checked[location] = ai.time
            return True
        if location not in self.blocked or self.blocked[location].kind != blockage.kind:
            what = f": {blockage.rocks.amount} rocks" if blockage.rocks else ""
            logger.info(f"{ai.time_formatted} expansion {location.rounded} blocked ({blockage.kind}{what})")
        self.blocked[location] = blockage
        return False

    def _forget_if_blockers_changed(self, ai: AresBot) -> None:
        """Drop cached answers when a structure (ours or theirs) or a rock appears or disappears."""
        if self._blockers_loop == ai.state.game_loop:
            return  # already checked this step
        self._blockers_loop = ai.state.game_loop
        blockers = hash(
            (
                frozenset(ai.structures.not_flying.tags),
                frozenset(ai.enemy_structures.not_flying.tags),
                frozenset(ai.destructables.tags),
            )
        )
        if blockers != self._blockers:
            self._blockers = blockers
            self._checked.clear()
            for blockage in self.blocked.values():
                blockage.checked_at = -EXPANSION_RECHECK_SECONDS

    def _find_blockage(self, ai: AresBot, location: Point2) -> Blockage | None:
        map_data = ai.mediator.get_map_data_object
        start = ai.townhalls.closest_to(location).position if ai.townhalls else ai.start_location
        # A fresh grid: ares only refreshes its own every 8 steps, so a structure that just
        # appeared might not be in `mediator.get_ground_grid` yet
        if map_data.pathfind(start, location, map_data.get_pyastar_grid()) is not None:
            return None

        # Rocks counted as open ground, structures still solid: if that works, only rocks are in the way
        without_rocks = map_data.get_pyastar_grid(include_destructables=False)
        if path := map_data.pathfind(start, location, without_rocks):
            # "Unbuildable" plates/bricks are floor tiles that don't block ground units
            rocks = ai.destructables.filter(lambda d: "UNBUILDABLE" not in d.type_id.name)
            return Blockage("rocks", ai.time, rocks=on_path(rocks, path))

        # Otherwise structures are involved: see whose are on the terrain-only route
        terrain = map_data.pather.get_base_pathing_grid(include_destructables=False)
        terrain = np.where(terrain != 0, 1, np.inf).astype(np.float32)
        if path := map_data.pathfind(start, location, terrain):
            if enemy := on_path(ai.enemy_structures.not_flying, path):
                return Blockage("hostile", ai.time, structures=enemy)
            # (not the hatchery the path starts from)
            own = ai.structures.not_flying.filter(lambda s: s.position.distance_to(start) > 3)
            if own := on_path(own, path):
                return Blockage("walled", ai.time, structures=own)
        return Blockage("unreachable", ai.time)


def on_path(units: Units, path: list[Point2]) -> Units:
    """The units whose footprint touches `path`."""
    if not units or not path:
        return units.subgroup([])
    points = np.array(path)
    return units.filter(
        lambda u: np.min(np.hypot(points[:, 0] - u.position.x, points[:, 1] - u.position.y)) < u.radius + 1
    )
