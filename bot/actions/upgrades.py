"""Carry out an upgrade wish list: gas, tech buildings and research.

Decisions only say *which* upgrades we want and in what order (`Decisions.upgrades`).
Everything here follows from that list, so a model can change it freely each step.
"""

from ares import AresBot
from ares.behaviors.macro import GasBuildingController, MacroPlan, UpgradeController
from sc2.ids.upgrade_id import UpgradeId

GAS_PER_EXTRACTOR = 400  # add another extractor for each this much gas still needed


def remaining_upgrades(ai: AresBot, upgrades: list[UpgradeId]) -> list[UpgradeId]:
    return [u for u in upgrades if not ai.pending_or_complete_upgrade(u)]


def remaining_gas(ai: AresBot, upgrades: list[UpgradeId]) -> int:
    return sum(ai.calculate_cost(u).vespene for u in remaining_upgrades(ai, upgrades))


def workers_per_gas(ai: AresBot, gas: int) -> int:
    """Mine gas only until we've banked enough for the upgrades we still want (`gas`:
    `remaining_gas`), then send those drones back to minerals (ares' `Mining` moves them)."""
    return 3 if gas > ai.vespene else 0


def extractors_wanted(ai: AresBot, gas: int) -> int:
    if gas <= ai.vespene:
        return 0
    return min(1 + gas // GAS_PER_EXTRACTOR, ai.townhalls.ready.amount)


def upgrade_plan(ai: AresBot, upgrades: list[UpgradeId], gas: int) -> MacroPlan:
    """`gas`: what the upgrades still to do cost (`remaining_gas`)."""
    plan = MacroPlan()
    if extractors := extractors_wanted(ai, gas):
        # `to_count` is a minimum: extractors we already have are never destroyed
        plan.add(GasBuildingController(to_count=extractors))
    # Researches in list order; builds missing tech buildings (e.g. evolution chamber, lair)
    plan.add(UpgradeController(upgrade_list=upgrades, base_location=ai.start_location))
    return plan
