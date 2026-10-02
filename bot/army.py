"""Army control: squads, fight-or-retreat, and per-unit target selection.

Zerglings get the ATTACKING role when they hatch (see `ZaszBot.on_unit_created`). ares groups
them into squads; each squad then:

1. Fights if enemies are close and the combat sim predicts at least an even fight (or we're
   defending a base). Zerglings dive siege tanks first instead of trading with them from the
   edge of their range.
2. Retreats as soon as the sim predicts a loss, but only until it's out of known enemy range,
   then re-engages if the fight looks winnable. Flip-flopping keeps pressure on; the rule is
   that units never stand in enemy fire without either fighting or leaving.
3. Otherwise regroups with the main squad before moving on, so we don't trickle in.

Threats are looked up around every unit in the squad (not just its center), and units that
are being shot from out of sight count as "under fire", so long-range units like sieged tanks
can't hit the front of the squad unnoticed.

Whether to attack at all is a decision (`Decision.attack`); this module only executes.
"""

from dataclasses import dataclass

from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, AttackTarget, PathUnitToTarget, ShootTargetInRange
from ares.consts import TOWNHALL_TYPES, WORKER_TYPES, EngagementResult, UnitRole, UnitTreeQueryType
from ares.managers.squad_manager import UnitSquad
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.army_plan import ArmyPlan, Task, center_of, enemy_groups, wins
from bot.settings import (
    APPROACH_DISTANCE,
    AVOID_MARGIN,
    AVOID_SECONDS,
    BREAKTHROUGH_RANGE,
    CHECK_AGAIN_SECONDS,
    DEFEND_RADIUS,
    ENGAGE_RANGE,
    FIGHT_AT,
    FIGHTER_PRIORITY_RANGE,
    FLANK_DISTANCE,
    LOOKOUT_FLEE_MARGIN,
    MEMORY_THREAT_MARGIN,
    PLAN_SECONDS,
    REGROUP_DISTANCE,
    SCOUT_ZERGLINGS,
    SQUAD_RADIUS,
    TANK_NEUTRALIZED_BY,
    TARGET_FILL,
    UNDER_FIRE_SECONDS,
    VISIBLE_THREAT_MARGIN,
)
from bot.targeting import BlockTracker, Detours, TargetSlots, pick_tiered

ARMY_TYPES = {UnitTypeId.ZERGLING}
SIEGE_MIN_RANGE = 2.0  # sieged tanks can't fire at targets this close
IGNORED_THREATS = {UnitTypeId.LARVA, UnitTypeId.EGG, UnitTypeId.OVERLORD, UnitTypeId.OVERSEER}
# Long-range or splash units zerglings should dive first rather than trade with from range
DIVE_FIRST = {UnitTypeId.SIEGETANKSIEGED, UnitTypeId.SIEGETANK, UnitTypeId.LURKERMPBURROWED, UnitTypeId.COLOSSUS}
# Structures worth killing first once their defenders are dead: economy, then power/production
PRIORITY_STRUCTURES = TOWNHALL_TYPES | {UnitTypeId.PYLON, UnitTypeId.SPAWNINGPOOL}


@dataclass
class Detachment:
    """A group of our units on the same task, shaped like an ares `UnitSquad` so the squad logic
    works on it (main_squad: it never waits to regroup with anyone)."""

    squad_units: list[Unit]
    main_squad: bool = True

    @property
    def squad_position(self) -> Point2:
        return center_of(self.squad_units)


