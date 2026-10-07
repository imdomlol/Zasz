"""Queens: one injector per hatchery, extra queens spread creep, and all queens defend.

Queens are also Zasz's only anti-air unit, so they fight anything (ground or air) that comes
close to our bases before going back to their job.
"""

from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, QueenSpreadCreep, ShootTargetInRange, TumorSpreadCreep
from ares.consts import UnitTreeQueryType
from loguru import logger
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
    note_injector_switches(ai, queens, injectors)
    for queen in queens:
        if defend(ai, queen):
            continue
        if queen.tag in injectors:
            inject(queen, injectors[queen.tag])
        else:
            ai.register_behavior(QueenSpreadCreep(unit=queen))
    spread_tumors(ai)


def pair_injectors(ai: AresBot, queens: list[Unit]) -> dict[int, Unit]:
    """Queen tag -> hatchery it injects. A hatchery keeps its injector while both live; one without (new,
    or its queen died) gets the closest queen that isn't injecting. (Re-pairing every step by distance
    made two queens swap two hatcheries back and forth, ~1,000 swaps a game, walking instead of injecting.)"""
    kept: dict[int, int] = ai.__dict__.setdefault("injectors", {})  # hatchery tag -> queen tag
    alive = {q.tag for q in queens}
    pairs: dict[int, Unit] = {}
    hatcheries = list(ai.townhalls.ready)
    for hatch in hatcheries:
        if (queen := kept.get(hatch.tag)) in alive and queen not in pairs:
            pairs[queen] = hatch
    paired = {hatch.tag for hatch in pairs.values()}
    free = [q for q in queens if q.tag not in pairs]
    for hatch in hatcheries:
        if hatch.tag in paired or not free:
            continue
        queen = min(free, key=lambda q: q.distance_to(hatch))
        free.remove(queen)
        pairs[queen.tag] = hatch
    kept.clear()
    kept.update({hatch.tag: queen for queen, hatch in pairs.items()})
    return pairs


def note_injector_switches(ai: AresBot, queens: list[Unit], injectors: dict[int, Unit]) -> None:
    """Log a hatchery's injector changing while its old one is still alive (the pairing
    used to churn every step; this should now stay silent)."""
    previous: dict[int, int] = ai.__dict__.setdefault("injector_of", {})  # hatchery tag -> queen tag
    alive = {q.tag for q in queens}
    for queen, hatch in injectors.items():
        old = previous.get(hatch.tag)
        if old is not None and old != queen and old in alive:
            logger.info(f"{ai.time_formatted} injector switch at hatchery {hatch.tag}: queen {old} -> {queen}")
    previous.clear()
    previous.update({hatch.tag: queen for queen, hatch in injectors.items()})


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
