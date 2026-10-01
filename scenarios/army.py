"""Army control (`bot/army.py`): target priority and when to fight."""

from sc2.ids.unit_typeid import UnitTypeId

from scenarios import Outcome, Scenario, ScenarioBot, failed, passed

TANKS = {UnitTypeId.SIEGETANK, UnitTypeId.SIEGETANKSIEGED}


class TankDive(Scenario):
    name = "tank_dive"
    description = "Zerglings run past closer marines to kill a sieged tank first (DIVE_FIRST)"
    time_limit = 40

    async def setup(self, bot: ScenarioBot) -> None:
        await bot.client.debug_show_map()
        await bot.client.debug_control_enemy()
        center = bot.game_info.map_center
        toward_us = center.towards(bot.start_location, 1) - center
        # Marines between us and the tank, so without the dive priority they'd be the closer target
        await bot.spawn(UnitTypeId.SIEGETANK, center, enemy=True)
        await bot.spawn(UnitTypeId.MARINE, center + toward_us * 4, amount=4, enemy=True)
        await bot.spawn(UnitTypeId.ZERGLING, center + toward_us * 11, amount=24)

    def __init__(self) -> None:
        self.was_sieged = False

    async def on_step(self, bot: ScenarioBot) -> None:
        # The built-in AI unsieges the tank and walks the marines off; keep them in place
        bot.siege_enemy_tanks()
        for marine in bot.enemy_units(UnitTypeId.MARINE):
            marine.hold_position()
        self.was_sieged |= bool(bot.enemy_units(UnitTypeId.SIEGETANKSIEGED))

    def check(self, bot: ScenarioBot) -> Outcome | None:
        if bot.time < 2:
            return None
        tanks = bot.enemy_units.of_type(TANKS)
        if not tanks and not self.was_sieged:
            return failed("setup: the tank died before it was ever sieged")
        marines = bot.enemy_units(UnitTypeId.MARINE)
        if not bot.units(UnitTypeId.ZERGLING) and tanks:
            return failed("all zerglings died, tank still alive")
        if not tanks:
            return passed(f"tank dead at {bot.time:.0f}s with {marines.amount} marines still alive")
        if not marines:
            return failed("killed every marine before the tank")
        return None

    def on_timeout(self, bot: ScenarioBot) -> Outcome:
        return failed(
            f"no result in {self.time_limit:.0f}s: {bot.units(UnitTypeId.ZERGLING).amount} zerglings, "
            f"{bot.enemy_units(UnitTypeId.MARINE).amount} marines, tank alive (zerglings never engaged?)"
        )


class RetreatFromLosingFight(Scenario):
    name = "retreat_losing_fight"
    description = "A few zerglings near a big marine/tank force don't throw themselves away"
    time_limit = 25

    async def setup(self, bot: ScenarioBot) -> None:
        await bot.client.debug_show_map()
        await bot.client.debug_control_enemy()
        center = bot.game_info.map_center
        toward_us = center.towards(bot.start_location, 1) - center
        await bot.spawn(UnitTypeId.SIEGETANK, center, amount=2, enemy=True)
        await bot.spawn(UnitTypeId.MARINE, center + toward_us * 2, amount=12, enemy=True)
        await bot.spawn(UnitTypeId.ZERGLING, center + toward_us * 12, amount=6)

    async def on_step(self, bot: ScenarioBot) -> None:
        bot.siege_enemy_tanks()

    def check(self, bot: ScenarioBot) -> Outcome | None:
        if bot.time > 2 and bot.units(UnitTypeId.ZERGLING).amount < 4:
            return failed(f"lost {6 - bot.units(UnitTypeId.ZERGLING).amount} of 6 zerglings")
        return None

    def on_timeout(self, bot: ScenarioBot) -> Outcome:
        return passed(f"{bot.units(UnitTypeId.ZERGLING).amount} of 6 zerglings alive")


class FightWinnable(Scenario):
    name = "fight_winnable"
    description = "The same few zerglings do take a fight they win (pairs with retreat_losing_fight)"
    time_limit = 25

    async def setup(self, bot: ScenarioBot) -> None:
        await bot.client.debug_show_map()
        center = bot.game_info.map_center
        toward_us = center.towards(bot.start_location, 1) - center
        await bot.spawn(UnitTypeId.MARINE, center, amount=2, enemy=True)
        await bot.spawn(UnitTypeId.ZERGLING, center + toward_us * 8, amount=6)

    def check(self, bot: ScenarioBot) -> Outcome | None:
        if bot.time > 2 and not bot.enemy_units(UnitTypeId.MARINE):
            return passed(f"marines dead at {bot.time:.0f}s, {bot.units(UnitTypeId.ZERGLING).amount} zerglings left")
        return None

    def on_timeout(self, bot: ScenarioBot) -> Outcome:
        return failed(f"{bot.enemy_units(UnitTypeId.MARINE).amount} marines still alive")


SCENARIOS = [TankDive(), RetreatFromLosingFight(), FightWinnable()]
