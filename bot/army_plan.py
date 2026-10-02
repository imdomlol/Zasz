"""Army plan: what each zergling does, every PLAN_SECONDS. Grouping only when necessary.

Used while attacking after the army has been turned back by an enemy army it can't beat (see
`Army.control`); until then, and when not attacking, the army moves as one, which wins fastest
against most opponents. A VeryHard benchmark with the plan always on went 2/6 (vs 5/6 without):
splitting into minimum-size raids all the time spread the pressure too thin.

The known enemy army is clustered into groups (visible units and ares' memory of units last seen
in the fog). Then:

- **Group up** only when there's a visible enemy group we can beat with the whole army but not
  with half of it: everyone gathers at a staging point outside its range, then engages together.
- Otherwise **spread out**:
  - **peek**: a remembered group that isn't in vision gets one zergling sent just close enough to
    see its last position while staying out of its weapon range. If that's impossible (its range
    is longer than zergling sight, e.g. sieged tanks), the area is avoided instead.
  - **fight**: a visible group part of the army can beat gets just enough zerglings; enemy bases
    away from groups we can't beat get raids sized by the combat sim, and so does their main when
    nothing in it is known yet and nothing we can't beat is in the way.
  - **scout**: single zerglings check the enemy's likely expansions we haven't seen lately.
  - **watch**: single zerglings sit outside the range of the biggest known enemy groups, on the way
    to our bases, so we see the army move.
  - leftovers join the biggest raid on a base (never a fight against an army); with no raid they
    attack a known enemy structure outside avoided areas, otherwise they hold while scouts look.
- When attacking with nothing to hit, the bulk waits forward on the field (pressure); when not
  attacking: watch posts and peeks for vision, everyone else holds at the rally point.

Fighting itself (target choice, fight-or-retreat, tank dives) stays in `bot/army.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ares.consts import TOWNHALL_TYPES, WORKER_TYPES
from loguru import logger
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.settings import (
    ENEMY_GROUP_RADIUS,
    FIGHT_AT,
    FORWARD_FRACTIONS,
    GATHERED_FRACTION,
    MAX_RAIDS,
    MAX_SCOUTS,
    MAX_WATCHERS,
    PEEK_AGAIN_SECONDS,
    PEEK_MARGIN,
    PLAN_SECONDS,
    RAID_MIN,
    STAGING_MARGIN,
    WATCH_MARGIN,
)

if TYPE_CHECKING:
    from ares import AresBot

    from bot.army import Army

IGNORED = {UnitTypeId.LARVA, UnitTypeId.EGG, UnitTypeId.OVERLORD, UnitTypeId.OVERSEER, UnitTypeId.OBSERVER}
ZERGLING_SIGHT = 8.0


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

    @property
    def key(self) -> tuple[int, int]:
        """Roughly where it is, to remember that we peeked at it."""
        return round(self.center.x / 4), round(self.center.y / 4)


@dataclass
class Task:
    kind: str  # fight, stage, peek, scout, watch, hold
    target: Point2
    group: EnemyGroup | None = None  # the enemy group a fight/stage/peek is about
    tags: set[int] = field(default_factory=set)


def center_of(units: list[Unit]) -> Point2:
    return Point2((sum(u.position.x for u in units) / len(units), sum(u.position.y for u in units) / len(units)))


def enemy_groups(ai: AresBot) -> list[EnemyGroup]:
    """Known enemy ground army (seen now or remembered), clustered by distance."""
    units = [
        u
        for u in ai.mediator.get_cached_enemy_army
        if not u.is_flying and u.type_id not in IGNORED and u.type_id not in WORKER_TYPES
    ]
    clusters: list[list[Unit]] = []
    for u in units:
        for cluster in clusters:
            if any(u.position.distance_to(o.position) < ENEMY_GROUP_RADIUS for o in cluster):
                cluster.append(u)
                break
        else:
            clusters.append([u])
    groups = []
    for c in clusters:
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


def wins(ai: AresBot, own: list[Unit], enemies: list[Unit]) -> bool:
    if not own:
        return False
    if not enemies:
        return True
    return ai.mediator.can_win_fight(own_units=own, enemy_units=enemies, workers_do_no_damage=True) >= FIGHT_AT


def needed(ai: AresBot, lings: list[Unit], enemies: list[Unit], near: Point2) -> int | None:
    """Fewest zerglings (closest to `near` first, in steps of RAID_MIN) the sim says beat
    `enemies`; None if even all of them don't."""
    ordered = sorted(lings, key=lambda u: u.position.distance_to(near))
    n = RAID_MIN
    while n < len(ordered):
        if wins(ai, ordered[:n], enemies):
            return n
        n += RAID_MIN
    return len(ordered) if wins(ai, ordered, enemies) else None


