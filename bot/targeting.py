"""Spreading attackers over targets so their damage actually lands (see `bot.efficiency`).

A melee target can only be hit by as many attackers as fit around it. Sending more just blocks:
the extras stand behind the front line (or at the bottom of a ramp) doing nothing. `TargetSlots`
caps each target at what can use it, so the next attacker picks the next target, deeper in.

The capacity rule depends on the attacker, not on zerglings: melee attackers are limited by space
around the target (a structure: part of the ring, they arrive from one side; a unit: about a full
ring, it gets surrounded); ranged and air attackers (e.g. mutalisks) aren't, so they get no cap
here (an overkill cap, based on the target's remaining health, would go in `attack_capacity`).

`BlockTracker` catches what capacity can't predict: a zergling that stops getting closer to its
target (stuck behind other units, in a cramped base, at a choke) is blocked *for that target*, so it
picks another one it can reach, or goes around.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np
from sc2.position import Point2
from sc2.unit import Unit
from scipy.ndimage import label
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

from bot.settings import (
    APPROACH_DETOUR_SLACK,
    BLOCKED_MEMORY_SECONDS,
    BLOCKED_SECONDS,
    DETOUR_CROWD_COST,
    DETOUR_JAM_COST,
    DETOUR_MAX_EXTRA,
    DETOUR_MAX_FACTOR,
    DETOUR_SECONDS,
    LEVEL_TOLERANCE,
    MELEE_SPACING,
    MELEE_SURROUND_FRACTION,
    MELEE_UNIT_SURROUND_FRACTION,
    PROGRESS_STEP,
    REACH_WINDOW_MARGIN,
    TARGET_MOVED,
)

MELEE_RANGE = 1.0  # weapon range at or below this counts as melee


def is_melee_against(attacker: Unit, target: Unit) -> bool:
    weapon_range = attacker.air_range if target.is_flying else attacker.ground_range
    return weapon_range <= MELEE_RANGE


def attack_capacity(attacker: Unit, target: Unit) -> int | None:
    """How many attackers like `attacker` can usefully hit `target` at once; None = no limit."""
    if not is_melee_against(attacker, target):
        return None  # ranged/air attackers aren't limited by space
    # Attackers stand on a ring around the target: structures mostly from one side, units (which
    # move and get surrounded) from all sides
    fraction = MELEE_SURROUND_FRACTION if target.is_structure else MELEE_UNIT_SURROUND_FRACTION
    ring = 2 * math.pi * (target.radius + attacker.radius) * fraction
    return max(1, int(ring // (2 * attacker.radius * MELEE_SPACING)))


class TargetSlots:
    """Per step: how many attackers each target already has."""

    def __init__(
        self, reach: ReachMap | None = None, caps: dict[int, int] | None = None, taken: dict[int, int] | None = None
    ) -> None:
        # Attackers per target tag. Pass one dict to every squad in a step, so squads fighting
        # the same enemy share its slots (each counting from zero put 20 zerglings on one stalker)
        self.taken: dict[int, int] = {} if taken is None else taken
        self.reach = reach
        self.caps = caps or {}  # target tag -> attackers, overriding the space rule (breaches)

    def has_room(self, attacker: Unit, target: Unit) -> bool:
        if target.tag in self.caps:
            return self.taken.get(target.tag, 0) < self.caps[target.tag]
        capacity = attack_capacity(attacker, target)
        if capacity is not None and self.reach is not None and not target.is_structure and not target.is_memory:
            # A unit plugging a gap only has room for as many attackers as open cells touch it
            capacity = min(capacity, max(1, self.reach.open_sides(target, attacker.radius)))
        return capacity is None or self.taken.get(target.tag, 0) < capacity

    def take(self, target: Unit) -> None:
        self.taken[target.tag] = self.taken.get(target.tag, 0) + 1

    def pick(self, attacker: Unit, candidates: list[Unit], depth_from: Point2 | None = None) -> Unit:
        """Which candidate this attacker should go for.

        Without `depth_from`: the closest one with room. With it (structures, melee): fill from
        the back. Attackers are handled front-first, and each takes the *deepest* candidate (the
        farthest from `depth_from`, the squad's position) that still has room, so the first ones
        in run past the front structures and later ones fill in toward the front: nobody has to
        get past anyone. An attacker keeps its current target while that still has room (the
        squad's position moves as it advances, so re-picking "deepest" every step would send
        attackers back and forth). If every candidate is full: the closest.
        """
        if not candidates:
            raise ValueError("TargetSlots.pick needs at least one candidate")
        by_distance = sorted(candidates, key=lambda e: e.position.distance_to(attacker.position))
        current = next((e for e in candidates if e.tag == attacker.order_target), None)
        if current is not None and self.has_room(attacker, current):
            choice = current  # stick with it until it dies or fills up (re-picking on the way thrashes)
        elif depth_from is not None:
            deepest = sorted(candidates, key=lambda e: -e.position.distance_to(depth_from))
            choice = next((e for e in deepest if self.has_room(attacker, e)), by_distance[0])
        else:
            choice = next((e for e in by_distance if self.has_room(attacker, e)), by_distance[0])
        self.take(choice)
        return choice


@dataclass
class _Progress:
    target: int  # target tag
    best: float  # closest the attacker has been to it (edge to edge)
    since: float  # game time `best` last improved
    target_at: Point2 | None = None  # where the target was at `since`


class BlockTracker:
    """Which attackers are stuck: not getting closer to their target while out of range."""

    def __init__(self) -> None:
        self.progress: dict[int, _Progress] = {}  # attacker tag -> progress on its current target
        self.blocked: dict[int, dict[int, float]] = {}  # attacker tag -> {target tag: blocked until}

    def is_blocked(self, attacker: Unit, target: Unit, now: float) -> bool:
        return self.blocked.get(attacker.tag, {}).get(target.tag, 0) > now

    def reachable(self, attacker: Unit, candidates: list[Unit], now: float) -> list[Unit]:
        """Candidates this attacker hasn't recently been blocked from."""
        return [c for c in candidates if not self.is_blocked(attacker, c, now)]

    def update(self, attacker: Unit, target: Unit, now: float, same_level: bool = True) -> bool:
        """Record progress toward `target`; True if the attacker just became blocked on it.
        `same_level`: on the target's terrain level (close but below a cliff isn't in range)."""
        gap = attacker.position.distance_to(target.position) - attacker.radius - target.radius
        weapon_range = attacker.air_range if target.is_flying else attacker.ground_range
        progress = self.progress.get(attacker.tag)
        in_range = gap <= weapon_range + 0.25 and (same_level or weapon_range > 1)
        if in_range or progress is None or progress.target != target.tag:
            self.progress[attacker.tag] = _Progress(target.tag, gap, now, target.position)  # in range / new
            return False
        if gap < progress.best - PROGRESS_STEP:
            progress.best, progress.since = gap, now
            return False
        if progress.target_at is not None and target.position.distance_to(progress.target_at) > TARGET_MOVED:
            # The target is on the move (kiting, walking off): that's a chase, not being stuck
            progress.best, progress.since, progress.target_at = gap, now, target.position
            return False
        if now - progress.since < BLOCKED_SECONDS:
            return False
        self.blocked.setdefault(attacker.tag, {})[target.tag] = now + BLOCKED_MEMORY_SECONDS
        del self.progress[attacker.tag]
        return True

    def forget_dead(self, alive: set[int]) -> None:
        self.progress = {t: p for t, p in self.progress.items() if t in alive}
        self.blocked = {t: b for t, b in self.blocked.items() if t in alive}


def pick_tiered(
    slots: TargetSlots,
    blocks: BlockTracker,
    attacker: Unit,
    tiers: list[tuple[list[Unit], bool]],
    depth_from: Point2 | None,
    now: float,
) -> tuple[Unit | None, bool]:
    """The best target for `attacker`: the first tier (highest priority) with a reachable target
    that still has room. Each tier is (targets, fill_from_back): fill from the back takes the deepest
    one from `depth_from` (or the closest without one); otherwise the tier's own order is kept.
    An attacker keeps its current target while that
    still has room and isn't blocked.

    Returns (target, flank): flank=True means every target is blocked for this attacker (go around
    the returned one). If every reachable target is full, the closest one in the best tier.
    """
    candidates = [t for tier, _ in tiers for t in tier]
    if not candidates:
        return None, False
    current = next((t for t in candidates if t.tag == attacker.order_target), None)
    best_tier = next(
        (tier for tier, _ in tiers if any(slots.has_room(attacker, e) for e in blocks.reachable(attacker, tier, now))),
        None,
    )
    if (
        current is not None
        and best_tier is not None
        and any(e.tag == current.tag for e in best_tier)
        and slots.has_room(attacker, current)
        and not blocks.is_blocked(attacker, current, now)
    ):
        # Stick with it, but only while it's in the best tier we can reach: a zergling breaking
        # through a gateway switches back to the units as soon as one is reachable
        slots.take(current)
        return current, False
    for tier, back in tiers:
        reachable = blocks.reachable(attacker, tier, now)
        if back and depth_from is not None:
            ordered = sorted(reachable, key=lambda e: -e.position.distance_to(depth_from))
        elif back:
            ordered = sorted(reachable, key=lambda e: e.position.distance_to(attacker.position))
        else:
            ordered = reachable  # already in the caller's preferred order
        if choice := next((e for e in ordered if slots.has_room(attacker, e)), None):
            slots.take(choice)
            return choice, False
    # Everything reachable is full: the closest reachable one in the best tier that has any
    for tier, _ in tiers:
        if reachable := blocks.reachable(attacker, tier, now):
            choice = min(reachable, key=lambda e: e.position.distance_to(attacker.position))
            slots.take(choice)
            return choice, False
    # Blocked from everything: go around the closest
    return min(candidates, key=lambda e: e.position.distance_to(attacker.position)), True


@dataclass
class _Route:
    target: int  # target tag
    waypoints: list[Point2]
    until: float  # give up on the detour after this


class Detours:
    """Routes around our own crowds for blocked attackers.

    Pathing ignores our own units, so a zergling stuck behind others keeps choosing the same jammed
    lane (e.g. a 1-tile gap between gateways). For a blocked attacker we plan on a copy of the
    ground grid where friendly units near it are expensive and the jammed spot even more, so the
    cheapest path to the same target goes around (e.g. around the outside of a gateway grid).
    """

    def __init__(self) -> None:
        self.routes: dict[int, _Route] = {}

    def route_target(self, attacker: Unit, now: float) -> int | None:
        route = self.routes.get(attacker.tag)
        if route is None or route.until < now or not route.waypoints:
            self.routes.pop(attacker.tag, None)
            return None
        return route.target

    def plan(self, ai, attacker: Unit, target: Unit, crowd: list[Unit], now: float) -> bool:
        """Plan a detour to `target` around `crowd`; False if there's no reasonable one."""
        map_data = ai.mediator.get_map_data_object
        grid = ai.mediator.get_ground_grid.copy()
        for unit in crowd:
            grid = map_data.add_cost(unit.position, unit.radius + 0.5, grid, weight=DETOUR_CROWD_COST)
        grid = map_data.add_cost(attacker.position, 2.0, grid, weight=DETOUR_JAM_COST)
        path = ai.mediator.find_raw_path(start=attacker.position, target=target.position, grid=grid, sensitivity=2)
        if not path:
            return False
        length = sum(a.distance_to(b) for a, b in zip([attacker.position, *path], path, strict=False))
        if length > DETOUR_MAX_FACTOR * attacker.position.distance_to(target.position) + DETOUR_MAX_EXTRA:
            return False  # no reasonable way around
        self.routes[attacker.tag] = _Route(target.tag, list(path), now + DETOUR_SECONDS)
        return True

    def next_waypoint(self, attacker: Unit) -> Point2 | None:
        route = self.routes.get(attacker.tag)
        if route is None:
            return None
        while route.waypoints and route.waypoints[0].distance_to(attacker.position) < 1.5:
            route.waypoints.pop(0)
        return route.waypoints[0] if route.waypoints else None

    def drop(self, attacker: Unit) -> None:
        self.routes.pop(attacker.tag, None)

    def forget_dead(self, alive: set[int]) -> None:
        self.routes = {t: r for t, r in self.routes.items() if t in alive}


class ReachMap:
    """Which enemy units our melee attackers can actually get to, right now.

    Pathing ignores units, so a zealot standing in a 1-tile gap between two gateways looks like an
    open lane. This does a breadth-first search over the ground pathing grid around the fight, with
    enemy ground units marked as obstacles, starting from every one of our attackers. A target is
    reachable if an open cell next to it was reached, and the number of such cells is how many melee
    attackers can stand around it (a zealot plugging a gap: one or two).
    """

    def __init__(
        self, grid, own: list[Unit], enemies: list[Unit], height=None, margin: int = REACH_WINDOW_MARGIN
    ) -> None:
        # height(Point2) -> terrain z: cells next to a target only count on the target's level (a
        # zergling below a cliff is close to a building on top of it, but can't hit it)
        self.height = height
        points = [u.position for u in own] + [e.position for e in enemies]
        width, height = grid.shape
        self.x0 = max(0, int(min(p.x for p in points)) - margin)
        self.y0 = max(0, int(min(p.y for p in points)) - margin)
        x1 = min(width, int(max(p.x for p in points)) + margin + 1)
        y1 = min(height, int(max(p.y for p in points)) + margin + 1)
        walkable = np.isfinite(grid[self.x0 : x1, self.y0 : y1])
        for e in enemies:
            if not e.is_structure and not e.is_flying:
                for cell in self._cells_within(e.position, e.radius + 0.1, walkable.shape):
                    walkable[cell] = False
        self.walkable = walkable
        self.reached = np.zeros_like(walkable, dtype=bool)
        queue: deque[tuple[int, int]] = deque()
        for u in own:
            cell = (int(u.position.x) - self.x0, int(u.position.y) - self.y0)
            if 0 <= cell[0] < walkable.shape[0] and 0 <= cell[1] < walkable.shape[1] and not self.reached[cell]:
                self.reached[cell] = True  # start from our units even if the grid says blocked
                queue.append(cell)
        w, h = walkable.shape
        while queue:
            cx, cy = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < w and 0 <= ny < h and walkable[nx, ny] and not self.reached[nx, ny]:
                    if dx and dy and not (walkable[cx + dx, cy] or walkable[cx, cy + dy]):
                        continue  # no squeezing diagonally between two blocked cells
                    self.reached[nx, ny] = True
                    queue.append((nx, ny))
        self._open: dict[int, int] = {}
        self._fields: dict[int, np.ndarray] = {}
        self._near_fields: dict[tuple[int, float], np.ndarray] = {}  # (target, limit) -> capped field
        self._graph = None  # walkable cells as a graph, for the approach fields (built when needed)
        self.breach_caps: dict[int, int] | None = None  # see `Army.breach_caps` (cached with the map)

    def _cells_within(self, center: Point2, radius: float, shape) -> list[tuple[int, int]]:
        cells = []
        for x in range(int(center.x - radius) - 1, int(center.x + radius) + 2):
            for y in range(int(center.y - radius) - 1, int(center.y + radius) + 2):
                if (x + 0.5 - center.x) ** 2 + (y + 0.5 - center.y) ** 2 <= radius**2:
                    lx, ly = x - self.x0, y - self.y0
                    if 0 <= lx < shape[0] and 0 <= ly < shape[1]:
                        cells.append((lx, ly))
        return cells

    def _ring(self, target: Unit, attacker_radius: float) -> list[tuple[int, int]]:
        """Reached cells on the target's level an attacker could stand on to hit it."""
        ring = self._cells_within(target.position, target.radius + 2 * attacker_radius + 0.6, self.walkable.shape)
        level = self.height(target.position) if self.height else None

        def same_level(c: tuple[int, int]) -> bool:
            if level is None:
                return True
            return abs(self.height(Point2((c[0] + self.x0 + 0.5, c[1] + self.y0 + 0.5))) - level) < LEVEL_TOLERANCE

        return [c for c in ring if self.walkable[c] and self.reached[c] and same_level(c)]

    def _neighbors(self, cx: int, cy: int):
        w, h = self.walkable.shape
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < w and 0 <= ny < h and self.walkable[nx, ny]:
                if dx and dy and not (self.walkable[cx + dx, cy] or self.walkable[cx, cy + dy]):
                    continue  # no squeezing diagonally between two blocked cells
                yield nx, ny

    def _cell_graph(self):
        """Walkable cells (enemy units as walls) as a graph: 8 neighbors, no diagonal squeezing."""
        if self._graph is None:
            walk = self.walkable
            w, h = walk.shape
            index = np.arange(w * h).reshape(w, h)
            rows, cols, weights = [], [], []
            for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
                # pairs (x, y) -> (x + dx, y + dy), both inside the window
                xs, xe = 0, w - dx
                ys, ye = max(0, -dy), h - max(0, dy)
                src = (slice(xs, xe), slice(ys, ye))
                dst = (slice(xs + dx, xe + dx), slice(ys + dy, ye + dy))
                ok = walk[src] & walk[dst]
                if dx and dy:
                    # one of the two orthogonal cells in between must be open
                    ok &= (
                        walk[(slice(xs + dx, xe + dx), slice(ys, ye))] | walk[(slice(xs, xe), slice(ys + dy, ye + dy))]
                    )
                rows.append(index[src][ok])
                cols.append(index[dst][ok])
                weights.append(np.full(int(ok.sum()), 1.4142 if dx and dy else 1.0))
            self._graph = coo_matrix(
                (np.concatenate(weights), (np.concatenate(rows), np.concatenate(cols))), shape=(w * h, w * h)
            ).tocsr()
        return self._graph

    def approach_step(self, target: Unit, position: Point2, lookahead: int = 4) -> Point2 | None:
        """Where to move next to get next to `target` walking around enemy units (pathing ignores
        units, so it sends zerglings down a lane a zealot plugs). None if there's no way, we're
        already there, or the way is open (walking there is barely longer than straight there: a
        plain attack order gets there just as well, and keeps following a target that moves).
        Follows a distance field from the open cells around the target."""
        if target.tag not in self._fields:
            seeds = [c[0] * self.walkable.shape[1] + c[1] for c in self._ring(target, 0.375)]
            if seeds:
                dist = dijkstra(self._cell_graph(), directed=False, indices=seeds, min_only=True)
                self._fields[target.tag] = dist.reshape(self.walkable.shape)
            else:
                self._fields[target.tag] = np.full(self.walkable.shape, np.inf)
        field = self._fields[target.tag]
        cx, cy = int(position.x) - self.x0, int(position.y) - self.y0
        w, h = field.shape
        if not (0 <= cx < w and 0 <= cy < h):
            return None
        here = field[cx, cy]
        if not np.isfinite(here):
            # Standing on a cell the grid calls blocked (or cut off): start from the best neighbor
            options = [n for n in self._neighbors(cx, cy) if np.isfinite(field[n])]
            if not options:
                return None
            cx, cy = min(options, key=lambda n: field[n])
            here = field[cx, cy]
        elif here == 0:
            return None  # already next to it
        ring = target.radius + 2 * 0.375 + 0.6  # where the field's zero cells are (see `_ring`)
        if here <= position.distance_to(target.position) - ring + APPROACH_DETOUR_SLACK:
            return None  # nothing in the way
        for _ in range(lookahead):
            if here == 0:
                break
            options = [n for n in self._neighbors(cx, cy) if field[n] < here]
            if not options:
                break
            cx, cy = min(options, key=lambda n: field[n])
            here = field[cx, cy]
        return Point2((cx + self.x0 + 0.5, cy + self.y0 + 0.5))

    def walk_distance(self, target: Unit, position: Point2, limit: float) -> float:
        """How far `position` has to walk to stand next to `target`, going around enemy units
        (inf if there's no way, or it's more than `limit`). Only searches out to `limit`, so it's
        cheap to ask about every enemy nearby."""
        key = (target.tag, limit)
        if key not in self._near_fields:
            seeds = [c[0] * self.walkable.shape[1] + c[1] for c in self._ring(target, 0.375)]
            if seeds:
                dist = dijkstra(self._cell_graph(), directed=False, indices=seeds, min_only=True, limit=limit)
                self._near_fields[key] = dist.reshape(self.walkable.shape)
            else:
                self._near_fields[key] = np.full(self.walkable.shape, np.inf)
        field = self._near_fields[key]
        cx, cy = int(position.x) - self.x0, int(position.y) - self.y0
        if not (0 <= cx < field.shape[0] and 0 <= cy < field.shape[1]):
            return np.inf
        if np.isfinite(field[cx, cy]):
            return float(field[cx, cy])
        # Standing on a cell the grid calls blocked (next to a building, or on top of a unit): one
        # step from the best open neighbor
        return float(min((field[n] + 1 for n in self._neighbors(cx, cy)), default=np.inf))

    def open_sides(self, target: Unit, attacker_radius: float = 0.375) -> int:
        """Reached cells on the target's level an attacker could stand on to hit it (0 = unreachable)."""
        if target.tag not in self._open:
            self._open[target.tag] = len(self._ring(target, attacker_radius))
        return self._open[target.tag]

    def breaches(self, structures: list[Unit], targets: list[Unit]) -> dict[int, tuple[int, list[Unit]]]:
        """Structures whose death opens a way to enemy units we can't get to now: the ones bordering
        both the ground our attackers reach and a sealed-off patch with some of `targets` in it (e.g.
        the back row of a gateway block with stalkers behind it). Per structure: how many reached
        cells border it (attackers that can hit it at once) and the targets it opens up."""
        regions, _ = label(self.walkable, structure=np.ones((3, 3), dtype=bool))
        reached = set(np.unique(regions[self.reached & self.walkable]).tolist()) - {0}
        behind: dict[int, list[Unit]] = {}  # sealed-off region -> targets in it
        for t in targets:
            if t.is_flying or t.is_memory or self.open_sides(t) > 0:
                continue  # reachable already (or not a melee question)
            ring = self._cells_within(t.position, t.radius + 1.5, self.walkable.shape)
            for region in {int(regions[c]) for c in ring if self.walkable[c]} - reached - {0}:
                behind.setdefault(region, []).append(t)
        found: dict[int, tuple[int, list[Unit]]] = {}
        if not behind:
            return found
        for s in structures:
            border = [
                c for c in self._cells_within(s.position, s.radius + 1.2, self.walkable.shape) if self.walkable[c]
            ]
            touching = {int(regions[c]) for c in border}
            opened = [t for region in touching & behind.keys() for t in behind[region]]
            if opened and touching & reached:
                attackers = sum(1 for c in border if self.reached[c] and regions[c] in reached)
                found[s.tag] = (max(1, attackers), opened)
        return found

    def reachable(self, target: Unit) -> bool:
        if target.is_flying or target.is_memory:
            return True  # air isn't a melee problem; remembered units: we don't know where they stand
        return self.open_sides(target) > 0
