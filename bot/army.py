"""Army control: squads, fight-or-retreat, and per-unit target selection.

Zerglings get the ATTACKING role when they hatch (see `ZaszBot.on_unit_created`). ares groups
them into squads; each squad then:

1. Fights if enemies are close and the combat sim predicts a win (or we're defending a base),
   and keeps fighting only while the sim predicts at least an even fight. Zerglings dive
   siege tanks first instead of trading with them from the edge of their range.
2. Retreats if the sim says the fight is lost, and keeps retreating until it's out of range.
3. Otherwise regroups with the main squad before moving on, so we don't trickle in.

Threats are looked up around every unit in the squad (not just its center), and units that
are being shot from out of sight count as "under fire", so long-range units like sieged tanks
can't hit the front of the squad unnoticed.

Whether to attack at all is a decision (`Decisions.should_attack`); this module only executes.
"""

from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, PathUnitToTarget, ShootTargetInRange
from ares.consts import TOWNHALL_TYPES, WORKER_TYPES, EngagementResult, UnitRole, UnitTreeQueryType
from ares.managers.squad_manager import UnitSquad
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.settings import (
    DEFEND_RADIUS,
    ENGAGE_RANGE,
    KEEP_FIGHTING_AT,
    REGROUP_DISTANCE,
    RETREAT_SECONDS,
    SQUAD_RADIUS,
    START_FIGHT_AT,
    UNDER_FIRE_SECONDS,
)

ARMY_TYPES = {UnitTypeId.ZERGLING}
IGNORED_THREATS = {UnitTypeId.LARVA, UnitTypeId.EGG, UnitTypeId.OVERLORD, UnitTypeId.OVERSEER}
# Long-range or splash units zerglings should dive first rather than trade with from range
DIVE_FIRST = {UnitTypeId.SIEGETANKSIEGED, UnitTypeId.SIEGETANK, UnitTypeId.LURKERMPBURROWED, UnitTypeId.COLOSSUS}
# Structures worth killing first once their defenders are dead: economy, then power/production
PRIORITY_STRUCTURES = TOWNHALL_TYPES | {UnitTypeId.PYLON, UnitTypeId.SPAWNINGPOOL}


class Army:
    """Keeps the per-unit memory that stops squads flip-flopping between fight and retreat."""

    def __init__(self) -> None:
        self.fighting: set[int] = set()  # unit tags that were fighting last step
        self.retreating_until: dict[int, float] = {}  # unit tag -> keep retreating until this time
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
        retreating = sum(self.retreating_until.get(u.tag, 0) > now for u in units) > len(units) / 2
        in_fight = sum(u.tag in self.fighting for u in units) > len(units) / 2
        under_fire = any(now - self.last_damaged.get(u.tag, -99) < UNDER_FIRE_SECONDS for u in units)

        if retreating:
            for unit in units:
                retreat(ai, unit)
            return

        if threats:
            near_home = ai.townhalls.closest_distance_to(squad.squad_position) < DEFEND_RADIUS
            result = fight_result(ai, units, threats)
            # Start a fight only if the sim predicts a win; stay only while it predicts at least
            # an even fight. Never keep feeding units into a predicted loss. The gap between the
            # two thresholds is what stops squads flip-flopping at the edge of enemy range.
            if near_home or result >= (KEEP_FIGHTING_AT if in_fight else START_FIGHT_AT):
                for unit in units:
                    self.fighting.add(unit.tag)
                    fight(ai, unit, threats, enemies)
            else:
                for unit in units:
                    self.fighting.discard(unit.tag)
                    self.retreating_until[unit.tag] = now + RETREAT_SECONDS
                    retreat(ai, unit)
            return

        for unit in units:
            self.fighting.discard(unit.tag)

        if under_fire and not advancing:
            # Shot by something we can't see, and not attacking: get out of there
            for unit in units:
                self.retreating_until[unit.tag] = now + RETREAT_SECONDS
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
    return ai.mediator.can_win_fight(own_units=own, enemy_units=threats, workers_do_no_damage=True)


def fight(ai: AresBot, unit: Unit, threats: list[Unit], enemies: list[Unit]) -> None:
    """Target priority: anything in range that fights back > workers in range > key structures
    in range > any structure in range. Otherwise close in, diving siege tanks and the like first.
    Picking the weakest target in range spreads zerglings out instead of piling onto one."""
    fighters = [e for e in threats if e.type_id not in WORKER_TYPES]
    workers = [e for e in threats if e.type_id in WORKER_TYPES]
    structures = [e for e in enemies if e.is_structure]
    key_structures = [s for s in structures if s.type_id in PRIORITY_STRUCTURES]

    maneuver = CombatManeuver()
    for targets in (fighters, workers, key_structures, structures):
        if targets:
            maneuver.add(ShootTargetInRange(unit=unit, targets=targets))

    dive = [e for e in fighters if e.type_id in DIVE_FIRST]
    goal_pool = dive or fighters or workers or key_structures or structures
    goal = min(goal_pool, key=lambda e: e.position.distance_to(unit.position))
    maneuver.add(AMove(unit=unit, target=goal.position))
    ai.register_behavior(maneuver)


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
