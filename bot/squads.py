"""Squads: lasting groups of zerglings, each doing its own job.

Zerglings are kept in squads of about SQUAD_SIZE (`SquadBook`): new ones fill up a squad near them,
small squads out of combat merge, big ones split. A squad keeps its identity, so it can be sent
somewhere and remembered as being on its way. Each squad has a job:

- **patrol**: not fighting, so scouting. Go where we've had no vision the longest (`Patrol`):
  expansions and a grid of spots over the map, nearest first among equally stale ones, each squad
  claiming its own spot so they spread out. Early on that finds proxies near our base; later,
  hidden expansions far away.
- **raid** (only while attacking): go for an enemy site (`Site`: a cluster of enemy structures, or an
  enemy army in the field) with as many zerglings as the combat sim says it needs, gathering
  outside its defenders' range first when it's defended.
- **help**: on the way to another squad's fight (see below).
- **defend**: enemies at our bases; everyone goes.

Every squad that meets the enemy fights or retreats on its own (`bot/army.py`). Meanwhile every
fight is watched (`Battle`): if the sim calls it worse than HELP_WIN_AT, the fighting squad gets
help, but only if more zerglings would actually fit around its targets (a surrounded enemy needs
nobody else piling on and blocking), and only the free squads that together make it a win, closest
first (`choose_helpers`): two squads that each couldn't turn the fight alone still go if together
they do. If no set of squads in reach turns it, nobody goes; squads already on their way turn back.

Fighting itself (target choice, fight-or-retreat, tank dives) stays in `bot/army.py`.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ares.consts import WORKER_TYPES, EngagementResult
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.settings import (
    ENEMY_GROUP_RADIUS,
    FIGHT_AT,
    JOIN_RADIUS,
    MERGE_RADIUS,
    PATROL_CLAIM_RADIUS,
    PATROL_DISTANCE_COST,
    PATROL_FRESH,
    PATROL_GRID,
    PATROL_STALE_CAP,
    RAID_MIN,
    SITE_DEFENSE_RADIUS,
    SITE_RADIUS,
    SQUAD_MAX,
    SQUAD_SIZE,
)

if TYPE_CHECKING:
    from ares import AresBot

IGNORED = {UnitTypeId.LARVA, UnitTypeId.EGG, UnitTypeId.OVERLORD, UnitTypeId.OVERSEER, UnitTypeId.OBSERVER}


def center_of(units: list[Unit]) -> Point2:
    return Point2((sum(u.position.x for u in units) / len(units), sum(u.position.y for u in units) / len(units)))


# --- the enemy ---


@dataclass
class EnemyGroup:
    units: list[Unit]
    center: Point2
    reach: float  # farthest ground weapon reach (range + radius) in the group
    visible: bool  # at least one unit seen right now (the rest may be remembered)
    spread: float = 0.0  # farthest unit from the center

    def outside(self, toward: Point2, margin: float) -> Point2:
        """A spot toward `toward`, outside the range of every unit in the group by `margin`."""
        return self.center.towards(toward, self.spread + self.reach + margin)


def cluster(units: list[Unit], radius: float) -> list[list[Unit]]:
    clusters: list[list[Unit]] = []
    for u in units:
        for c in clusters:
            if any(u.position.distance_to(o.position) < radius for o in c):
                c.append(u)
                break
        else:
            clusters.append([u])
    return clusters


def enemy_groups(ai: AresBot) -> list[EnemyGroup]:
    """Known enemy ground army (seen now or remembered), clustered by distance. Built once per game
    step (several callers ask for it, some once per spot they consider); don't change the result."""
    state = getattr(ai, "state", None)
    if state is not None and getattr(ai, "_enemy_groups", (None,))[0] == state.game_loop:
        return ai._enemy_groups[1]
    groups = _enemy_groups(ai)
    if state is not None:
        ai._enemy_groups = (state.game_loop, groups)
    return groups


