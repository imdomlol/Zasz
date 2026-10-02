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

from collections import Counter
from dataclasses import dataclass

from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, AttackTarget, PathUnitToTarget, ShootTargetInRange
from ares.behaviors.combat.individual.combat_individual_behavior import CombatIndividualBehavior
from ares.consts import TOWNHALL_TYPES, WORKER_TYPES, EngagementResult, UnitRole, UnitTreeQueryType
from ares.managers.squad_manager import UnitSquad
from loguru import logger
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
    CLEAR_THE_WAY_RANGE,
    DEFEND_RADIUS,
    ENGAGE_RANGE,
    FIGHT_AT,
    FIGHTER_PRIORITY_RANGE,
    FLANK_DISTANCE,
    LEVEL_TOLERANCE,
    LOOKOUT_FLEE_MARGIN,
    MEMORY_THREAT_MARGIN,
    MIN_OPEN_SIDES,
    PLAN_SECONDS,
    REACH_SECONDS,
    REGROUP_DISTANCE,
    SCOUT_ZERGLINGS,
    SQUAD_RADIUS,
    TANK_NEUTRALIZED_BY,
    TARGET_FILL,
    THREAT_REACH_MARGIN,
    UNDER_FIRE_SECONDS,
    VISIBLE_THREAT_MARGIN,
)
from bot.targeting import BlockTracker, Detours, ReachMap, TargetSlots, pick_tiered

ARMY_TYPES = {UnitTypeId.ZERGLING}
COUNTS: Counter[str] = Counter()  # debug: zergling decisions since the last log (see Army.debug)
SIEGE_MIN_RANGE = 2.0  # sieged tanks can't fire at targets this close
IGNORED_THREATS = {UnitTypeId.LARVA, UnitTypeId.EGG, UnitTypeId.OVERLORD, UnitTypeId.OVERSEER}
# Long-range or splash units zerglings should dive first rather than trade with from range
DIVE_FIRST = {UnitTypeId.SIEGETANKSIEGED, UnitTypeId.SIEGETANK, UnitTypeId.LURKERMPBURROWED, UnitTypeId.COLOSSUS}
# Structures worth killing first once their defenders are dead: economy, then power/production
PRIORITY_STRUCTURES = TOWNHALL_TYPES | {UnitTypeId.PYLON, UnitTypeId.SPAWNINGPOOL}


