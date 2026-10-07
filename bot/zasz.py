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
from bot.actions.macro import macro_plan
from bot.actions.pathing import ExpansionAccess
from bot.actions.queens import build_queens, control_queens
from bot.actions.scouting import Scouting
from bot.actions.upgrades import remaining_gas, upgrade_plan, workers_per_gas
from bot.combat import ARMY_TYPES
from bot.decisions import Decision, Decisions, ScriptedDecisions
from bot.efficiency import DamageTracker
from bot.observation import Observation, observe
from bot.recording import DecisionRecorder
from bot.settings import MAX_DRONES


class ZaszBot(AresBot):
    """Zasz: a Zerg bot built on ares-sc2 (https://aressc2.github.io/ares-sc2/).

    This class only wires things together each step: `observe` what we can see, `decisions`
    choose what to do, `actions` and `army` carry it out. Remember to call `super()` in every hook.
    """

    NAME = "Zasz"

    def __init__(
        self,
        decisions: Decisions | None = None,
        recorder: DecisionRecorder | None = None,
        game_step_override: int | None = None,
    ):
        super().__init__(game_step_override)
        self.decisions: Decisions = decisions or ScriptedDecisions()
        self.recorder = recorder  # logs (observation, decision) pairs, e.g. for training a model
        self.observation = Observation()  # this step's, for logging and tests
        self.decision = Decision()
        # How many drones each extractor should have; follows the upgrade wish list
        self.workers_per_gas: int = 3
        self.army = army.Army()
        self.scouting = Scouting()
        self.expansions = ExpansionAccess()  # which expansions we can walk to, and why not
        self.damage = DamageTracker(ARMY_TYPES)  # how much of the army's damage lands

    async def on_start(self) -> None:
        await super().on_start()
        logger.info(f"{self.NAME} starting on {self.game_info.map_name}")

    async def on_step(self, iteration: int) -> None:
        await super().on_step(iteration)
        self.damage.update(self)

        if iteration == 0:
            await self.chat_send("(glhf)")

        if not self.townhalls:
            army.all_in(self)
            return

        self.observation = observe(self)
        self.decision = decision = self.decisions.decide(self.observation)
        if self.recorder:
            self.recorder.record(self.state.game_loop, self.observation, decision)

        upgrades = list(decision.upgrades)
        gas = remaining_gas(self, upgrades)
        self.workers_per_gas = workers_per_gas(self, gas)

        # Speed mining, worker distribution, gas and fleeing from danger
        self.register_behavior(Mining(workers_per_gas=self.workers_per_gas))

        build_queens(self, decision.queens)
        await build_spores(self, decision.spores_per_base)
        # Behaviors run in registration order, so upgrades get resources before the macro plan
        self.register_behavior(upgrade_plan(self, upgrades, gas))
        self.register_behavior(macro_plan(self, decision, self.expansions))

        control_queens(self)
        self.scouting.control(self)
        self.army.control(self, attacking=decision.attack)

    async def on_unit_created(self, unit: Unit) -> None:
        await super().on_unit_created(unit)
        if unit.type_id in ARMY_TYPES:
            self.mediator.assign_role(tag=unit.tag, role=UnitRole.ATTACKING)

    async def on_unit_took_damage(self, unit: Unit, amount_damage_taken: float) -> None:
        await super().on_unit_took_damage(unit, amount_damage_taken)
        self.army.on_damage(self, unit, amount_damage_taken)
        self.scouting.on_damage(self, unit)
        self.damage.on_damage_taken(unit, amount_damage_taken)

    async def on_unit_destroyed(self, unit_tag: int) -> None:
        await super().on_unit_destroyed(unit_tag)
        self.damage.on_unit_destroyed(unit_tag)
        self.army.on_death(self, unit_tag)

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
        logger.info(f"Army damage: {self.damage.total.summary()}")
        if self.recorder:
            self.recorder.close(game_result)
