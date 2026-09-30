from ares import AresBot
from ares.behaviors.macro import AutoSupply, BuildStructure, ExpansionController, MacroPlan, SpawnController
from sc2.ids.unit_typeid import UnitTypeId

from bot.actions.building import affordable_on_arrival
from bot.actions.drones import BuildDrones, drone_target
from bot.settings import ARMY_COMPOSITION, ATTACK_AT_ZERGLINGS, EXPAND_TO_BASES, POOL_AT_DRONES


class ScriptedDecisions:
    """Hand-written rules: 16 pool, natural, saturate, mass zerglings, attack at 12."""

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

        if self.expansion_wanted(ai):
            if affordable_on_arrival(ai, UnitTypeId.HATCHERY, ai.mediator.get_own_nat):
                plan.add(ExpansionController(to_count=EXPAND_TO_BASES, can_afford_check=False))
            return plan

        plan.add(BuildDrones(to_count=drone_target(ai)))
        plan.add(SpawnController(ARMY_COMPOSITION))
        return plan

    def should_attack(self, ai: AresBot) -> bool:
        return ai.units(UnitTypeId.ZERGLING).amount >= ATTACK_AT_ZERGLINGS

    # `already_pending` doesn't count a drone that is still walking to the build spot,
    # so these stay True (and keep saving minerals) until the building is placed

    def pool_wanted(self, ai: AresBot) -> bool:
        if ai.structures(UnitTypeId.SPAWNINGPOOL) or ai.already_pending(UnitTypeId.SPAWNINGPOOL):
            return False
        return ai.supply_workers + ai.already_pending(UnitTypeId.DRONE) >= POOL_AT_DRONES

    def expansion_wanted(self, ai: AresBot) -> bool:
        if not ai.structures(UnitTypeId.SPAWNINGPOOL):
            return False
        return ai.townhalls.amount < EXPAND_TO_BASES
