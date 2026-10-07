"""Static defense: spore crawlers in each mineral line (how many is `Decision.spores_per_base`).

Zerglings can't shoot up, so against air units the queens and spores are all we have.
"""

from ares import AresBot
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.settings import SPORE_RETRY_SECONDS

# Enemy tech that means air units are coming
AIR_TECH = {
    UnitTypeId.STARGATE,
    UnitTypeId.FLEETBEACON,
    UnitTypeId.STARPORT,
    UnitTypeId.STARPORTFLYING,
    UnitTypeId.FUSIONCORE,
    UnitTypeId.SPIRE,
    UnitTypeId.GREATERSPIRE,
}
HARMLESS_AIR = {UnitTypeId.OVERLORD, UnitTypeId.OVERSEER, UnitTypeId.OBSERVER, UnitTypeId.OVERLORDTRANSPORT}


def enemy_has_air(ai: AresBot) -> bool:
    if any(s.type_id in AIR_TECH for s in ai.enemy_structures):
        return True
    return any(u.is_flying and u.type_id not in HARMLESS_AIR for u in ai.mediator.get_cached_enemy_army)


async def build_spores(ai: AresBot, per_base: int) -> None:
    if not per_base or not ai.can_afford(UnitTypeId.SPORECRAWLER):
        return
    if ai.already_pending(UnitTypeId.SPORECRAWLER) or ai.structure_pending(UnitTypeId.SPORECRAWLER):
        return  # one at a time
    spores = ai.structures(UnitTypeId.SPORECRAWLER)
    # Bases where a spore couldn't be placed (or no drone found) lately: the next base gets a go, and the
    # placement query isn't repeated every step
    failed = ai.action_state.spore_failed_at
    for hatch in ai.townhalls.ready:
        if spores.closer_than(8, hatch).amount >= per_base:
            continue
        if ai.time - failed.get(hatch.tag, -SPORE_RETRY_SECONDS) < SPORE_RETRY_SECONDS:
            continue
        if spot := mineral_line_spot(ai, hatch):
            pos = await ai.find_placement(UnitTypeId.SPORECRAWLER, near=spot, max_distance=4)
            if pos and (drone := ai.mediator.select_worker(target_position=pos)):
                ai.mediator.build_with_specific_worker(worker=drone, structure_type=UnitTypeId.SPORECRAWLER, pos=pos)
                return
            failed[hatch.tag] = ai.time


def mineral_line_spot(ai: AresBot, hatch: Unit) -> Point2 | None:
    """Between the hatchery and its minerals, where a spore covers the drones."""
    minerals = ai.mineral_field.closer_than(10, hatch)
    if not minerals:
        return None
    return hatch.position.towards(minerals.center, 3.5)