def _enemy_groups(ai: AresBot) -> list[EnemyGroup]:
    units = [
        u
        for u in ai.mediator.get_cached_enemy_army
        if not u.is_flying and u.type_id not in IGNORED and u.type_id not in WORKER_TYPES
    ]
    groups = []
    for c in cluster(units, ENEMY_GROUP_RADIUS):
        center = center_of(c)
        groups.append(
            EnemyGroup(
                c,
                center,
                max(u.ground_range + u.radius for u in c),
                any(not u.is_memory for u in c),
                max(u.position.distance_to(center) for u in c),
            )
        )
    return groups


def fight_verdict(ai: AresBot, own: list[Unit], enemies: list[Unit]) -> EngagementResult:
    """ares' combat sim verdict for `own` against `enemies`. ares runs the sim on every request, and the
    same fight gets asked about many times a step (both win bars, every helper or strike size, every
    spot a squad might go), so verdicts are kept for the rest of the game step. Keyed by the units in
    order (the sim gets exactly the same input); a new step starts afresh (health, positions change)."""
    state = getattr(ai, "state", None)
    if state is None:  # a stand-in bot (tests)
        return ai.mediator.can_win_fight(own_units=own, enemy_units=enemies, workers_do_no_damage=True)
    loop, verdicts = getattr(ai, "_fight_verdicts", (None, None))
    if loop != state.game_loop:
        verdicts = {}
        ai._fight_verdicts = (state.game_loop, verdicts)
    key = (tuple(u.tag for u in own), tuple(u.tag for u in enemies))
    if key not in verdicts:
        verdicts[key] = ai.mediator.can_win_fight(own_units=own, enemy_units=enemies, workers_do_no_damage=True)
    return verdicts[key]


def wins(ai: AresBot, own: list[Unit], enemies: list[Unit], at: EngagementResult = FIGHT_AT) -> bool:
    if not own:
        return False
    if not enemies:
        return True
    return fight_verdict(ai, own, enemies) >= at


def needed(
    ai: AresBot, lings: list[Unit], enemies: list[Unit], near: Point2, at: EngagementResult = FIGHT_AT
) -> int | None:
    """Fewest zerglings (closest to `near` first, in steps of RAID_MIN) the sim says beat
    `enemies` (at least `at`); None if even all of them don't."""
    ordered = sorted(lings, key=lambda u: u.position.distance_to(near))
    n = RAID_MIN
    while n < len(ordered):
        if wins(ai, ordered[:n], enemies, at):
            return n
        n += RAID_MIN
    return len(ordered) if wins(ai, ordered, enemies, at) else None


@dataclass
class Site:
    """Something to raid: a cluster of enemy structures (a base, a proxy), or an enemy army in the
    field, with the enemy army defending it."""

    center: Point2
    structures: list[Unit]
    defenders: list[Unit]


def enemy_sites(ai: AresBot, structures: list[Unit], groups: list[EnemyGroup]) -> list[Site]:
    """Clusters of the given enemy structures, each with the known army near it; plus visible enemy
    groups away from any structure (an army in the field)."""
    army = [u for g in groups for u in g.units]
    sites = []
    for c in cluster(structures, SITE_RADIUS):
        center = center_of(c)
        defenders = [u for u in army if u.position.distance_to(center) < SITE_DEFENSE_RADIUS]
        sites.append(Site(center, c, defenders))
    for g in groups:
        if g.visible and all(g.center.distance_to(s.center) >= SITE_DEFENSE_RADIUS for s in sites):
            sites.append(Site(g.center, [], g.units))
    return sites


# --- our squads ---


