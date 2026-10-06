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
    BREACH_DANGER_COST,
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
    SLOT_HOLD_GAP,
    SLOT_TAKE_OVER_MARGIN,
    TARGET_MOVED,
)

MELEE_RANGE = 1.0  # weapon range at or below this counts as melee
SPOT_STEP = 0.025  # attack spots: how finely a target's edge is sampled (tiles of arc)


def is_melee_against(attacker: Unit, target: Unit) -> bool:
    weapon_range = attacker.air_range if target.is_flying else attacker.ground_range
    return weapon_range <= MELEE_RANGE


def melee_width(attacker_radius: float) -> float:
    """Room along a target's edge one melee attacker takes: its width, MELEE_SPACING apart (they never pack
    perfectly)."""
    return 2 * attacker_radius * MELEE_SPACING


def hit_ring(target: Unit, attacker_radius: float, attacker_range: float) -> float:
    """How far from `target`'s center a melee attacker stands to hit it: SC2 measures range edge to edge
    (both radii), and it stands about half its range in."""
    return target.radius + attacker_radius + min(attacker_range, MELEE_RANGE) / 2


def melee_share(attacker: Unit, target: Unit) -> float:
    """The angle (radians) of the way round `target` one melee attacker like this takes. Attackers of
    different sizes share a target by angle: a big one stands further out and takes more of it."""
    return melee_width(attacker.radius) / hit_ring(target, attacker.radius, attacker.ground_range)


def spot_key(p: Point2) -> tuple[float, float]:
    """An attack spot as a key (`TargetSlots.cells_taken`): spots taken this step aren't handed out again."""
    return round(p.x, 2), round(p.y, 2)


