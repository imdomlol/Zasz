"""Carry out a decision's spending: supply, the one thing we're saving for, drones and army.

The decision says *what* (`Decision.build`, `drone_target`, `army`); this decides *when* and
*how*: just-in-time builder drones, which base to take, and ares' spending priorities.
"""

from ares import AresBot
from ares.behaviors.macro import AutoSupply, BuildStructure, MacroPlan, SpawnController
from sc2.ids.unit_typeid import UnitTypeId

from bot.actions.building import ExpandTo, affordable_on_arrival, next_expansion_location
from bot.actions.drones import BuildDrones
from bot.actions.pathing import ExpansionAccess
from bot.decisions import Build, Decision


def macro_plan(ai: AresBot, decision: Decision, expansions: ExpansionAccess) -> MacroPlan:
    """Spending priorities, highest first. Each step stops later steps from spending when it
    wants the money (e.g. AutoSupply saves up for an overlord)."""
    plan = MacroPlan()
    plan.add(AutoSupply(base_location=ai.start_location))

    # Save minerals for key buildings; only send the drone once it will arrive just as we can
    # afford it, instead of idling at the build spot
    if decision.build == Build.POOL:
        pool_spot = ai.start_location.towards(ai.game_info.map_center, 6)
        if affordable_on_arrival(ai, UnitTypeId.SPAWNINGPOOL, pool_spot):
            plan.add(BuildStructure(base_location=ai.start_location, structure_id=UnitTypeId.SPAWNINGPOOL))
        return plan

    if decision.build == Build.EXPAND and (location := next_expansion_location(ai, expansions)):
        if affordable_on_arrival(ai, UnitTypeId.HATCHERY, location):
            plan.add(ExpandTo(to_count=ai.townhalls.ready.amount + 1, can_afford_check=False, location=location))
        return plan

    plan.add(BuildDrones(to_count=decision.drone_target))
    if composition := spawn_composition(decision):
        plan.add(SpawnController(composition))
    return plan


def spawn_composition(decision: Decision) -> dict:
    """The decision's army proportions in ares' `SpawnController` format (listed order = priority)."""
    return {
        unit: {"proportion": share, "priority": priority}
        for priority, (unit, share) in enumerate(decision.army.items())
        if share > 0
    }