@dataclass
class Squad:
    id: int
    tags: set[int]
    job: str = "patrol"  # patrol, raid, push, help, gather, engage, defend, scout
    target: Point2 | None = None  # patrol spot, raid site, or the fight being helped
    helping: int | None = None  # the squad whose fight we're joining
    site: Point2 | None = None  # the site being raided (its center)
    site_tags: set[int] = field(default_factory=set)  # enemy units and structures at that site
    units: list[Unit] = field(default_factory=list)  # this step's units

    # Shaped like an ares UnitSquad, for the per-squad fight logic in bot/army.py
    @property
    def squad_units(self) -> list[Unit]:
        return self.units

    @property
    def squad_position(self) -> Point2:
        return center_of(self.units)

    def free(self, job: str = "patrol") -> None:
        self.job, self.target, self.helping, self.site = job, None, None, None
        self.site_tags = set()


class SquadBook:
    """Which zergling is in which squad."""

    def __init__(self) -> None:
        self.squads: dict[int, Squad] = {}
        self._ids = itertools.count(1)

    def update(self, lings: list[Unit], busy: set[int]) -> list[Squad]:
        """Drop dead zerglings, put new ones in a squad, and (for squads not in `busy`, i.e. not
        fighting or helping) merge small squads and split big ones."""
        by_tag = {u.tag: u for u in lings}
        for squad in list(self.squads.values()):
            squad.tags &= by_tag.keys()
            if not squad.tags:
                del self.squads[squad.id]
                continue
            squad.units = [by_tag[t] for t in squad.tags]
        member = {t for s in self.squads.values() for t in s.tags}
        for u in lings:
            if u.tag in member:
                continue
            room = [
                s
                for s in self.squads.values()
                if s.id not in busy
                and len(s.tags) < SQUAD_SIZE
                and s.squad_position.distance_to(u.position) < JOIN_RADIUS
            ]
            squad = min(room, key=lambda s: s.squad_position.distance_to(u.position)) if room else self.new()
            squad.tags.add(u.tag)
            squad.units.append(u)
        self._merge(busy)
        self._split(busy)
        return list(self.squads.values())

    def new(self) -> Squad:
        squad = Squad(next(self._ids), set())
        self.squads[squad.id] = squad
        return squad

    def _merge(self, busy: set[int]) -> None:
        """Two squads out of combat, close together, that fit in one: one squad (one merge a step)."""
        idle = [s for s in self.squads.values() if s.id not in busy]
        for a, b in itertools.combinations(idle, 2):
            if (
                len(a.tags) + len(b.tags) <= SQUAD_SIZE
                and a.squad_position.distance_to(b.squad_position) < MERGE_RADIUS
            ):
                keep, gone = (a, b) if len(a.tags) >= len(b.tags) else (b, a)
                keep.tags |= gone.tags
                keep.units += gone.units
                del self.squads[gone.id]
                return

    def _split(self, busy: set[int]) -> None:
        """A squad out of combat that grew past SQUAD_MAX (helpers that joined a fight): split it in
        two along its longer side."""
        for squad in list(self.squads.values()):
            if squad.id in busy or len(squad.tags) <= SQUAD_MAX:
                continue
            xs = [u.position.x for u in squad.units]
            ys = [u.position.y for u in squad.units]
            wide = max(xs) - min(xs) >= max(ys) - min(ys)
            ordered = sorted(squad.units, key=lambda u: u.position.x if wide else u.position.y)
            half = ordered[len(ordered) // 2 :]
            other = self.new()
            other.tags = {u.tag for u in half}
            other.units = half
            squad.tags -= other.tags
            squad.units = ordered[: len(ordered) // 2]


# --- patrolling ---


class Patrol:
    """Where we've had no vision the longest: expansions plus a grid of spots over the map. Vision
    means one of our own units (or structures) seeing the spot, not SC2's visibility (which a
    revealed map, e.g. in scenarios, makes total)."""

    def __init__(self) -> None:
        self.spots: list[Point2] = []
        self.seen: dict[Point2, float] = {}

    def setup(self, ai: AresBot) -> None:
        spots = list(ai.expansion_locations_list)
        area = ai.game_info.playable_area
        for x in range(int(area.x) + PATROL_GRID // 2, int(area.x + area.width), PATROL_GRID):
            for y in range(int(area.y) + PATROL_GRID // 2, int(area.y + area.height), PATROL_GRID):
                spot = Point2((x + 0.5, y + 0.5))
                if ai.in_pathing_grid(spot) and all(spot.distance_to(s) > PATROL_GRID / 2 for s in spots):
                    spots.append(spot)
        self.spots = spots
        self.seen = {s: -PATROL_STALE_CAP for s in spots}  # never seen: as stale as it gets (not seen at 0 s)

    def update(self, ai: AresBot) -> None:
        own = [(u.position, u.sight_range) for u in ai.all_own_units if u.sight_range > 0]
        for spot in self.spots:
            if any(spot.distance_to(p) < sight for p, sight in own):
                self.seen[spot] = ai.time

    def fresh(self, spot: Point2, now: float) -> bool:
        """Seen by us within the last PATROL_FRESH seconds: nothing new to find there."""
        return now - self.seen.get(spot, -PATROL_STALE_CAP) < PATROL_FRESH

    def pick(
        self, now: float, position: Point2, claimed: list[Point2], avoid: Callable[[Point2], bool]
    ) -> Point2 | None:
        """The stalest spot (nearest first among equally stale ones) no other squad has claimed
        and that isn't dangerous."""
        best, best_score = None, -float("inf")
        for spot in self.spots:
            if self.fresh(spot, now) or any(spot.distance_to(c) < PATROL_CLAIM_RADIUS for c in claimed) or avoid(spot):
                continue
            score = min(now - self.seen[spot], PATROL_STALE_CAP) - PATROL_DISTANCE_COST * spot.distance_to(position)
            if score > best_score:
                best, best_score = spot, score
        return best


@dataclass
class Strike:
    """Zerglings sent for a group of enemies shooting our units (`Army.update_strikes`)."""

    targets: set[int]  # tags of the enemies in the group
    lings: set[int]  # our zerglings on it
    spot: Point2 | None  # where they gather, outside the enemies' reach
    since: float
    reach: object = None  # the ReachMap it was planned on (`bot.targeting.ReachMap`)
    go: bool = False  # gathered: going in


# --- helping ---


@dataclass
class Rally:
    """A losing squad and the squads coming to help it, gathering outside the enemy's reach to go in
    together."""

    spot: Point2  # where they gather
    enemy: Point2  # the fight they go into
    squads: list[int]  # squad ids
    since: float


@dataclass
class Battle:
    """A squad's fight this step, for the others to watch."""

    center: Point2
    threats: list[Unit]
    present: list[Unit]  # our army units in the fight (any squad)
    result: EngagementResult
    room: int  # how many more of our melee units would fit around the targets


def choose_helpers(
    present: list[Unit],
    enemies: list[Unit],
    candidates: list[tuple[Squad, list[Unit]]],
    wins_with: Callable[[list[Unit], list[Unit]], bool],
    room: int,
    wins_at_all: Callable[[list[Unit], list[Unit]], bool] | None = None,
) -> list[Squad] | None:
    """Which free squads should join a fight: the fewest of `candidates` (in the order given, closest
    first) whose zerglings, added to those already `present`, make the sim call it a win. Only the
    first `room` helping zerglings count (more wouldn't fit around the targets). [] if it's already
    won; None if no set of candidates turns it (then nobody should go). If no set makes it a
    `wins_with` win but one makes it a `wins_at_all` win (a looser bar), the fewest for that."""
    if wins_with(present, enemies):
        return []
    for bar in (wins_with, wins_at_all):
        if bar is None:
            continue
        chosen: list[Squad] = []
        extra: list[Unit] = []
        for squad, units in candidates:
            chosen.append(squad)
            extra += units
            if bar(present + extra[: max(0, room)], enemies):
                return chosen
    return None
