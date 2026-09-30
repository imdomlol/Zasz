from ares import AresBot
from ares.behaviors.macro import Mining
from ares.consts import UnitRole
from loguru import logger
from sc2.data import Result
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.unit import Unit

from bot import army
from bot.actions.defense import build_spores
from bot.actions.drones import drone_target
from bot.actions.queens import build_queens, control_queens
from bot.actions.upgrades import upgrade_plan, workers_per_gas
from bot.decisions import Decisions, ScriptedDecisions
from bot.settings import MAX_DRONES


class ZaszBot(AresBot):
    """Zasz: a Zerg bot built on ares-sc2 (https://aressc2.github.io/ares-sc2/).

    This class only wires things together each step: `decisions` chooses what to do,
    `actions` and `army` carry it out. Remember to call `super()` in every hook.
    """

    NAME = "Zasz"

    def __init__(self, decisions: Decisions | None = None, game_step_override: int | None = None):
        super().__init__(game_step_override)
        self.decisions: Decisions = decisions or ScriptedDecisions()
        # How many drones each extractor should have; follows the upgrade wish list
        self.workers_per_gas: int = 3

    async def on_start(self) -> None:
        await super().on_start()
        logger.info(f"{self.NAME} starting on {self.game_info.map_name}")

    async def on_step(self, iteration: int) -> None:
        await super().on_step(iteration)

        if iteration == 0:
            await self.chat_send("(glhf)")

        if not self.townhalls:
            army.all_in(self)
            return

        upgrades = self.decisions.upgrades(self)
        self.workers_per_gas = workers_per_gas(self, upgrades)

        # Speed mining, worker distribution, gas and fleeing from danger
        self.register_behavior(Mining(workers_per_gas=self.workers_per_gas))

        build_queens(self)
        await build_spores(self)
        # Behaviors run in registration order, so upgrades get resources before the macro plan
        self.register_behavior(upgrade_plan(self, upgrades))
        self.register_behavior(self.decisions.macro_plan(self))

        control_queens(self)
        army.control(self, attacking=self.decisions.should_attack(self))

    async def on_unit_created(self, unit: Unit) -> None:
        await super().on_unit_created(unit)
        if unit.type_id in army.ARMY_TYPES:
            self.mediator.assign_role(tag=unit.tag, role=UnitRole.ATTACKING)

    async def on_building_construction_started(self, unit: Unit) -> None:
        await super().on_building_construction_started(unit)
        if unit.type_id == UnitTypeId.HATCHERY:
            logger.info(
                f"{self.time_formatted} expanding: base #{self.townhalls.amount}, "
                f"drones {self.supply_workers}/{drone_target(self)} (max {MAX_DRONES})"
            )

    async def on_upgrade_complete(self, upgrade: UpgradeId) -> None:
        await super().on_upgrade_complete(upgrade)
        logger.info(f"{self.time_formatted} upgrade complete: {upgrade.name}")

    async def on_end(self, game_result: Result) -> None:
        await super().on_end(game_result)
        logger.info(f"{self.NAME} game ended: {game_result}")
