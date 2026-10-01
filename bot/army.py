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

Whether to attack at all is a decision (`Decisions.should_attack`); this module only executes.
"""

from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, AttackTarget, PathUnitToTarget, ShootTargetInRange
from ares.consts import TOWNHALL_TYPES, WORKER_TYPES, EngagementResult, UnitRole, UnitTreeQueryType
from ares.managers.squad_manager import UnitSquad
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.settings import (
    DEFEND_RADIUS,
    ENGAGE_RANGE,
    FIGHT_AT,
    FIGHTER_PRIORITY_RANGE,
    REGROUP_DISTANCE,
    SQUAD_RADIUS,
    TANK_NEUTRALIZED_BY,
    UNDER_FIRE_SECONDS,
)

ARMY_TYPES = {UnitTypeId.ZERGLING}
SIEGE_MIN_RANGE = 2.0  # sieged tanks can't fire at targets this close
IGNORED_THREATS = {UnitTypeId.LARVA, UnitTypeId.EGG, UnitTypeId.OVERLORD, UnitTypeId.OVERSEER}
# Long-range or splash units zerglings should dive first rather than trade with from range
DIVE_FIRST = {UnitTypeId.SIEGETANKSIEGED, UnitTypeId.SIEGETANK, UnitTypeId.LURKERMPBURROWED, UnitTypeId.COLOSSUS}
# Structures worth killing first once their defenders are dead: economy, then power/production
PRIORITY_STRUCTURES = TOWNHALL_TYPES | {UnitTypeId.PYLON, UnitTypeId.SPAWNINGPOOL}


class Army:
    """Keeps the per-unit memory that stops squads flip-flopping between fight and retreat."""

    def __init__(self) -> None:
        self.retreating: set[int] = set()  # unit tags retreating until they're out of enemy range
        self.last_damaged: dict[int, float] = {}  # unit tag -> last time it took damage

    def on_damage(self, ai: AresBot, unit: Unit) -> None:
        self.last_damaged[unit.tag] = ai.time

    def control(self, ai: AresBot, attacking: bool) -> None:
        squads: list[UnitSquad] = ai.mediator.get_squads(role=UnitRole.ATTACKING, squad_radius=SQUAD_RADIUS)
        if not squads:
            return

        home_threats = [
            u for u in ai.mediator.get_main_ground_threats_near_townhall if u.type_id not in IGNORED_THREATS
        ]
        if home_threats:
            # Defending always comes first
            target = Point2(home_threats[0].position)
        elif attacking:
            target = attack_target(ai)
        else:
            target = rally_point(ai)
        main_squad_position = ai.mediator.get_position_of_main_squad(role=UnitRole.ATTACKING)

        for squad in squads:
            self.control_squad(ai, squad, target, attacking or bool(home_threats), main_squad_position)

    def control_squad(
        self, ai: AresBot, squad: UnitSquad, target: Point2, advancing: bool, main_position: Point2
    ) -> None:
        units = squad.squad_units
        enemies = enemies_near(ai, units)
        threats = [e for e in enemies if not e.is_structure or e.can_attack_ground]
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
                for unit in units:
                    fight(ai, unit, threats, enemies)
            else:
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

        for unit in units:
            if advancing:
                if enemies:
                    fight(ai, unit, threats, enemies)
                else:
                    # Includes pushing forward when shot from out of sight, to reveal the shooter
                    ai.register_behavior(AMove(unit=unit, target=target))
            elif unit.distance_to(target) > 5:
                move(ai, unit, target)


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


def fight(ai: AresBot, unit: Unit, threats: list[Unit], enemies: list[Unit]) -> None:
    """Target priority for one zergling.

    If enemy units that fight back are within FIGHTER_PRIORITY_RANGE, they're the only targets:
    hit the weakest one in range, otherwise go straight at the closest (siege tanks and other
    DIVE_FIRST units first). Buildings and workers wait until no fighters are near, so zerglings
    don't chew on a barracks while marines and tanks shoot them.

    Otherwise: workers > key structures > any structure in range, then close in on the nearest.
    Picking the weakest target in range spreads zerglings out instead of piling onto one.
    """
    position = unit.position
    fighters = [e for e in threats if e.type_id not in WORKER_TYPES]
    near_fighters = [e for e in fighters if e.position.distance_to(position) < FIGHTER_PRIORITY_RANGE]

    maneuver = CombatManeuver()
    if near_fighters:
        maneuver.add(ShootTargetInRange(unit=unit, targets=near_fighters))
        pool = [e for e in near_fighters if e.type_id in DIVE_FIRST] or near_fighters
    else:
        workers = [e for e in threats if e.type_id in WORKER_TYPES]
        structures = [e for e in enemies if e.is_structure]
        key_structures = [s for s in structures if s.type_id in PRIORITY_STRUCTURES]
        for targets in (workers, key_structures, structures):
            if targets:
                maneuver.add(ShootTargetInRange(unit=unit, targets=targets))
        pool = fighters or workers or key_structures or structures
    goal = min(pool, key=lambda e: e.position.distance_to(position))
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


def attack_target(ai: AresBot) -> Point2:
    """Where to attack: the least defended known enemy base, preferring ones far from their main
    (expansions are expensive and easier to hit, and hitting a far one pulls their army away).
    Falls back to any known structure, then sweeps expansions to find hidden buildings."""
    enemy_main = ai.enemy_start_locations[0]
    bases = [s for s in ai.enemy_structures if s.type_id in TOWNHALL_TYPES]
    if bases:
        army = ai.mediator.get_cached_enemy_army

        def defense(base: Unit) -> tuple[float, float]:
            spot = base.position
            nearby_supply = sum(ai.calculate_supply_cost(u.type_id) for u in army if u.position.distance_to(spot) < 15)
            static = sum(1 for s in ai.enemy_structures if s.can_attack_ground and s.position.distance_to(spot) < 10)
            return nearby_supply + 4 * static, -spot.distance_to(enemy_main)

        return min(bases, key=defense).position
    if ai.enemy_structures:
        return ai.enemy_structures.closest_to(rally_point(ai)).position
    if not ai.is_visible(enemy_main):
        return enemy_main
    # Visit expansions in turn, farthest from us first, changing every 20 seconds
    expansions = sorted(ai.expansion_locations_list, key=lambda p: -p.distance_to(ai.start_location))
    return expansions[int(ai.time // 20) % len(expansions)]


def all_in(ai: AresBot) -> None:
    """No hatchery left: throw everything at the enemy."""
    for unit in ai.units.exclude_type({UnitTypeId.EGG, UnitTypeId.LARVA}):
        unit.attack(ai.enemy_start_locations[0])