@dataclass
class MoveTo(CombatIndividualBehavior):
    """Plain move to a point (a step along our own path)."""

    unit: Unit
    target: Point2

    def execute(self, ai: AresBot, config: dict, mediator) -> bool:
        self.unit.move(self.target)
        return True


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
        self.reach_cache: dict[tuple[int, int], tuple[float, ReachMap]] = {}  # area -> (built at, map)
        # Debug: count what zerglings decide, logged every few seconds (scenarios switch this on)
        self.debug = False
        self.counts = COUNTS
        self.counts_logged_at = 0.0
        self.scouts: dict[int, Point2] = {}  # scouting zergling tag -> spot it's checking
        self.armies_checked_at = -999.0
        # Committed: attack and keep fighting whatever the combat sim says (no avoid areas, no
        # retreats). Off in games; scenarios that test execution against a fight the sim calls a
        # loss switch it on
        self.committed = False

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
        if self.debug and ai.time - self.counts_logged_at >= 3:
            self.counts_logged_at = ai.time
            mode = "plan" if self.avoided(ai) else "army"
            lings = len([u for u in ai.units if u.type_id in ARMY_TYPES])
            seen = Counter(u.type_id.name.lower() for u in ai.enemy_units if not u.is_memory)
            seen.update(s.type_id.name.lower() for s in ai.enemy_structures if not s.is_memory)
            summary = ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items()))
            enemy = " ".join(f"{k} {v}" for k, v in sorted(seen.items()))
            logger.info(f"{ai.time_formatted} army debug: mode {mode}, {lings} zerglings vs {enemy} | {summary}")
            self.counts.clear()
        home_threats = [
            u for u in ai.mediator.get_main_ground_threats_near_townhall if u.type_id not in IGNORED_THREATS
        ]
        if home_threats:
            # Defending always comes first: every squad goes for the threat
            self.control_squads(ai, Point2(home_threats[0].position), advancing=True)
            return
        attacking = attacking or self.committed
        self.control_scouts(ai, attacking)
        if attacking and not self.committed and ai.time - self.armies_checked_at >= PLAN_SECONDS:
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

    def reach_map(self, ai: AresBot, units: list[Unit], enemies: list[Unit]) -> ReachMap | None:
        """Which enemy units this squad can actually get to; rebuilt every REACH_SECONDS per area."""
        ground = [e for e in enemies if not e.is_flying]
        if not ground:
            return None
        center = center_of(units)
        key = (round(center.x / 10), round(center.y / 10))
        cached = self.reach_cache.get(key)
        if cached is None or ai.time - cached[0] >= REACH_SECONDS:
            cached = (ai.time, ReachMap(ai.mediator.get_ground_grid, units, ground, ai.get_terrain_z_height))
            self.reach_cache[key] = cached
        return cached[1]

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
        """Where to attack: a known enemy base (least defended, far from their main first), else
        another structure, both outside the areas we were turned back from (or inside one but
        hittable from outside known enemy range), else known enemy ground units. Only things our
        army can hit: zerglings can't hit a floating building, so it isn't a target (it would pull
        the whole army to it). None if there's nothing."""
        anti_air = any(u.can_attack_air for u in ai.units if u.type_id in ARMY_TYPES)

        def hittable(e: Unit) -> bool:
            return anti_air or not e.is_flying

        enemy_main = ai.enemy_start_locations[0]
        rally = rally_point(ai)
        bases = [
            s
            for s in ai.enemy_structures
            if s.type_id in TOWNHALL_TYPES and hittable(s) and not self.blocked(ai, s.position)
        ]
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
        structures = [s for s in ai.enemy_structures if hittable(s) and not self.blocked(ai, s.position)]
        if structures:
            return min(structures, key=lambda s: s.position.distance_to(rally)).position
        # Inside an avoided area, but hittable from outside known enemy range: still fair game
        grid = ai.mediator.get_ground_grid
        safe = [
            s
            for s in ai.enemy_structures
            if hittable(s)
            and ai.mediator.is_position_safe(grid=grid, position=s.position.towards(rally, s.radius + 0.6))
        ]
        if safe:
            return min(safe, key=lambda s: s.position.distance_to(rally)).position
        # No structures we can hit: hunt the enemy units we know about
        units = [u for u in ai.mediator.get_cached_enemy_army if hittable(u) and not self.blocked(ai, u.position)]
        if units:
            return min(units, key=lambda u: u.position.distance_to(rally)).position
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
            if near_home or self.committed or result >= FIGHT_AT:
                slots = TargetSlots(self.reach_map(ai, units, enemies))
                for unit in front_first(units, enemies):
                    fight(ai, unit, threats, enemies, slots, squad.squad_position, self.blocks, self.detours)
            else:
                self.turned_back(ai, threats)
                COUNTS[f"retreat ({result.name})"] += len(units)
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

        slots = TargetSlots(self.reach_map(ai, units, enemies) if enemies else None)
        for unit in front_first(units, enemies):
            if advancing:
                if threats or any(e.is_structure for e in enemies):
                    fight(ai, unit, threats, enemies, slots, squad.squad_position, self.blocks, self.detours)
                elif under_fire or unit.distance_to(target) < APPROACH_DISTANCE:
                    # Close to the target (or shot from out of sight): attack-move in, all the way
                    # (AMove's default does nothing within 7 of the target: zerglings stood next
                    # to a lone zealot that wasn't a threat yet, doing nothing)
                    if not (unit.is_attacking and unit.order_target == target):
                        ai.register_behavior(AMove(unit=unit, target=target, success_at_distance=0))
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
    """The combat sim's verdict for a fight against `threats`, counting every army unit of ours near
    them (not just this squad: ares squads are small clusters, and judging each on its own against
    the whole enemy group made every one of them retreat from a fight the army as a whole wins)."""
    center = center_of(threats)
    own = [
        u
        for u in ai.mediator.get_units_in_range(
            start_points=[center], distances=ENGAGE_RANGE + 12, query_tree=UnitTreeQueryType.AllOwn
        )[0]
        if u.type_id in ARMY_TYPES or u.type_id == UnitTypeId.QUEEN
    ]
    own = own or list(units)
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
    # Fighters that can hit this zergling (touching, or within their weapon range + a little): fight
    # back at those. One further away (a stalker behind a wall of gateways) isn't worth streaming
    # toward along a long path: keep clearing, and fight it once it's shooting at us
    near_fighters = [
        e
        for e in fighters
        if e.position.distance_to(position) < FIGHTER_PRIORITY_RANGE
        and e.position.distance_to(position) - e.radius - unit.radius < e.ground_range + THREAT_REACH_MARGIN
    ]
    workers = [e for e in threats if e.type_id in WORKER_TYPES]
    structures = [e for e in enemies if e.is_structure]
    if slots.reach is not None:
        # Only structures we can actually stand next to (not ones on top of a cliff from here)
        structures = [s for s in structures if slots.reach.reachable(s)]

        # Only enemy units we can surround (enough open cells reachable around them); fighting one
        # through a 1-tile gap is a 1-on-1 we lose. Exception: a ranged one right next to us, hit it
        # back (stepping away doesn't get us out of its range). A melee one plugging a gap (a zealot
        # between two gateways): step out of its reach instead, it gets surrounded once the
        # buildings next to it fall
        def touching(e: Unit) -> bool:
            return e.position.distance_to(position) - e.radius - unit.radius < 0.75

        def surroundable(e: Unit) -> bool:
            return slots.reach.open_sides(e, unit.radius) >= MIN_OPEN_SIDES

        pinned = [e for e in near_fighters if touching(e) and not surroundable(e) and e.ground_range <= 1]
        if pinned:
            nearest = min(pinned, key=lambda e: e.position.distance_to(position))
            COUNTS["step away (gap)"] += 1
            unit.move(nearest.position.towards(position, nearest.radius + unit.radius + 2.0))
            return
        near_fighters = [e for e in near_fighters if surroundable(e) or touching(e)]
        workers = [e for e in workers if slots.reach.reachable(e)]
    if near_fighters:
        dive = [e for e in near_fighters if e.type_id in DIVE_FIRST]
        # When every close fighter is full or blocked (e.g. zealots in 1-tile gaps between
        # gateways), break through the structure in the way rather than die in the lane
        in_the_way = by_safety(
            ai, [s for s in structures if s.position.distance_to(position) - s.radius < BREAKTHROUGH_RANGE], unit
        )
        tiers = [
            (dive, True),
            ([e for e in near_fighters if e not in dive], True),
            (workers, False),
            (in_the_way, False),
        ]
    elif any(e.type_id not in WORKER_TYPES for e in threats):
        # Fighters around but none we can reach: clear the way through the closest structures
        # (front first) instead of milling about in their fire
        clear = by_safety(
            ai, [s for s in structures if s.position.distance_to(position) - s.radius < CLEAR_THE_WAY_RANGE], unit
        )
        tiers = [(workers, False), (clear, False)]
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
            COUNTS["detour follow"] += 1
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
    same_level = goal.is_flying or abs(ai.get_terrain_z_height(unit) - ai.get_terrain_z_height(goal)) < LEVEL_TOLERANCE
    if not flank and blocks.update(unit, goal, now, same_level):
        # Just got stuck on this one. An enemy unit: go around our own crowd to it if there's a
        # reasonable way; otherwise (or a structure) take another target we can reach
        crowd = [u for u in ai.units if u.tag != unit.tag and u.type_id in ARMY_TYPES and u.distance_to(unit) < 8]
        if not goal.is_structure and not goal.is_memory and detours.plan(ai, unit, goal, crowd, now):
            COUNTS["detour planned"] += 1
            fight(ai, unit, threats, enemies, slots, squad_position, blocks, detours)
            return
        COUNTS["blocked, repick"] += 1
        goal, flank = pick_tiered(slots, blocks, unit, tiers, None, now)
    if flank:
        category = "flank"
    elif goal.is_structure and near_fighters:
        category = "break through"
    elif goal.is_structure:
        category = "structure"
    else:
        category = "unit"
        if unit.target_in_range(goal, 0.25):
            COUNTS["unit in range"] += 1
    COUNTS[category] += 1
    if not ai.mediator.is_position_safe(grid=ai.mediator.get_ground_grid, position=position):
        COUNTS[f"UNSAFE {category}"] += 1

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
    # An enemy unit: walk around the other enemy units to it (e.g. around the outside of a gateway
    # grid instead of down a lane a zealot plugs), then attack it
    if (
        not goal.is_structure
        and not goal.is_memory
        and slots.reach is not None
        and not unit.target_in_range(goal, 0.5)
        and (step := slots.reach.approach_step(goal, position)) is not None
    ):
        COUNTS["approach around"] += 1
        maneuver.add(MoveTo(unit=unit, target=step))
        ai.register_behavior(maneuver)
        return
    if chase := chase_behavior(unit, goal):
        maneuver.add(chase)
    ai.register_behavior(maneuver)


def by_safety(ai: AresBot, structures: list[Unit], unit: Unit) -> list[Unit]:
    """Structures we can hit from outside known enemy weapon range first, then the rest; closest
    first within each (front-row gateways come down without stalker fire)."""
    grid = ai.mediator.get_ground_grid

    def key(s: Unit) -> tuple[bool, float]:
        near_side = s.position.towards(unit.position, s.radius + unit.radius + 0.2)
        safe = ai.mediator.is_position_safe(grid=grid, position=near_side)
        return (not safe, s.position.distance_to(unit.position))

    return sorted(structures, key=key)


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