class ArmyPlan:
    def __init__(self) -> None:
        self.tasks: list[Task] = []
        self.assignment: dict[int, Task] = {}  # zergling tag -> its task
        self.planned_at = -999.0
        self.peeked: dict[tuple[int, int], float] = {}  # enemy group key -> when we last looked
        # While planning: the tasks being built, zerglings not given one yet, last plan's tasks
        self._new: list[Task] = []
        self._free: list[Unit] = []
        self._previous: dict[int, Task] = {}
        self._last_summary = ""

    def update(self, ai: AresBot, army: Army, lings: list[Unit], attacking: bool, rally: Point2) -> list[Task]:
        alive = {u.tag for u in lings}
        new_lings = alive - set(self.assignment)
        if ai.time - self.planned_at >= PLAN_SECONDS or (new_lings and not self.tasks):
            self.plan(ai, army, lings, attacking, rally)
        else:
            # Keep the plan between re-plans: drop dead units, newcomers join the default task
            for task in self.tasks:
                task.tags &= alive
            if default := self.default_task():
                default.tags |= new_lings
        self.assignment = {tag: t for t in self.tasks for tag in t.tags}
        return [t for t in self.tasks if t.tags]

    def default_task(self) -> Task | None:
        bulk = [t for t in self.tasks if t.kind in ("fight", "stage", "hold")]
        return max(bulk, key=lambda t: len(t.tags)) if bulk else None

    def on_peek_done(self, group: EnemyGroup, now: float) -> None:
        self.peeked[group.key] = now

    # --- planning ---

    def take(self, task: Task, count: int) -> None:
        """Give `task` up to `count` free zerglings: ones already doing the same thing first (so a
        re-plan doesn't reshuffle everyone), then the closest."""
        same = [
            u
            for u in self._free
            if (old := self._previous.get(u.tag)) and old.kind == task.kind and old.target.distance_to(task.target) < 6
        ]
        rest = sorted((u for u in self._free if u not in same), key=lambda u: u.position.distance_to(task.target))
        task.tags = {u.tag for u in (same + rest)[:count]}
        self._free = [u for u in self._free if u.tag not in task.tags]
        if task.tags:
            self._new.append(task)

    def plan(self, ai: AresBot, army: Army, lings: list[Unit], attacking: bool, rally: Point2) -> None:
        self.planned_at = ai.time
        self._previous, self._free, self._new = self.assignment, list(lings), []
        groups = enemy_groups(ai)

        if attacking and (target := self.group_up_target(ai, lings, groups)) is not None:
            # Group up: everyone gathers out of its range, then engages together
            staging = target.outside(rally, STAGING_MARGIN)
            gathered = sum(u.position.distance_to(staging) < 12 for u in lings)
            if gathered >= GATHERED_FRACTION * len(lings):
                self.take(Task("fight", target.center, target), len(lings))
            else:
                self.take(Task("stage", staging, target), len(lings))
            self.tasks = self._new
            self.log_if_changed(ai)
            return

        self.plan_peeks(ai, army, groups)
        if attacking:
            self.plan_fights(ai, army, groups)
            self.plan_scouts(ai, army)
        self.plan_watchers(ai, groups)
        if self._free:
            # Leftovers: the biggest raid on a base (never a fight against an army: those get exactly
            # the zerglings the sim says they need), else a known target, else hold while scouts look
            raids = [t for t in self._new if t.kind == "fight" and t.group is None]
            if raids:
                max(raids, key=lambda t: len(t.tags)).tags |= {u.tag for u in self._free}
                self._free = []
            elif attacking and (spot := army.known_target(ai)) is not None:
                self.take(Task("fight", spot), len(self._free))
            elif attacking:
                # Nothing to hit yet: wait out on the field (pressure), not back home
                self.take(Task("hold", self.forward_point(ai, army, groups, rally)), len(self._free))
            else:
                self.take(Task("hold", rally), len(self._free))
        self.tasks = self._new
        self.log_if_changed(ai)

    def log_if_changed(self, ai: AresBot) -> None:
        summary = ", ".join(
            f"{t.kind} x{len(t.tags)} @ {t.target.rounded}" for t in sorted(self.tasks, key=lambda t: t.kind)
        )
        if summary != self._last_summary:
            self._last_summary = summary
            logger.info(f"{ai.time_formatted} army plan: {summary or 'nothing'}")

    def forward_point(self, ai: AresBot, army: Army, groups: list[EnemyGroup], rally: Point2) -> Point2:
        """Where the bulk waits when attacking but there's nothing to hit yet: part of the way from
        our rally point to their natural, outside avoided areas and out of reach of known enemy
        groups. Falls back to the rally point."""
        enemy_nat = ai.mediator.get_enemy_nat
        for fraction in FORWARD_FRACTIONS:
            spot = rally.towards(enemy_nat, rally.distance_to(enemy_nat) * fraction)
            if (
                ai.in_pathing_grid(spot)
                and not army.blocked(ai, spot)
                and all(spot.distance_to(g.center) > g.spread + g.reach + WATCH_MARGIN for g in groups)
            ):
                return spot
        return rally

    def group_up_target(self, ai: AresBot, lings: list[Unit], groups: list[EnemyGroup]) -> EnemyGroup | None:
        """The closest visible enemy group we beat with everyone but not with half: worth grouping
        for. (Remembered-only groups get peeked at first.)"""
        if len(lings) < 2 * RAID_MIN:
            return None
        center = center_of(lings)
        for group in sorted((g for g in groups if g.visible), key=lambda g: g.center.distance_to(center)):
            half = sorted(lings, key=lambda u: u.position.distance_to(group.center))[: len(lings) // 2]
            if wins(ai, lings, group.units) and not wins(ai, half, group.units):
                return group
        return None

    def plan_peeks(self, ai: AresBot, army: Army, groups: list[EnemyGroup]) -> None:
        for group in groups:
            if group.visible:
                continue
            if (
                group.reach + PEEK_MARGIN >= ZERGLING_SIGHT - 0.5
            ):  # the nearest units must be visible from outside their range
                # Can't see it without being shot (e.g. sieged tanks): avoid the area instead
                army.turned_back(ai, group.units)
                continue
            if ai.time - self.peeked.get(group.key, -999) < PEEK_AGAIN_SECONDS:
                continue
            self.take(Task("peek", group.outside(ai.start_location, PEEK_MARGIN), group), 1)

    def plan_fights(self, ai: AresBot, army: Army, groups: list[EnemyGroup]) -> None:
        # Visible enemy groups part of the army can beat get just enough zerglings
        for group in groups:
            if not group.visible:
                continue  # remembered only: peek first, don't commit a fight to a guess
            n = needed(ai, self._free, group.units, group.center)
            if n is not None:
                self.take(Task("fight", group.center, group), n)
            else:
                army.turned_back(ai, group.units)  # can't beat it with what's free: keep away
        # Raids on enemy bases away from groups we can't beat, far from their main first
        targets = [
            s.position for s in ai.enemy_structures if s.type_id in TOWNHALL_TYPES and not army.blocked(ai, s.position)
        ]
        # Their main is always worth checking: if we know nothing in it and no army we can't beat
        # is in the way, raid it like any base (when their army moves out to defend elsewhere, the
        # main opens up)
        main = ai.enemy_start_locations[0]
        if not ai.enemy_structures.closer_than(12, main) and not army.blocked(ai, main):
            targets.append(main)
        enemy_army = ai.mediator.get_cached_enemy_army
        raids = 0
        for spot in sorted(targets, key=lambda t: -t.distance_to(main)):
            if raids >= MAX_RAIDS or not self._free:
                break
            defenders = [u for u in enemy_army if u.position.distance_to(spot) < 15]
            n = needed(ai, self._free, defenders, spot) if defenders else RAID_MIN
            if n is not None:
                self.take(Task("fight", spot), max(n, RAID_MIN))
                raids += 1

    def plan_scouts(self, ai: AresBot, army: Army) -> None:
        scouts = 0
        for spot, _distance in ai.mediator.get_enemy_expansions:  # their main first, then expansions
            if scouts >= MAX_SCOUTS or not self._free:
                break
            if army.blocked(ai, spot) or ai.enemy_structures.closer_than(8, spot):
                continue  # dangerous, or a known base (raided, not scouted)
            if ai.time - army.checked_empty.get(spot, -999) < 60:
                continue
            if ai.is_visible(spot):
                army.checked_empty[spot] = ai.time
                continue
            self.take(Task("scout", spot), 1)
            scouts += 1

    def plan_watchers(self, ai: AresBot, groups: list[EnemyGroup]) -> None:
        """Out of range of the biggest known enemy groups, toward our nearest bases: see them move."""
        posts: list[Point2] = []
        for group in sorted(groups, key=lambda g: -len(g.units))[:2]:
            for base in sorted(ai.townhalls, key=lambda t: t.distance_to(group.center))[:2]:
                post = group.outside(base.position, WATCH_MARGIN)
                if ai.in_pathing_grid(post) and all(post.distance_to(p) > 6 for p in posts):
                    posts.append(post)
        for post in posts[:MAX_WATCHERS]:
            if not self._free:
                break
            self.take(Task("watch", post), 1)
