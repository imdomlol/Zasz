from ares import AresBot
from sc2.ids.unit_typeid import UnitTypeId

from bot.settings import DEFEND_RADIUS


def attack(ai: AresBot) -> None:
    target = ai.enemy_structures.random_or(ai.enemy_start_locations[0]).position
    for ling in ai.units(UnitTypeId.ZERGLING).idle:
        ling.attack(target)


def defend(ai: AresBot) -> None:
    """Attack anything near our bases."""
    threats = ai.enemy_units.filter(lambda u: ai.townhalls.closest_distance_to(u) < DEFEND_RADIUS)
    if threats:
        for ling in ai.units(UnitTypeId.ZERGLING):
            ling.attack(threats.closest_to(ling))


def all_in(ai: AresBot) -> None:
    """No hatchery left: throw everything at the enemy."""
    for unit in ai.units.exclude_type({UnitTypeId.EGG, UnitTypeId.LARVA}):
        unit.attack(ai.enemy_start_locations[0])