class Army:
    """Keeps the per-unit memory that stops squads flip-flopping between fight and retreat."""

    def __init__(self) -> None:
        self.retreating: set[int] = set()  # unit tags retreating until they're out of enemy range
        self.last_damaged: dict[int, float] = {}  # unit tag -> last time it took damage
        # Where squads were turned back by a fight the sim says we'd lose: (enemy position, until).
        # Attack targets near these are skipped for a while, so the army tries other bases instead
        # of walking back into the same wall as soon as the enemy leaves vision.
        self.turned_back_at: list[tuple[Point2, float, float]] = []  # (center, radius, until)
        self.checked_empty: dict[Point2, float] = {}  # expansion spot -> when we saw nothing there
        self.plan = ArmyPlan()  # what each zergling does: fight, stage, peek, scout, watch, hold
        self.blocks = BlockTracker()  # which zerglings are stuck behind others, per target
        self.detours = Detours()  # routes around our own crowd for blocked zerglings
        self.scouts: dict[int, Point2] = {}  # scouting zergling tag -> spot it's checking
        self.armies_checked_at = -999.0

    def on_damage(self, ai: AresBot, unit: Unit) -> None:
        self.last_damaged[unit.tag] = ai.time

    def control(self, ai: AresBot, attacking: bool) -> None:
        """Defend first. Otherwise one army moving together (attack, or hold at the rally point):
        against most opponents a single ball of zerglings wins fastest. Only once an attack has been
        turned back by an army we can't beat (an active avoid area) does the army plan take over:
        peek, raid around it, scout, watch (`ArmyPlan`), until those areas expire."""
        alive = {u.tag for u in ai.units}
        self.blocks.forget_dead(alive)
        self.detours.forget_dead(alive)
        home_threats = [
            u for u in ai.mediator.get_main_ground_threats_near_townhall if u.type_id not in IGNORED_THREATS
        ]
        if home_threats:
            # Defending always comes first: every squad goes for the threat
            self.control_squads(ai, Point2(home_threats[0].position), advancing=True)
            return
        self.control_scouts(ai, attacking)
        if attacking and ai.time - self.armies_checked_at >= PLAN_SECONDS:
            self.armies_checked_at = ai.time
            self.avoid_armies_we_cannot_beat(ai)
        if not attacking or not self.avoided(ai):
            self.control_squads(ai, self.attack_target(ai) if attacking else rally_point(ai), advancing=attacking)
            return

        lings = [u for u in ai.mediator.get_units_from_role(role=UnitRole.ATTACKING) if u.type_id in ARMY_TYPES]
        if not lings:
            return
        by_tag = {u.tag: u for u in lings}
        for task in self.plan.update(ai, self, lings, attacking, rally_point(ai)):
            units = [by_tag[t] for t in task.tags if t in by_tag]
            if not units:
                continue
            if task.kind in ("fight", "hold", "stage"):
                # Staging and holding still fight what they can beat and back off from what they
                # can't; they just don't advance
                group = Detachment(units)
                self.control_squad(ai, group, task.target, task.kind == "fight", group.squad_position)
            else:
                for unit in units:
                    self.lookout(ai, unit, task)

    def avoid_armies_we_cannot_beat(self, ai: AresBot) -> None:
        """Mark every known enemy group (seen now or remembered) the whole army can't beat as an
        area to avoid, before walking into it rather than after losing half the zerglings."""
        lings = [u for u in ai.mediator.get_units_from_role(role=UnitRole.ATTACKING) if u.type_id in ARMY_TYPES]
        for group in enemy_groups(ai):
            if not wins(ai, lings, group.units):
                self.turned_back(ai, group.units)

    def control_scouts(self, ai: AresBot, attacking: bool) -> None:
        """While attacking, SCOUT_ZERGLINGS zerglings run ahead as lookouts: their natural first,
        then expansions we haven't seen lately, so the army sees what's out there before it walks
        in. They rejoin the army when we stop attacking."""
        alive = {u.tag: u for u in ai.units(UnitTypeId.ZERGLING)}
        self.scouts = {tag: spot for tag, spot in self.scouts.items() if tag in alive}
        if not attacking:
            for tag in self.scouts:
                ai.mediator.assign_role(tag=tag, role=UnitRole.ATTACKING)
            self.scouts = {}
            return
        army = [u for u in ai.mediator.get_units_from_role(role=UnitRole.ATTACKING) if u.type_id in ARMY_TYPES]
        while len(self.scouts) < SCOUT_ZERGLINGS and len(army) > SCOUT_ZERGLINGS:
            spot = self.next_scout_spot(ai)
            scout = min(army, key=lambda u: u.position.distance_to(spot))
            army.remove(scout)
            ai.mediator.assign_role(tag=scout.tag, role=UnitRole.SCOUTING)
            self.scouts[scout.tag] = spot
        for tag, spot in list(self.scouts.items()):
            unit = alive[tag]
            if unit.distance_to(spot) < 4 or ai.is_visible(spot):
                if not ai.enemy_structures.closer_than(8, spot):
                    self.checked_empty[spot] = ai.time
                spot = self.scouts[tag] = self.next_scout_spot(ai)
            self.lookout(ai, unit, Task("scout", spot))

    def next_scout_spot(self, ai: AresBot) -> Point2:
        """Their natural, then their expansions, skipping spots another scout has or that we've
        seen empty in the last minute."""
        taken = set(self.scouts.values())
        spots = [spot for spot, _ in ai.mediator.get_enemy_expansions]
        for spot in spots[1:] + spots[:1]:
            if spot in taken or ai.time - self.checked_empty.get(spot, -999) < CHECK_AGAIN_SECONDS:
                continue
            return spot
        return ai.mediator.get_enemy_nat

    def control_squads(self, ai: AresBot, target: Point2, advancing: bool) -> None:
        """Every ares squad toward one target; squads away from the main one regroup with it first."""
        squads: list[UnitSquad] = ai.mediator.get_squads(role=UnitRole.ATTACKING, squad_radius=SQUAD_RADIUS)
        if not squads:
            return
        main_squad_position = ai.mediator.get_position_of_main_squad(role=UnitRole.ATTACKING)
        for squad in squads:
            self.control_squad(ai, squad, target, advancing, main_squad_position)

    def lookout(self, ai: AresBot, unit: Unit, task: Task) -> None:
        """Peek, scout and watch: single zerglings that look, never fight. They back off from any
        visible enemy that could shoot them, or when shot by something unseen. Their spots are a
        bit farther out than the flee distance, so they don't bounce between the two."""
        danger = [
            e
            for e in ai.enemy_units
            if not e.is_memory
            and e.can_attack_ground
            and e.position.distance_to(unit.position) < e.ground_range + e.radius + unit.radius + LOOKOUT_FLEE_MARGIN
        ]
        if danger or ai.time - self.last_damaged.get(unit.tag, -99) < UNDER_FIRE_SECONDS:
            move(ai, unit, rally_point(ai))
            return
        spot = task.target
        if task.kind == "peek" and task.group is not None:
            nearest = min(task.group.units, key=lambda e: e.position.distance_to(unit.position))
            if ai.is_visible(nearest.position):
                # Seen: still there (stays known) or gone (ares forgets it)
                self.plan.on_peek_done(task.group, ai.time)
        if unit.distance_to(spot) > APPROACH_DISTANCE:
            move(ai, unit, spot)  # around known enemy fire
        elif unit.distance_to(spot) > 1:
            unit.move(spot)  # the last stretch straight in (the danger-aware path won't go this close)

    def turned_back(self, ai: AresBot, threats: list[Unit]) -> None:
        """Remember where a squad had to back off: the area those enemies cover (their spread plus
        weapon range), replacing an older entry for the same spot."""
        center = center_of(threats)
        spread = max(t.position.distance_to(center) for t in threats)
        radius = spread + max(t.ground_range + t.radius for t in threats) + AVOID_MARGIN
        self.turned_back_at = [(p, r, t) for p, r, t in self.turned_back_at if p.distance_to(center) > radius / 2]
        self.turned_back_at.append((center, radius, ai.time + AVOID_SECONDS))

    def avoided(self, ai: AresBot) -> list[tuple[Point2, float]]:
        """Active avoid areas: (center, radius)."""
        self.turned_back_at = [(p, r, t) for p, r, t in self.turned_back_at if t > ai.time]
        return [(p, r) for p, r, _ in self.turned_back_at]

    def blocked(self, ai: AresBot, spot: Point2) -> bool:
        return any(spot.distance_to(p) < r for p, r in self.avoided(ai))

    def known_target(self, ai: AresBot) -> Point2 | None:
        """A known enemy base (least defended, far from their main first) or structure outside the
        areas we were turned back from; None if there isn't one."""
        enemy_main = ai.enemy_start_locations[0]
        bases = [s for s in ai.enemy_structures if s.type_id in TOWNHALL_TYPES and not self.blocked(ai, s.position)]
        if bases:
            army = ai.mediator.get_cached_enemy_army

            def defense(base: Unit) -> tuple[float, float]:
                spot = base.position
                supply = sum(ai.calculate_supply_cost(u.type_id) for u in army if u.position.distance_to(spot) < 15)
                static = sum(
                    1 for s in ai.enemy_structures if s.can_attack_ground and s.position.distance_to(spot) < 10
                )
                return supply + 4 * static, -spot.distance_to(enemy_main)

            return min(bases, key=defense).position
        structures = [s for s in ai.enemy_structures if not self.blocked(ai, s.position)]
        if structures:
            return min(structures, key=lambda s: s.position.distance_to(rally_point(ai))).position
        return None

    def attack_target(self, ai: AresBot) -> Point2:
        """Where to attack when there's one target for everyone (see `ArmyPlan`): a known target
        (`known_target`), else the enemy main if unseen, else sweep expansions to find hidden
        buildings; hold at the rally point if everything known is blocked."""
        if (spot := self.known_target(ai)) is not None:
            return spot
        if self.avoided(ai):
            return rally_point(ai)
        enemy_main = ai.enemy_start_locations[0]
        if not ai.is_visible(enemy_main):
            return enemy_main
        # Visit expansions in turn, farthest from us first, changing every 20 seconds
        expansions = sorted(ai.expansion_locations_list, key=lambda p: -p.distance_to(ai.start_location))
        return expansions[int(ai.time // 20) % len(expansions)]

    def control_squad(
        self, ai: AresBot, squad: UnitSquad, target: Point2, advancing: bool, main_position: Point2
    ) -> None:
        units = squad.squad_units
        enemies = enemies_near(ai, units)
        threats = [e for e in enemies if (not e.is_structure or e.can_attack_ground) and can_reach_soon(e, units)]
        now = ai.time
        grid = ai.mediator.get_ground_grid
        if sum(u.tag in self.retreating for u in units) > len(units) / 2:
            # Keep retreating only until every unit is out of known enemy range, then re-engage
            # right away if the fight looks winnable again: constant pressure, no free damage
            if any(not ai.mediator.is_position_safe(grid=grid, position=u.position) for u in units):
                for unit in units:
                    retreat(ai, unit)
                return
            self.retreating.difference_update(u.tag for u in units)
        under_fire = any(now - self.last_damaged.get(u.tag, -99) < UNDER_FIRE_SECONDS for u in units)

        if threats:
            near_home = ai.townhalls.closest_distance_to(squad.squad_position) < DEFEND_RADIUS
            result = fight_result(ai, units, threats)
            # Fight while the sim predicts at least an even fight; any predicted loss means
            # retreat, so we never keep feeding units into a losing fight
            if near_home or result >= FIGHT_AT:
                slots = TargetSlots()
                for unit in front_first(units, enemies):
                    fight(ai, unit, threats, enemies, slots, squad.squad_position, self.blocks, self.detours)
            else:
                self.turned_back(ai, threats)
                for unit in units:
                    self.retreating.add(unit.tag)
                    retreat(ai, unit)
            return

        if under_fire and not advancing:
            # Shot by something we can't see, and not attacking: get out of there
            for unit in units:
                self.retreating.add(unit.tag)
                retreat(ai, unit)
            return

        if (
            advancing
            and not under_fire
            and not squad.main_squad
            and squad.squad_position.distance_to(main_position) > REGROUP_DISTANCE
        ):
            # Join the main squad before going further (but never stop while being shot)
            for unit in units:
                move(ai, unit, main_position)
            return

        slots = TargetSlots()
        for unit in front_first(units, enemies):
            if advancing:
                if threats or any(e.is_structure for e in enemies):
                    fight(ai, unit, threats, enemies, slots, squad.squad_position, self.blocks, self.detours)
                elif under_fire or unit.distance_to(target) < APPROACH_DISTANCE:
                    # Close to the target (or shot from out of sight): attack-move in
                    ai.register_behavior(AMove(unit=unit, target=target))
                else:
                    # On the way: follow ares' path around known enemy fire (e.g. the army that
                    # turned us back) instead of walking straight through it
                    move(ai, unit, target)
            elif unit.distance_to(target) > 5:
                move(ai, unit, target)


def front_first(units: list[Unit], enemies: list[Unit]) -> list[Unit]:
    """Units closest to the enemy first, so they claim the target slots they're already at."""
    if not enemies:
        return units
    return sorted(units, key=lambda u: min(u.position.distance_to(e.position) for e in enemies))


def can_reach_soon(enemy: Unit, units: list[Unit]) -> bool:
    """Within the enemy's weapon range plus a margin of one of these units: a margin for visible
    enemies (they can step in), a smaller one for remembered ones (last seen in the fog). Enemies
    farther away don't turn a squad back, so going *around* an army at a safe distance works."""
    margin = MEMORY_THREAT_MARGIN if enemy.is_memory else VISIBLE_THREAT_MARGIN
    reach = enemy.ground_range + enemy.radius + margin
    return any(u.position.distance_to(enemy.position) - u.radius <= reach for u in units)


def enemies_near(ai: AresBot, units: list[Unit]) -> list[Unit]:
    """Enemy ground units/structures within ENGAGE_RANGE of any unit in the squad.

    Includes ares' memory of units last seen in the fog (e.g. a sieged tank revealed by firing).
    Memory units aren't in python-sc2's per-frame distance cache, so always measure distances
    to enemies with positions (`a.position.distance_to(b.position)`), never `unit.distance_to(unit)`.
    """
    found: dict[int, Unit] = {}
    for group in ai.mediator.get_units_in_range(
        start_points=[u.position for u in units], distances=ENGAGE_RANGE, query_tree=UnitTreeQueryType.EnemyGround
    ):
        for e in group:
            if e.type_id not in IGNORED_THREATS:
                found[e.tag] = e
    return list(found.values())


def fight_result(ai: AresBot, units: list[Unit], threats: list[Unit]) -> EngagementResult:
    center = Point2((sum(u.position.x for u in units) / len(units), sum(u.position.y for u in units) / len(units)))
    own = [
        u
        for u in ai.mediator.get_units_in_range(
            start_points=[center], distances=ENGAGE_RANGE + 4, query_tree=UnitTreeQueryType.AllOwn
        )[0]
        if u.type_id in ARMY_TYPES or u.type_id == UnitTypeId.QUEEN
    ]
    threats = [t for t in threats if not tank_neutralized(t, own)]
    if not threats:
        return EngagementResult.VICTORY_EMPHATIC
    return ai.mediator.can_win_fight(own_units=own, enemy_units=threats, workers_do_no_damage=True)


def tank_neutralized(enemy: Unit, own: list[Unit]) -> bool:
    """A sieged tank with several of our zerglings inside its minimum range.

    ares' combat sim ignores minimum range, so it rates such a tank at full damage even though it
    can't shoot those zerglings and will die before it can unsiege. We leave it out of the sim
    (it's still first in the dive priority, so it keeps getting attacked).

    NOTE: a judgement call that might need reverting if it causes big mistakes. The tank can
    still shoot zerglings that aren't on top of it, and leaving it out also drops its health
    from the sim, so predictions get a little optimistic. Requiring TANK_NEUTRALIZED_BY
    zerglings in its minimum range limits this to tanks we're clearly already on top of.
    """
    if enemy.type_id != UnitTypeId.SIEGETANKSIEGED:
        return False
    reach = SIEGE_MIN_RANGE + enemy.radius
    inside = sum(
        1 for u in own if u.type_id in ARMY_TYPES and u.position.distance_to(enemy.position) < reach + u.radius
    )
    return inside >= TANK_NEUTRALIZED_BY


def fight(
    ai: AresBot,
    unit: Unit,
    threats: list[Unit],
    enemies: list[Unit],
    slots: TargetSlots,
    squad_position: Point2,
    blocks: BlockTracker,
    detours: Detours,
) -> None:
    """Target choice for one zergling, in priority tiers; it takes the best-tier target that still
    has room (`TargetSlots`: melee attackers per target are capped by the space around it):

    1. siege tanks and other DIVE_FIRST units close by, then any other close enemy that fights back
    2. workers
    3. key structures (town halls, pylons, pools), then any structure, once no fighters are close
       (so zerglings don't chew on a barracks while marines and tanks shoot them). Exception: when
       every close fighter is full or blocked, the structure physically in the way (within
       BREAKTHROUGH_RANGE) is next, so zerglings break through instead of dying in a lane

    When a target is full the next zergling takes the next one (a full pylon doesn't hold up the
    gateway next to it). Targets fill from the back (TARGET_FILL "back"): zerglings are handled
    front-first and each takes the *deepest* target with room, so the first ones in run past the
    front line (and up ramps) and later ones fill in toward the front, instead of everyone stopping
    at the first enemy and blocking the ones behind. A zergling only stops to hit its own target.

    A zergling that stops getting closer to an enemy unit (`BlockTracker`) first takes a detour to
    it around our own crowd (`Detours`, e.g. around the outside of a block of gateways instead of
    queueing at a 1-tile gap). With no reasonable detour (or for a structure) it takes another
    target it can reach, breaking through the structure in its way if that's all there is; a
    zergling breaking through switches back to the units as soon as one is reachable.
    """
    position = unit.position
    fighters = [e for e in threats if e.type_id not in WORKER_TYPES]
    near_fighters = [e for e in fighters if e.position.distance_to(position) < FIGHTER_PRIORITY_RANGE]
    workers = [e for e in threats if e.type_id in WORKER_TYPES]
    structures = [e for e in enemies if e.is_structure]
    if near_fighters:
        dive = [e for e in near_fighters if e.type_id in DIVE_FIRST]
        # When every close fighter is full or blocked (e.g. zealots in 1-tile gaps between
        # gateways), break through the structure in the way rather than die in the lane
        in_the_way = [s for s in structures if s.position.distance_to(position) - s.radius < BREAKTHROUGH_RANGE]
        tiers = [
            (dive, True),
            ([e for e in near_fighters if e not in dive], True),
            (workers, False),
            (in_the_way, False),
        ]
    else:
        key = [s for s in structures if s.type_id in PRIORITY_STRUCTURES]
        tiers = [(fighters, True), (workers, True), (key, True), ([s for s in structures if s not in key], True)]
    tiers = [(t, back) for t, back in tiers if t]
    if not tiers:
        return  # nothing to go after (e.g. only far-off remembered units)

    now = ai.time
    maneuver = CombatManeuver()
    # On a detour around our own crowd: keep following it to that target
    if (route_target := detours.route_target(unit, now)) is not None:
        goal = next((e for t, _ in tiers for e in t if e.tag == route_target), None)
        waypoint = detours.next_waypoint(unit) if goal is not None else None
        if goal is not None and waypoint is not None and not unit.target_in_range(goal, 0.25):
            maneuver.add(ShootTargetInRange(unit=unit, targets=[goal]))
            maneuver.add(
                PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=waypoint, sense_danger=False)
            )
            ai.register_behavior(maneuver)
            return
        detours.drop(unit)

    if TARGET_FILL == "off":
        goal = min((e for t, _ in tiers for e in t), key=lambda e: e.position.distance_to(position))
        flank = False
    else:
        depth_from = squad_position if TARGET_FILL == "back" else None
        goal, flank = pick_tiered(slots, blocks, unit, tiers, depth_from, now)
    if goal is None:
        return
    if not flank and blocks.update(unit, goal, now):
        # Just got stuck on this one. An enemy unit: go around our own crowd to it if there's a
        # reasonable way; otherwise (or a structure) take another target we can reach
        crowd = [u for u in ai.units if u.tag != unit.tag and u.type_id in ARMY_TYPES and u.distance_to(unit) < 8]
        if not goal.is_structure and not goal.is_memory and detours.plan(ai, unit, goal, crowd, now):
            fight(ai, unit, threats, enemies, slots, squad_position, blocks, detours)
            return
        goal, flank = pick_tiered(slots, blocks, unit, tiers, None, now)

    if flank and not goal.is_memory:
        side = 1 if unit.tag % 2 else -1  # alternate sides so they spread around the jam
        away = goal.position.towards(unit.position, 1) - goal.position
        around = goal.position + Point2((-away.y, away.x)) * side * (goal.radius + FLANK_DISTANCE)
        maneuver.add(ShootTargetInRange(unit=unit, targets=[e for t, _ in tiers for e in t]))
        maneuver.add(PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=around, sense_danger=False))
        ai.register_behavior(maneuver)
        return
    # Only stop for *our* target: stopping at whatever we brush past blocks everyone behind
    maneuver.add(ShootTargetInRange(unit=unit, targets=[goal]))
    if chase := chase_behavior(unit, goal):
        maneuver.add(chase)
    ai.register_behavior(maneuver)


