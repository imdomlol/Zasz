from ares import AresBot
from ares.behaviors.macro import Mining
from loguru import logger
from sc2.data import Result

from bot import army
from bot.actions.queens import build_queens, inject_larva
from bot.decisions import Decisions, ScriptedDecisions


class ZaszBot(AresBot):
    """Zasz: a Zerg bot built on ares-sc2 (https://aressc2.github.io/ares-sc2/).

    This class only wires things together each step: `decisions` chooses what to do,
    `actions` and `army` carry it out. Remember to call `super()` in every hook.
    """

    NAME = "Zasz"

    def __init__(self, decisions: Decisions | None = None, game_step_override: int | None = None):
        super().__init__(game_step_override)
        self.decisions: Decisions = decisions or ScriptedDecisions()

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

        # Speed mining, worker distribution, gas and fleeing from danger
        self.register_behavior(Mining())

        build_queens(self)
        self.register_behavior(self.decisions.macro_plan(self))
        inject_larva(self)

        if self.decisions.should_attack(self):
            army.attack(self)
        else:
            army.defend(self)

    async def on_end(self, game_result: Result) -> None:
        await super().on_end(game_result)
        logger.info(f"{self.NAME} game ended: {game_result}")
