from ares import AresBot
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId


def build_queens(ai: AresBot) -> None:
    """One queen per hatchery (including ones still building)."""
    if not ai.structures(UnitTypeId.SPAWNINGPOOL).ready:
        return
    queens = ai.units(UnitTypeId.QUEEN).amount + ai.already_pending(UnitTypeId.QUEEN)
    if queens < ai.townhalls.amount and ai.can_afford(UnitTypeId.QUEEN):
        for hatch in ai.townhalls.ready.idle:
            hatch.train(UnitTypeId.QUEEN)
            break


def inject_larva(ai: AresBot) -> None:
    # Pair each hatchery with its own queen (closest unassigned one)
    queens = list(ai.units(UnitTypeId.QUEEN))
    for hatch in ai.townhalls.ready:
        if not queens:
            return
        queen = min(queens, key=lambda q: q.distance_to(hatch))
        queens.remove(queen)
        if not queen.is_idle:
            continue
        if queen.distance_to(hatch) > 8:
            queen.move(hatch.position)
        elif queen.energy >= 25 and not hatch.has_buff(BuffId.QUEENSPAWNLARVATIMER):
            queen(AbilityId.EFFECT_INJECTLARVA, hatch)