def attack_capacity(attacker: Unit, target: Unit) -> int | None:
    """How many attackers like `attacker` can usefully hit `target` at once; None = no limit."""
    if not is_melee_against(attacker, target):
        return None  # ranged/air attackers aren't limited by space
    # Attackers stand on a ring around the target: structures mostly from one side, units (which
    # move and get surrounded) from all sides
    fraction = MELEE_SURROUND_FRACTION if target.is_structure else MELEE_UNIT_SURROUND_FRACTION
    ring = 2 * math.pi * (target.radius + attacker.radius) * fraction
    return max(1, int(ring // melee_width(attacker.radius)))


class Assignments:
    """Which target each attacker went for, kept across steps (`Army` keeps one). Slots are handed out
    fresh every step, so without this an attacker walking around others (a move order, no target to
    read back) re-picked from scratch, found its slot taken and switched, back and forth. Each step
    (`start`), every attacker that picked a target last step has its slot reserved until it's handled;
    newcomers only get slots that are actually free.

    Room is kept as angle round each target (`melee_share`), so attackers of different sizes share it:
    a target with room for 7 zerglings has room for fewer big ones.

    A slot held by an attacker still far from its target (more than SLOT_HOLD_GAP, stuck behind others)
    goes to a closer one that wants it (`TargetSlots.take`): a zergling stuck 5 tiles behind the bays
    held a bunker slot for seconds while the ones next to it worked the bays."""

    def __init__(self) -> None:
        self.target: dict[int, int] = {}  # attacker tag -> target tag
        self.picked_at: dict[int, float] = {}  # attacker tag -> when it last picked
        self.share: dict[int, float] = {}  # attacker tag -> its share of its target (`melee_share`)
        self.reserved: dict[int, float] = {}  # target tag -> shares reserved, not yet handled this step
        self.used: dict[int, float] = {}  # target tag -> shares taken this step (`TargetSlots.take`)
        self.handled: set[int] = set()  # attackers handled this step (their reservation released)
        self.gap: dict[int, float] = {}  # attacker tag -> how far from its target (edge to edge) when it picked
        self.taken_over = 0  # slots given to a closer attacker (`TargetSlots.take`), for the debug log
        self.now = 0.0
        self.recent = 0.5
        self.deferred: set[int] = set()  # targets not attacked head on for now (a widening plan, `Army`)
        self.holders: dict[int, set[int]] = {}  # target tag -> attackers whose target it is (`far_holder`)

    def start(self, now: float, alive: set[int], recent: float = 0.5) -> None:
        """New step: forget dead attackers; reserve the targets of those that picked one recently."""
        self.target = {a: t for a, t in self.target.items() if a in alive}
        self.picked_at = {a: w for a, w in self.picked_at.items() if a in self.target}
        self.share = {a: s for a, s in self.share.items() if a in self.target}
        self.gap = {a: g for a, g in self.gap.items() if a in self.target}
        self.holders = {}
        for attacker, target in self.target.items():
            self.holders.setdefault(target, set()).add(attacker)
        self.now, self.recent = now, recent
        self.reserved = {}
        self.used = {}
        for attacker, target in self.target.items():
            if now - self.picked_at.get(attacker, -99) <= recent:
                self.reserved[target] = self.reserved.get(target, 0.0) + self.share.get(attacker, 0.0)
        self.handled = set()

    def release(self, attacker: Unit) -> None:
        """This attacker is being handled now: its reservation becomes an ordinary pick (or not)."""
        if attacker.tag in self.handled:
            return
        self.handled.add(attacker.tag)
        target = self.target.get(attacker.tag)
        if target is not None and target in self.reserved:
            self.reserved[target] = max(0.0, self.reserved[target] - self.share.get(attacker.tag, 0.0))

    def assign(self, attacker: Unit, target: Unit, now: float) -> None:
        if (old := self.target.get(attacker.tag)) is not None:
            self.holders[old].discard(attacker.tag)
        self.holders.setdefault(target.tag, set()).add(attacker.tag)
        self.target[attacker.tag] = target.tag
        self.picked_at[attacker.tag] = now
        self.share[attacker.tag] = melee_share(attacker, target)
        self.gap[attacker.tag] = edge_gap(attacker, target)

    def holds(self, attacker: int, target: int) -> bool:
        """Whether `attacker` counts against `target`'s room this step (took it, or holds it from last step)."""
        if self.target.get(attacker) != target:
            return False
        picked = self.picked_at.get(attacker, -99)
        return picked == self.now if attacker in self.handled else self.now - picked <= self.recent

    def far_holder(self, attacker: Unit, target: Unit) -> int | None:
        """The attacker holding a slot on `target` from furthest away, if it's further than SLOT_HOLD_GAP
        and SLOT_TAKE_OVER_MARGIN further than `attacker`: that slot is better used by `attacker`."""
        own = edge_gap(attacker, target)
        far = [
            (g, a)
            for a in self.holders.get(target.tag, ())
            if a in self.gap
            and a != attacker.tag
            and (g := self.gap[a]) > SLOT_HOLD_GAP
            and g > own + SLOT_TAKE_OVER_MARGIN
            and self.holds(a, target.tag)
        ]
        return max(far)[1] if far else None

    def drop(self, attacker: int, target: int) -> None:
        """Take `attacker`'s slot on `target` away (a closer attacker gets it); it picks again when handled."""
        share = self.share.get(attacker, 0.0)
        if attacker in self.handled:
            self.used[target] = max(0.0, self.used.get(target, 0.0) - share)
        else:
            self.reserved[target] = max(0.0, self.reserved.get(target, 0.0) - share)
        del self.target[attacker]
        self.holders[target].discard(attacker)
        self.picked_at.pop(attacker, None)
        self.taken_over += 1


def edge_gap(attacker: Unit, target: Unit) -> float:
    """How far `attacker` is from `target`, edge to edge."""
    return attacker.position.distance_to(target.position) - attacker.radius - target.radius


class TargetSlots:
    """Per step: how many attackers each target already has."""

    def __init__(
        self,
        reach: ReachMap | None = None,
        caps: dict[int, int] | None = None,
        taken: dict[int, int] | None = None,
        settled: dict[tuple[int, float], int | None] | None = None,
        cells: set[tuple[float, float]] | None = None,
        assignments: Assignments | None = None,
    ) -> None:
        # Attackers per target tag. Pass one dict to every squad in a step, so squads fighting
        # the same enemy share its slots (each counting from zero put 20 zerglings on one stalker)
        self.taken: dict[int, int] = {} if taken is None else taken
        self.reach = reach
        self.caps = caps or {}  # target tag -> attackers, overriding the space rule (breaches)
        # (target tag, attacker radius) -> its capacity this step, shared like `taken`: the first squad to
        # work it out settles it (squads' reach maps cover different areas and can disagree by a spot or two)
        self.settled: dict[tuple[int, float], int | None] = {} if settled is None else settled
        self.strike: set[int] = set()  # enemy tags this attacker's strike goes for (no distance limit)
        # Structure tag -> the safe spots (outside every enemy's reach) to hit it from (`Army` fills it per
        # zergling); its capacity is that many. Spots taken this step (`spot_key`) are shared like `taken`
        self.safe_cells: dict[int, list[Point2]] = {}
        self.cells_taken: set[tuple[float, float]] = set() if cells is None else cells
        # Structure tag -> its "conditionally free" spots: in reach only of enemies already busy with our
        # attackers (`army.covered_work`); its capacity is that many
        self.covered: dict[int, list[Point2]] = {}
        self.assignments = assignments if assignments is not None else Assignments()

    def capacity(self, attacker: Unit, target: Unit) -> int | None:
        """How many attackers like `attacker` `target` takes at once (None: no limit)."""
        if target.tag in self.caps:
            return self.caps[target.tag]
        for spots in (self.safe_cells, self.covered):
            if target.tag in spots:
                capacity = attack_capacity(attacker, target)
                return len(spots[target.tag]) if capacity is None else min(capacity, len(spots[target.tag]))
        key = (target.tag, round(attacker.radius, 3))
        if key in self.settled:
            return self.settled[key]
        capacity = attack_capacity(attacker, target)
        if capacity is not None and self.reach is not None and not target.is_memory:
            # Only as many attackers as fit next to it on ground we reach: a unit plugging a gap gets one
            # or two; a building in a wall only its open faces (a bunker with one face open had 10 slots,
            # the rest crowded behind the front row in its fire)
            capacity = self.reach.attack_slots(target, attacker.radius, capacity, attacker.ground_range)
        self.settled[key] = capacity
        return capacity

    def has_room(self, attacker: Unit, target: Unit) -> bool:
        """Room for one more, counting attackers that kept this target from last step and haven't been
        handled yet (their slot is held for them). By angle round the target (`melee_share`): with one
        attacker size that's just "fewer than capacity"; mixed sizes share the same room."""
        capacity = self.capacity(attacker, target)
        if capacity is None:
            return True
        return self._fits(attacker, target, capacity) or self.assignments.far_holder(attacker, target) is not None

    def _fits(self, attacker: Unit, target: Unit, capacity: int) -> bool:
        share = melee_share(attacker, target)
        held = self.assignments.reserved.get(target.tag, 0.0)
        used = self.assignments.used.get(target.tag, 0.0)
        return used + held + share <= capacity * share + 1e-6

    def busy(self, tag: int) -> bool:
        """Some of our attackers are on this enemy this step (or kept it from last step): it's shooting them."""
        return self.taken.get(tag, 0) > 0 or self.assignments.reserved.get(tag, 0.0) > 0

    def take(self, target: Unit, attacker: Unit) -> None:
        capacity = self.capacity(attacker, target)
        # Full: the slot of an attacker still far away is this closer one's (`Assignments.far_holder`)
        if (
            capacity is not None
            and not self._fits(attacker, target, capacity)
            and (far := self.assignments.far_holder(attacker, target)) is not None
        ):
            self.assignments.drop(far, target.tag)
            self.taken[target.tag] = max(0, self.taken.get(target.tag, 0) - 1)
        self.taken[target.tag] = self.taken.get(target.tag, 0) + 1
        used = self.assignments.used
        used[target.tag] = used.get(target.tag, 0.0) + melee_share(attacker, target)


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
    # Its target from last step (kept by `Assignments`: a zergling walking around others has a move
    # order, so its game order doesn't say what it was going for)
    slots.assignments.release(attacker)
    kept = slots.assignments.target.get(attacker.tag, attacker.order_target)
    current = next((t for t in candidates if t.tag == kept), None)
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
        slots.take(current, attacker)
        slots.assignments.assign(attacker, current, now)
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
            slots.take(choice, attacker)
            slots.assignments.assign(attacker, choice, now)
            return choice, False
    # Everything reachable is full: the closest reachable one in the best tier that has any
    for tier, _ in tiers:
        if reachable := blocks.reachable(attacker, tier, now):
            choice = min(reachable, key=lambda e: e.position.distance_to(attacker.position))
            slots.take(choice, attacker)
            slots.assignments.assign(attacker, choice, now)
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
        static = walkable.copy()
        for e in enemies:
            if not e.is_structure and not e.is_flying:
                for cell in self._cells_within(e.position, e.radius + 0.1, walkable.shape):
                    walkable[cell] = False
        self.walkable = walkable
        # Cells blocked by an enemy unit (not terrain or a building). SC2 lets units through the corner gap
        # between two buildings that only touch diagonally (walls must touch edge to edge); a diagonal
        # step is only refused next to an enemy unit
        self.unit_blocked = static & ~walkable
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
                    if dx and dy and self._squeezed(cx, cy, dx, dy):
                        continue
                    self.reached[nx, ny] = True
                    queue.append((nx, ny))
        self._open: dict[int, int] = {}
        self._fields: dict[int, np.ndarray] = {}
        self._group_fields: dict[frozenset[int], np.ndarray] = {}  # targets -> walking distance field
        self._plugs: set[int] | None = None  # see `plugs`
        # breach structure tag -> the cells our attackers stand on to hit it (`breaches`)
        self.attack_cells: dict[int, list[Point2]] = {}
        self._graph = None  # walkable cells as a graph, for the approach fields (built when needed)
        # Danger (cells some armed enemy can hit), set per step by `set_danger`; safe cells as areas
        self.danger = np.zeros_like(walkable, dtype=bool)
        self.danger_stamp: float | None = None
        self.danger_zones: list[tuple[Point2, float]] = []  # as given to `set_danger`
        # Work derived from this step's enemy positions (e.g. `army.covered_work`'s covering enemies per
        # spot), cleared with each new danger stamp: computed once per step, not once per zergling
        self.step_cache: dict[tuple, object] = {}
        self.safe_labels = np.zeros(walkable.shape, dtype=np.int32)
        self._safe_graph = None
        self._safe_fields: dict[tuple[int, int], np.ndarray] = {}
        self._breach_fields: dict[tuple[int, int], np.ndarray] = {}
        self._cell_centers: list[np.ndarray] | None = None  # map x, y of every cell's center (`set_danger`)
        self._spots: dict[tuple, list[Point2]] = {}  # see `attack_spots`

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

    def attack_spots(
        self, target: Unit, attacker_radius: float, attacker_range: float = 0.1, reached: bool = True
    ) -> list[Point2]:
        """Where melee attackers this size stand to hit `target`, packed along its edge: points on the
        circle they hit it from (`hit_ring`) on walkable ground on its level (and reached by us, unless
        `reached` is False), `melee_width` apart along it. Not whole tiles: zerglings are 0.75 wide, so a
        3-tile face fits more than 3; a building in a wall gets only its open faces; big attackers get
        fewer, wider spots."""
        key = (target.tag, round(attacker_radius, 3), round(min(attacker_range, MELEE_RANGE), 3), reached)
        if key in self._spots:
            return self._spots[key]
        ring = hit_ring(target, attacker_radius, attacker_range)
        n = max(16, int(2 * math.pi * ring / SPOT_STEP))
        arc = 2 * math.pi * ring / n  # between samples
        # All samples at once (hundreds per target, and maps are rebuilt often)
        angles = 2 * np.pi * np.arange(n) / n
        xs = target.position.x + ring * np.cos(angles)
        ys = target.position.y + ring * np.sin(angles)
        cx = np.floor(xs).astype(int) - self.x0
        cy = np.floor(ys).astype(int) - self.y0
        w, h = self.walkable.shape
        valid = (cx >= 0) & (cx < w) & (cy >= 0) & (cy < h)
        inside = np.nonzero(valid)[0]
        open_ground = self.walkable if not reached else self.walkable & self.reached
        valid[inside] = open_ground[cx[inside], cy[inside]]
        if self.height:
            # On the target's level, by tile (terrain height is per tile)
            level = self.height(target.position)
            for c in {(int(cx[i]), int(cy[i])) for i in np.nonzero(valid)[0]}:
                if abs(self.height(self.point(c)) - level) >= LEVEL_TOLERANCE:
                    valid[(cx == c[0]) & (cy == c[1])] = False

        def at(i: int) -> Point2:
            return Point2((float(xs[i]), float(ys[i])))

        spots: list[Point2] = []
        if valid.all():
            # Open all round: as many as fit, evenly spaced
            k = max(1, int(2 * math.pi * ring // melee_width(attacker_radius)))
            spots = [at(int(i * n / k)) for i in range(k)]
        elif valid.any():
            # Greedy, one spot every `melee_width` of arc, starting where an open stretch begins, so each
            # stretch is packed from its end
            start = int(np.nonzero(valid & ~np.roll(valid, 1))[0][0])
            gap = math.ceil(melee_width(attacker_radius) / arc - 1e-9)
            first = last = None
            for k in np.sort((np.nonzero(valid)[0] - start) % n).tolist():
                if last is None or k - last >= gap:
                    spots.append(at((start + k) % n))
                    first = k if first is None else first
                    last = k
            if len(spots) > 1 and n - last + first < gap:
                spots.pop()  # the last one would crowd the first, round the back
        self._spots[key] = spots
        return spots

    def plugs(self, units: list[Unit], targets: list[Unit]) -> set[int]:
        """Of `units`, the ones blocking our way to some of `targets` (killing it would connect us to
        enemies we can't reach now: `breaches` with units as the blockers). Cached with the map."""
        if self._plugs is None:
            self._plugs = set(self.breaches(units, targets))
        return self._plugs

    def _neighbors(self, cx: int, cy: int):
        w, h = self.walkable.shape
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < w and 0 <= ny < h and self.walkable[nx, ny]:
                if dx and dy and self._squeezed(cx, cy, dx, dy):
                    continue
                yield nx, ny

    def _squeezed(self, cx: int, cy: int, dx: int, dy: int) -> bool:
        """A diagonal step between two blocked cells, one of them blocked by an enemy unit. Between two
        buildings (or terrain) touching only at a corner it's allowed: SC2 units walk through that gap."""
        a, b = (cx + dx, cy), (cx, cy + dy)
        if self.walkable[a] or self.walkable[b]:
            return False
        return bool(self.unit_blocked[a] or self.unit_blocked[b])

    def cell(self, p: Point2) -> tuple[int, int] | None:
        """The map cell `p` is in (None outside the window)."""
        c = (int(p.x) - self.x0, int(p.y) - self.y0)
        w, h = self.walkable.shape
        return c if 0 <= c[0] < w and 0 <= c[1] < h else None

    def point(self, c: tuple[int, int]) -> Point2:
        return Point2((c[0] + self.x0 + 0.5, c[1] + self.y0 + 0.5))

    def attack_points(self, target: Unit, attacker_radius: float = 0.375) -> list[Point2]:
        """The reached cells an attacker can stand on to hit `target` (as points)."""
        return [self.point(c) for c in self._ring(target, attacker_radius)]

    def set_danger(self, zones: list[tuple[Point2, float]], stamp: float) -> None:
        """Mark the cells within each (center, radius) zone as danger (once per `stamp`, e.g. per step)."""
        if self.danger_stamp == stamp:
            return
        same = self.danger_stamp is not None and list(zones) == list(self.danger_zones)
        self.danger_stamp = stamp
        self.danger_zones = zones
        self.step_cache = {}
        if same:
            # Nobody moved (a held wall, a sieged tank): the danger, safe areas and the fields built on them
            # are still right
            return
        if self._cell_centers is None:
            w, h = self.walkable.shape
            xs = np.arange(w) + self.x0 + 0.5
            ys = np.arange(h) + self.y0 + 0.5
            self._cell_centers = np.meshgrid(xs, ys, indexing="ij")
        gx, gy = self._cell_centers
        danger = np.zeros(self.walkable.shape, dtype=bool)
        for center, radius in zones:
            danger |= (gx - center.x) ** 2 + (gy - center.y) ** 2 <= radius * radius
        self.danger = danger
        self.safe_labels, _ = label(self.walkable & ~danger, structure=np.ones((3, 3), dtype=bool))
        self._safe_graph = None
        self._safe_fields = {}
        self._breach_fields = {}

    def safe_area(self, p: Point2) -> int:
        """Which connected patch of safe ground `p` is on (0: in danger, blocked or off the map)."""
        c = self.cell(p)
        return 0 if c is None else int(self.safe_labels[c])

    def safe_attack_spots(
        self, target: Unit, attacker_radius: float, area: int, attacker_range: float = 0.1
    ) -> list[Point2]:
        """`attack_spots` outside every enemy's reach, in safe `area`. Which spots are out of reach is
        worked out once per danger stamp (`step_cache`); per attacker only its safe area is checked."""
        if not area:
            return []
        key = ("safe spots", target.tag, attacker_radius, attacker_range)
        spots = self.step_cache.get(key)
        if spots is None:
            spots = [
                (p, int(self.safe_labels[self.cell(p)]))
                for p in self.attack_spots(target, attacker_radius, attacker_range)
                if not self.in_danger(p)
            ]
            self.step_cache[key] = spots
        return [p for p, spot_area in spots if spot_area == area]

    def in_danger(self, p: Point2) -> bool:
        """`p` itself inside a danger zone (`set_danger`): a spot can sit in a safe tile's corner and still
        be in reach."""
        return any(p.distance_to(center) <= radius for center, radius in self.danger_zones)

    def attack_slots(self, target: Unit, attacker_radius: float, ring: int, attacker_range: float = 0.1) -> int:
        """How many attackers fit next to `target` from the ground we reach: a unit by its open sides, a
        building by its `attack_spots` (up to a full ring around it, not `ring`'s one-sided guess). At
        least 1."""
        if target.is_structure:
            full = max(1, int(2 * math.pi * (target.radius + attacker_radius) // melee_width(attacker_radius)))
            return max(1, min(full, len(self.attack_spots(target, attacker_radius, attacker_range))))
        return max(1, min(ring, self.open_sides(target, attacker_radius)))

    def breach_step(self, goal: tuple[int, int], position: Point2, lookahead: int = 4) -> Point2 | None:
        """Where to move next to reach cell `goal` (next to a breach target) with the least time in danger:
        danger cells cost BREACH_DANGER_COST times a normal step (except the last couple of tiles, the
        breach's own cost), so the way goes around their reach where it can, gets out of it the quickest
        way from inside, and crosses it the shortest way when it must. None if `goal` can't be walked to."""
        if goal not in self._breach_fields:
            w, h = self.walkable.shape
            gx, gy = np.meshgrid(np.arange(w), np.arange(h), indexing="ij")
            near_goal = (gx - goal[0]) ** 2 + (gy - goal[1]) ** 2 <= 2.5**2
            cost = np.where(self.danger & ~near_goal, BREACH_DANGER_COST, 1.0)
            dist = dijkstra(self._build_graph(self.walkable, cost), directed=False, indices=goal[0] * h + goal[1])
            self._breach_fields[goal] = dist.reshape(self.walkable.shape)
        field = self._breach_fields[goal]
        here_cell = self.cell(position)
        if here_cell is None or not np.isfinite(field[here_cell]):
            return None
        return self.point(self._descend(field, here_cell, lookahead))

    def safe_spot_count(self, target: Unit, attacker_radius: float, attacker_range: float = 0.1) -> int:
        """How many of `target`'s `attack_spots` are outside every enemy's reach."""
        return sum(1 for p in self.attack_spots(target, attacker_radius, attacker_range) if not self.in_danger(p))

    def safe_step(self, goal: tuple[int, int], position: Point2, lookahead: int = 4) -> Point2 | None:
        """Where to move next to reach cell `goal` without entering danger. None if there's no such way."""
        if goal not in self._safe_fields:
            if self._safe_graph is None:
                self._safe_graph = self._build_graph(self.walkable & ~self.danger)
            dist = dijkstra(self._safe_graph, directed=False, indices=goal[0] * self.walkable.shape[1] + goal[1])
            self._safe_fields[goal] = dist.reshape(self.walkable.shape)
        field = self._safe_fields[goal]
        here_cell = self.cell(position)
        if here_cell is None or not np.isfinite(field[here_cell]):
            return None
        return self.point(self._descend(field, here_cell, lookahead))

    def _descend(self, field: np.ndarray, cell: tuple[int, int], lookahead: int) -> tuple[int, int]:
        """Walk downhill on a distance `field` from `cell` (always to the lowest neighbor, `_neighbors`), up
        to `lookahead` cells or until it can't go lower: where to head next."""
        cx, cy = cell
        here = field[cx, cy]
        for _ in range(lookahead):
            if here == 0:
                break
            options = [n for n in self._neighbors(cx, cy) if field[n] < here]
            if not options:
                break
            cx, cy = min(options, key=lambda n: field[n])
            here = field[cx, cy]
        return cx, cy

    def _cell_graph(self):
        """Walkable cells (enemy units as walls) as a graph: 8 neighbors (see `_squeezed`)."""
        if self._graph is None:
            self._graph = self._build_graph(self.walkable)
        return self._graph

    def _build_graph(self, walk: np.ndarray, cost: np.ndarray | None = None):
        """Cells where `walk` is True as a graph (`build_graph`), with this map's enemy units for the
        diagonal rule (`_squeezed`)."""
        return build_graph(walk, self.unit_blocked, cost)

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
        return self.point(self._descend(field, (cx, cy), lookahead))

    def group_field(self, targets: list[Unit]) -> np.ndarray:
        """Walking distance from every cell to a spot next to the nearest of `targets` (around enemy
        units; inf where there's no way)."""
        key = frozenset(t.tag for t in targets)
        if key not in self._group_fields:
            seeds = sorted({c[0] * self.walkable.shape[1] + c[1] for t in targets for c in self._ring(t, 0.375)})
            if seeds:
                dist = dijkstra(self._cell_graph(), directed=False, indices=seeds, min_only=True)
                self._group_fields[key] = dist.reshape(self.walkable.shape)
            else:
                self._group_fields[key] = np.full(self.walkable.shape, np.inf)
        return self._group_fields[key]

    def walk_to(self, targets: list[Unit], position: Point2) -> float:
        """How far `position` has to walk to stand next to one of `targets` (inf: no way)."""
        field = self.group_field(targets)
        cx, cy = int(position.x) - self.x0, int(position.y) - self.y0
        if not (0 <= cx < field.shape[0] and 0 <= cy < field.shape[1]):
            return np.inf
        if np.isfinite(field[cx, cy]):
            return float(field[cx, cy])
        return float(min((field[n] + 1 for n in self._neighbors(cx, cy)), default=np.inf))

    def gather_spot(self, targets: list[Unit], shooters: list[tuple[Point2, float]]) -> Point2 | None:
        """Where to gather before going for `targets` together: the cell closest to them (walking)
        that's outside every shooter's reach (`shooters`: position and reach). None if none is."""
        field = self.group_field(targets)
        w, h = field.shape
        xs, ys = np.meshgrid(np.arange(w) + self.x0 + 0.5, np.arange(h) + self.y0 + 0.5, indexing="ij")
        safe = np.isfinite(field) & self.walkable
        for spot, reach in shooters:
            safe &= (xs - spot.x) ** 2 + (ys - spot.y) ** 2 > reach**2
        if not safe.any():
            return None
        cell = np.unravel_index(np.argmin(np.where(safe, field, np.inf)), field.shape)
        return Point2((cell[0] + self.x0 + 0.5, cell[1] + self.y0 + 0.5))

    def safe_ground(self, shooters: list[tuple[Point2, float]]) -> bool:
        """Is any ground our attackers can reach outside every shooter's reach (`shooters`: position
        and reach)? False: cornered."""
        w, h = self.walkable.shape
        xs, ys = np.meshgrid(np.arange(w) + self.x0 + 0.5, np.arange(h) + self.y0 + 0.5, indexing="ij")
        safe = self.reached & self.walkable
        for spot, reach in shooters:
            safe &= (xs - spot.x) ** 2 + (ys - spot.y) ** 2 > reach**2
        return bool(safe.any())

    def open_sides(self, target: Unit, attacker_radius: float = 0.375) -> int:
        """Reached cells on the target's level an attacker could stand on to hit it (0 = unreachable)."""
        if target.tag not in self._open:
            self._open[target.tag] = len(self._ring(target, attacker_radius))
        return self._open[target.tag]

    def breaches(
        self, structures: list[Unit], targets: list[Unit], attacker_radius: float = 0.375, attacker_range: float = 0.1
    ) -> dict[int, tuple[int, list[Unit]]]:
        """Structures whose death opens a way to enemy units we can't get to now: the ones bordering
        both the ground our attackers reach and a sealed-off patch with some of `targets` in it (e.g.
        the back row of a gateway block with stalkers behind it). Per structure: how many attackers this
        size can hit it at once (its `attack_spots`) and the targets it opens up."""
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
        self.attack_cells = {}
        if not behind:
            return found
        for s in structures:
            border = [
                c for c in self._cells_within(s.position, s.radius + 1.2, self.walkable.shape) if self.walkable[c]
            ]
            touching = {int(regions[c]) for c in border}
            opened = [t for region in touching & behind.keys() for t in behind[region]]
            if opened and touching & reached:
                spots = self.attack_spots(s, attacker_radius, attacker_range)
                self.attack_cells[s.tag] = spots
                found[s.tag] = (max(1, len(spots)), opened)
        return found

    def reachable(self, target: Unit) -> bool:
        if target.is_flying or target.is_memory:
            return True  # air isn't a melee problem; remembered units: we don't know where they stand
        return self.open_sides(target) > 0


class RouteMap:
    """Map-wide routes around danger: the walkable map with every danger zone (center, radius) as a
    wall. A destination is only worth travelling to if a route reaches it; squads then follow that
    route (skirting a sieged tank's range) instead of ares' path, which treats danger as extra cost
    and goes straight through when there's no cheap way round."""

    def __init__(self, grid: np.ndarray, zones: list[tuple[Point2, float]]) -> None:
        walkable = np.isfinite(grid)
        w, h = walkable.shape
        gx, gy = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5, indexing="ij")
        danger = np.zeros((w, h), dtype=bool)
        for center, radius in zones:
            x0, x1 = max(0, int(center.x - radius) - 1), min(w, int(center.x + radius) + 2)
            y0, y1 = max(0, int(center.y - radius) - 1), min(h, int(center.y + radius) + 2)
            inside = (gx[x0:x1, y0:y1] - center.x) ** 2 + (gy[x0:x1, y0:y1] - center.y) ** 2 <= radius * radius
            danger[x0:x1, y0:y1] |= inside
        self.open = walkable & ~danger
        self.shape = (w, h)
        self._graph = None
        self._fields: dict[tuple[int, int], np.ndarray] = {}

    def _cell(self, p: Point2) -> tuple[int, int] | None:
        c = (int(p.x), int(p.y))
        return c if 0 <= c[0] < self.shape[0] and 0 <= c[1] < self.shape[1] else None

    def _nearest_open(self, c: tuple[int, int], radius: int = 4) -> tuple[int, int] | None:
        """`c` if it's open, else the closest open cell within `radius` (a destination's center can be
        inside a building, a squad can stand just inside danger while backing off)."""
        if self.open[c]:
            return c
        best = None
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                n = (c[0] + dx, c[1] + dy)
                if 0 <= n[0] < self.shape[0] and 0 <= n[1] < self.shape[1] and self.open[n]:
                    d = dx * dx + dy * dy
                    if best is None or d < best[0]:
                        best = (d, n)
        return None if best is None else best[1]

    def field(self, goal: Point2) -> np.ndarray | None:
        """Walking distance to `goal` over open ground, from every cell (inf: no route)."""
        c = self._cell(goal)
        c = self._nearest_open(c) if c is not None else None
        if c is None:
            return None
        if c not in self._fields:
            if self._graph is None:
                self._graph = build_graph(self.open)
            dist = dijkstra(self._graph, directed=False, indices=c[0] * self.shape[1] + c[1])
            self._fields[c] = dist.reshape(self.shape)
        return self._fields[c]

    def distance(self, start: Point2, goal: Point2) -> float:
        """Walking distance from `start` to `goal` around the danger (inf: no route)."""
        field = self.field(goal)
        c = self._cell(start)
        c = self._nearest_open(c, 8) if c is not None else None
        if field is None or c is None:
            return math.inf
        return float(field[c])

    def reaches(self, start: Point2, goal: Point2) -> bool:
        field = self.field(goal)
        c = self._cell(start)
        c = self._nearest_open(c, 8) if c is not None else None
        return field is not None and c is not None and bool(np.isfinite(field[c]))

    def waypoint(self, start: Point2, goal: Point2, lookahead: int = 8) -> Point2 | None:
        """The next point on the route to `goal` (lookahead cells on), or None without a route."""
        field = self.field(goal)
        c = self._cell(start)
        c = self._nearest_open(c, 8) if c is not None else None
        if field is None or c is None or not np.isfinite(field[c]):
            return None
        cx, cy = c
        here = field[cx, cy]
        for _ in range(lookahead):
            if here == 0:
                break
            options = [
                (cx + dx, cy + dy)
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                if (dx or dy)
                and 0 <= cx + dx < self.shape[0]
                and 0 <= cy + dy < self.shape[1]
                and field[cx + dx, cy + dy] < here
            ]
            if not options:
                break
            cx, cy = min(options, key=lambda n: field[n])
            here = field[cx, cy]
        return Point2((cx + 0.5, cy + 0.5))


def build_graph(walk: np.ndarray, unit_blocked: np.ndarray | None = None, cost: np.ndarray | None = None):
    """Cells where `walk` is True as a graph: 8 neighbors, a diagonal 1.4142 long. Units fit through the
    corner gap between two buildings touching diagonally; with `unit_blocked` (cells enemy units block),
    not past an enemy unit: a diagonal step between two blocked cells only if neither is blocked by one
    (`ReachMap._squeezed`). `cost`: a per-cell cost (danger), averaged over each step's two ends."""
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
        if dx and dy and unit_blocked is not None:
            # one of the two orthogonal cells in between open, or neither blocked by an enemy unit
            a = (slice(xs + dx, xe + dx), slice(ys, ye))
            b = (slice(xs, xe), slice(ys + dy, ye + dy))
            ok &= walk[a] | walk[b] | ~(unit_blocked[a] | unit_blocked[b])
        rows.append(index[src][ok])
        cols.append(index[dst][ok])
        step = np.full(int(ok.sum()), 1.4142 if dx and dy else 1.0)
        if cost is not None:
            step = step * (cost[src][ok] + cost[dst][ok]) / 2
        weights.append(step)
    return coo_matrix(
        (np.concatenate(weights), (np.concatenate(rows), np.concatenate(cols))), shape=(w * h, w * h)
    ).tocsr()
