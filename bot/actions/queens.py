"""Queens: one injector per hatchery, extra queens spread creep, and all queens defend.

Queens are also Zasz's only anti-air unit, so they fight anything (ground or air) that comes
close to our bases before going back to their job.
"""

from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, QueenSpreadCreep, ShootTargetInRange, TumorSpreadCreep
from ares.consts import UnitTreeQueryType
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.unit import Unit

from bot.settings import QUEEN_DEFEND_RANGE


def build_queens(ai: AresBot, wanted: int) -> None:
    """Make queens (one at a time, from idle hatcheries) until we have `wanted` (`Decision.queens`)."""
    if not ai.structures(UnitTypeId.SPAWNINGPOOL).ready:
        return
    queens = ai.units(UnitTypeId.QUEEN).amount + ai.already_pending(UnitTypeId.QUEEN)
    if queens < wanted and ai.can_afford(UnitTypeId.QUEEN):
        for hatch in ai.townhalls.ready.idle:
            hatch.train(UnitTypeId.QUEEN)
            break


def control_queens(ai: AresBot) -> None:
    queens = list(ai.units(UnitTypeId.QUEEN))
    injectors = pair_injectors(ai, queens)
    for queen in queens:
        if defend(ai, queen):
            continue
        if queen.tag in injectors:
            inject(queen, injectors[queen.tag])
        else:
            ai.register_behavior(QueenSpreadCreep(unit=queen))
    spread_tumors(ai)


def pair_injectors(ai: AresBot, queens: list[Unit]) -> dict[int, Unit]:
    """Queen tag -> hatchery it injects. Each hatchery gets its closest unassigned queen."""
    pairs: dict[int, Unit] = {}
    free = list(queens)
    for hatch in ai.townhalls.ready:
        if not free:
            break
        queen = min(free, key=lambda q: q.distance_to(hatch))
        free.remove(queen)
        pairs[queen.tag] = hatch
    return pairs


def inject(queen: Unit, hatch: Unit) -> None:
    if not queen.is_idle:
        return
    if queen.distance_to(hatch) > 8:
        queen.move(hatch.position)
    elif queen.energy >= 25 and not hatch.has_buff(BuffId.QUEENSPAWNLARVATIMER):
        queen(AbilityId.EFFECT_INJECTLARVA, hatch)


def defend(ai: AresBot, queen: Unit) -> bool:
    """Fight enemies (ground or air) near this queen while it's near our bases."""
    if not ai.townhalls or ai.townhalls.closest_distance_to(queen) > QUEEN_DEFEND_RANGE:
        return False
    enemies = [
        e
        for e in ai.mediator.get_units_in_range(
            start_points=[queen.position], distances=QUEEN_DEFEND_RANGE, query_tree=UnitTreeQueryType.AllEnemy
        )[0]
        if e.can_attack or e.type_id in {UnitTypeId.OVERSEER, UnitTypeId.OBSERVER} or e.is_structure
    ]
    if not enemies:
        return False
    maneuver = CombatManeuver()
    maneuver.add(ShootTargetInRange(unit=queen, targets=enemies))
    maneuver.add(AMove(unit=queen, target=min(enemies, key=lambda e: e.position.distance_to(queen.position)).position))
    ai.register_behavior(maneuver)
    return True


def spread_tumors(ai: AresBot) -> None:
    for tumor in ai.mediator.get_own_structures_dict[UnitTypeId.CREEPTUMORBURROWED]:
        ai.register_behavior(TumorSpreadCreep(unit=tumor, target=ai.enemy_start_locations[0]))