def chase_behavior(unit: Unit, goal: Unit) -> AttackTarget | AMove | None:
    """Go attack `goal`. Visible units get a direct attack order (only re-issued if the zergling
    isn't already on it). Remembered units in the fog can't be targeted directly, so attack-move
    to their last known spot; `success_at_distance=0` because AMove's default does nothing within
    7 of the target, which left zerglings stuck on their previous target."""
    if goal.is_memory:
        return AMove(unit=unit, target=goal.position, success_at_distance=0)
    if unit.order_target == goal.tag:
        return None
    return AttackTarget(unit=unit, target=goal)


def retreat(ai: AresBot, unit: Unit) -> None:
    move(ai, unit, rally_point(ai))


def move(ai: AresBot, unit: Unit, target: Point2) -> None:
    """Move along ares' path, avoiding known enemy fire."""
    ai.register_behavior(PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=target))


def rally_point(ai: AresBot) -> Point2:
    """In front of our base closest to the enemy."""
    base = ai.townhalls.closest_to(ai.enemy_start_locations[0])
    return base.position.towards(ai.game_info.map_center, 6)


def all_in(ai: AresBot) -> None:
    """No hatchery left: throw everything at the enemy."""
    for unit in ai.units.exclude_type({UnitTypeId.EGG, UnitTypeId.LARVA}):
        unit.attack(ai.enemy_start_locations[0])
