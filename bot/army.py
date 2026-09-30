"""Army control: squads, fight-or-retreat, and per-unit target selection.

Zerglings get the ATTACKING role when they hatch (see `ZaszBot.on_unit_created`). ares groups
them into squads; each squad then:

1. Fights if enemies are close and the combat sim says we win (or we're defending a base),
   each zergling hitting the weakest enemy in range instead of everyone chasing one target.
2. Retreats to the rally point if the sim says the fight is lost.
3. Otherwise regroups with the main squad before moving on, so we don't trickle in.

Whether to attack at all is a decision (`Decisions.should_attack`); this module only executes.
"""

from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, PathUnitToTarget, ShootTargetInRange
from ares.consts import UnitRole, UnitTreeQueryType
from ares.managers.squad_manager import UnitSquad
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.settings import DEFEND_RADIUS, ENGAGE_RANGE, REGROUP_DISTANCE, RETREAT_AT, SQUAD_RADIUS

ARMY_TYPES = {UnitTypeId.ZERGLING}
# Workers and harmless units don't count as a threat worth fighting over
IGNORED_THREATS = {UnitTypeId.LARVA, UnitTypeId.EGG, UnitTypeId.OVERLORD, UnitTypeId.OVERSEER}


def control(ai: AresBot, attacking: bool) -> None:
    squads: list[UnitSquad] = ai.mediator.get_squads(role=UnitRole.ATTACKING, squad_radius=SQUAD_RADIUS)
    if not squads:
        return

    home_threats = [u for u in ai.mediator.get_main_ground_threats_near_townhall if u.type_id not in IGNORED_THREATS]
    if home_threats:
        # Defending always comes first
        target = Point2(home_threats[0].position)
    elif attacking:
        target = attack_target(ai)
    else:
        target = rally_point(ai)
    main_squad_position = ai.mediator.get_position_of_main_squad(role=UnitRole.ATTACKING)

    for squad in squads:
        control_squad(ai, squad, target, attacking or bool(home_threats), main_squad_position)


def control_squad(ai: AresBot, squad: UnitSquad, target: Point2, advancing: bool, main_position: Point2) -> None:
    position = squad.squad_position
    enemies = [
        e
        for e in ai.mediator.get_units_in_range(
            start_points=[position], distances=ENGAGE_RANGE, query_tree=UnitTreeQueryType.EnemyGround
        )[0]
        if e.type_id not in IGNORED_THREATS
    ]
    threats = [e for e in enemies if not e.is_structure or e.can_attack_ground]

    if threats:
        near_home = ai.townhalls.closest_distance_to(position) < DEFEND_RADIUS
        if near_home or can_win(ai, position, threats):
            for unit in squad.squad_units:
                fight(ai, unit, threats, enemies)
        else:
            for unit in squad.squad_units:
                retreat(ai, unit)
        return

    if advancing and not squad.main_squad and position.distance_to(main_position) > REGROUP_DISTANCE:
        # Join the main squad before going further
        for unit in squad.squad_units:
            move(ai, unit, main_position)
        return

    for unit in squad.squad_units:
        if advancing:
            if enemies:
                fight(ai, unit, threats, enemies)
            else:
                ai.register_behavior(AMove(unit=unit, target=target))
        elif unit.distance_to(target) > 5:
            move(ai, unit, target)


def can_win(ai: AresBot, position: Point2, threats: list[Unit]) -> bool:
    own = [
        u
        for u in ai.mediator.get_units_in_range(
            start_points=[position], distances=ENGAGE_RANGE + 4, query_tree=UnitTreeQueryType.AllOwn
        )[0]
        if u.type_id in ARMY_TYPES or u.type_id == UnitTypeId.QUEEN
    ]
    result = ai.mediator.can_win_fight(own_units=own, enemy_units=threats, workers_do_no_damage=True)
    return result > RETREAT_AT


def fight(ai: AresBot, unit: Unit, threats: list[Unit], enemies: list[Unit]) -> None:
    """Hit the weakest threat in range, then anything in range, else close in on the nearest
    threat. Spreads zerglings over targets instead of piling onto one."""
    maneuver = CombatManeuver()
    maneuver.add(ShootTargetInRange(unit=unit, targets=threats))
    maneuver.add(ShootTargetInRange(unit=unit, targets=enemies))
    goal = min(threats or enemies, key=lambda e: e.distance_to(unit))
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
    """Nearest known enemy structure; if none are known, sweep expansions to find the rest."""
    if ai.enemy_structures:
        return ai.enemy_structures.closest_to(rally_point(ai)).position
    enemy_start = ai.enemy_start_locations[0]
    if not ai.is_visible(enemy_start):
        return enemy_start
    # Visit expansions in turn, farthest from us first, changing every 20 seconds
    expansions = sorted(ai.expansion_locations_list, key=lambda p: -p.distance_to(ai.start_location))
    return expansions[int(ai.time // 20) % len(expansions)]


def all_in(ai: AresBot) -> None:
    """No hatchery left: throw everything at the enemy."""
    for unit in ai.units.exclude_type({UnitTypeId.EGG, UnitTypeId.LARVA}):
        unit.attack(ai.enemy_start_locations[0])
