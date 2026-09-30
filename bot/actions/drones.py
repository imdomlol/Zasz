from dataclasses import dataclass

from ares import AresBot
from ares.behaviors.macro.macro_behavior import MacroBehavior
from ares.managers.manager_mediator import ManagerMediator
from sc2.ids.unit_typeid import UnitTypeId

from bot.settings import MAX_DRONES


@dataclass
class BuildDrones(MacroBehavior):
    """Make drones up to exactly `to_count`, counting eggs.

    Replaces ares' `BuildWorkers`, which ignores eggs and can overshoot. ares mines with
    max 2 drones per mineral patch, so extra drones get sent to long distance mine.
    """

    to_count: int

    def execute(self, ai: AresBot, config: dict, mediator: ManagerMediator) -> bool:
        missing = self.to_count - ai.supply_workers - ai.already_pending(UnitTypeId.DRONE)
        amount = min(int(missing), ai.num_larva_left)
        if amount <= 0 or not ai.can_afford(UnitTypeId.DRONE):
            return False
        trained = ai.train(UnitTypeId.DRONE, amount)
        ai.num_larva_left -= trained
        return trained > 0


def drone_target(ai: AresBot) -> int:
    """2 drones per mineral patch plus 3 per extractor, matching how ares' `Mining`
    saturates bases. A hatchery counts once it's nearly done, so drones hatch in time."""
    patches = sum(len(ai.mineral_field.closer_than(10, th)) for th in ai.townhalls if th.build_progress > 0.9)
    extractors = ai.gas_buildings.ready.amount
    return min(2 * patches + 3 * extractors, MAX_DRONES)
