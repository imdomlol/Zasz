"""Static defense (`bot/actions/defense.py`)."""

from sc2.ids.unit_typeid import UnitTypeId

from scenarios.base import Outcome, Scenario, ScenarioBot, failed, passed


class SporesVsAir(Scenario):
    name = "spores_vs_air"
    description = "A starport shows up: a spore crawler goes down in our mineral line"
    time_limit = 40

    async def setup(self, bot: ScenarioBot) -> None:
        await bot.client.debug_minerals()
        await bot.spawn(UnitTypeId.SPAWNINGPOOL, bot.start_location.towards(bot.game_info.map_center, 7))
        # Somewhere our main can see it
        await bot.spawn(UnitTypeId.STARPORT, bot.start_location.towards(bot.game_info.map_center, 12), enemy=True)

    def check(self, bot: ScenarioBot) -> Outcome | None:
        spores = bot.structures(UnitTypeId.SPORECRAWLER)
        if not spores:
            return None
        hatch = bot.townhalls.closest_to(spores.first)
        minerals = bot.mineral_field.closer_than(10, hatch)
        if spores.first.distance_to(minerals.center) > spores.first.distance_to(hatch) + 2:
            return failed(f"spore at {spores.first.position.rounded} isn't on the mineral side of the hatchery")
        return passed(f"spore started at {bot.time:.0f}s")

    def on_timeout(self, bot: ScenarioBot) -> Outcome:
        seen = bot.enemy_structures(UnitTypeId.STARPORT)
        return failed(f"no spore within {self.time_limit:.0f}s (starport seen: {bool(seen)})")


SCENARIOS = [SporesVsAir]
