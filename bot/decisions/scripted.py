from ares import AresBot
from ares.behaviors.macro import AutoSupply, BuildStructure, MacroPlan, SpawnController
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

from bot.actions.building import ExpandTo, affordable_on_arrival, next_expansion_location
from bot.actions.drones import BuildDrones, drone_target
from bot.actions.pathing import ExpansionAccess
from bot.settings import (
    ARMY_COMPOSITION,
    ATTACK_AT_ZERGLINGS,
    EXPAND_WHEN_DRONES_WITHIN,
    MAX_DRONES,
    POOL_AT_DRONES,
    SCRIPTED_UPGRADES,
    STOP_ATTACK_AT_ZERGLINGS,
)


class ScriptedDecisions:
    """Hand-written rules: 16 pool, ling speed, expand as drones saturate, mass zerglings, attack at 12."""

    def macro_plan(self, ai: AresBot) -> MacroPlan:
        """Spending priorities, highest first. Each step stops later steps from spending
        when it wants the money (e.g. AutoSupply saves up for an overlord)."""
        plan = MacroPlan()
        plan.add(AutoSupply(base_location=ai.start_location))

        # Save minerals for key buildings; only send the drone once it will arrive
        # just as we can afford it, instead of idling at the build spot
        if self.pool_wanted(ai):
            pool_spot = ai.start_location.towards(ai.game_info.map_center, 6)
            if affordable_on_arrival(ai, UnitTypeId.SPAWNINGPOOL, pool_spot):
                plan.add(BuildStructure(base_location=ai.start_location, structure_id=UnitTypeId.SPAWNINGPOOL))
            return plan

        if self.expansion_wanted(ai) and (location := next_expansion_location(ai, self.expansions)):
            if affordable_on_arrival(ai, UnitTypeId.HATCHERY, location):
                plan.add(ExpandTo(to_count=ai.townhalls.ready.amount + 1, can_afford_check=False, location=location))
            return plan

        plan.add(BuildDrones(to_count=drone_target(ai)))
        plan.add(SpawnController(ARMY_COMPOSITION))
        return plan

    def __init__(self) -> None:
        self.attacking = False
        self.expansions = ExpansionAccess()

    def should_attack(self, ai: AresBot) -> bool:
        """Start at ATTACK_AT_ZERGLINGS, keep going until the army drops below
        STOP_ATTACK_AT_ZERGLINGS. The gap stops the army flip-flopping between attacking and
        pulling back each time a few zerglings die or hatch."""
        lings = ai.units(UnitTypeId.ZERGLING).amount
        if lings >= ATTACK_AT_ZERGLINGS or ai.supply_used >= 190:
            self.attacking = True
        elif lings < STOP_ATTACK_AT_ZERGLINGS:
            self.attacking = False
        return self.attacking

    def upgrades(self, ai: AresBot) -> list[UpgradeId]:
        # Wait for the pool, otherwise ares' UpgradeController would build its own
        if not ai.structures(UnitTypeId.SPAWNINGPOOL):
            return []
        return SCRIPTED_UPGRADES

    # `already_pending` doesn't count a drone that is still walking to the build spot,
    # so these stay True (and keep saving minerals) until the building is placed

    def pool_wanted(self, ai: AresBot) -> bool:
        if ai.structures(UnitTypeId.SPAWNINGPOOL) or ai.already_pending(UnitTypeId.SPAWNINGPOOL):
            return False
        return ai.supply_workers + ai.already_pending(UnitTypeId.DRONE) >= POOL_AT_DRONES

    def expansion_wanted(self, ai: AresBot) -> bool:
        """Expand when our drones are about to fill every base we have.

        `drone_target` only counts mineral patches that still exist, so this also triggers
        when patches mine out and drones have nowhere left to mine.
        """
        # One hatchery at a time; the pool comes first
        if not ai.structures(UnitTypeId.SPAWNINGPOOL) or ai.townhalls.not_ready:
            return False
        target = drone_target(ai)
        if target >= MAX_DRONES:
            return False  # we already have room for as many drones as we'll ever make
        drones = ai.supply_workers + ai.already_pending(UnitTypeId.DRONE)
        return drones >= target - EXPAND_WHEN_DRONES_WITHIN
