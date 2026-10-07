"""Expansion reachability (`bot/actions/pathing.py`): bases we can't walk to are skipped, with the reason."""

from sc2.ids.unit_typeid import UnitTypeId

from bot.actions.building import next_expansion_location
from scenarios import Outcome, Scenario, ScenarioBot, failed, passed


class ExpansionBaseline(Scenario):
    name = "expansion_baseline"
    description = "Nothing in the way: the next expansion is the closest base by path"
    time_limit = 5

    def check(self, bot: ScenarioBot) -> Outcome | None:
        location = next_expansion_location(bot, bot.expansions)
        closest = bot.mediator.get_own_expansions[0][0]
        if location != closest:
            return failed(f"next expansion {location} isn't the closest base {closest.rounded}")
        return passed(f"natural at {location.rounded}")


class RampBlocked(Scenario):
    """A block of engineering bays over the bottom of our main ramp cuts the main off from every base.

    Engineering bays because they can't lift off. A 5x5 block, because debug spawns that don't fit
    get pushed to the nearest free spot, which left gaps in a tighter wall.
    """

    kind = ""
    enemy_wall = False
    time_limit = 10

    async def setup(self, bot: ScenarioBot) -> None:
        if self.enemy_wall:
            await bot.client.debug_show_map()  # we only know about enemy walls we've seen
        bottom = bot.main_base_ramp.bottom_center
        for dx in (-6, -3, 0, 3, 6):
            for dy in (-6, -3, 0, 3, 6):
                await bot.spawn(UnitTypeId.ENGINEERINGBAY, bottom.offset((dx, dy)), enemy=self.enemy_wall)

    def check(self, bot: ScenarioBot) -> Outcome | None:
        if bot.time < 3:  # let the spawns land and ares refresh its grid
            return None
        location = next_expansion_location(bot, bot.expansions)
        if location is not None:
            return failed(f"picked {location.rounded} although the main is walled in")
        blocked = bot.expansions.blocked
        if not blocked:
            return failed("no expansion recorded as blocked")
        kinds = {b.kind for b in blocked.values()}
        if kinds != {self.kind}:
            return failed(f"blocked as {kinds}, expected {self.kind!r}")
        return passed(f"{len(blocked)} bases skipped as {self.kind}")


class ExpansionHostile(RampBlocked):
    name = "expansion_hostile"
    description = "Enemy structures wall our main in: every base is skipped as 'hostile'"
    kind = "hostile"
    enemy_wall = True


class ExpansionWalled(RampBlocked):
    name = "expansion_walled"
    description = "Our own structures wall our main in: every base is skipped as 'walled'"
    kind = "walled"


class ExpansionRocks(Scenario):
    name = "expansion_rocks"
    description = "Torches: the bases behind an expedition gate are blocked by 'rocks', listing the gate"
    map = "TorchesAIE_v4"
    time_limit = 5

    def check(self, bot: ScenarioBot) -> Outcome | None:
        known = {location for location, _ in bot.mediator.get_own_expansions}
        behind_gates = [
            location
            for location in bot.expansion_locations_list
            if location not in known and location.distance_to(bot.start_location) > 5
        ]
        if not behind_gates:
            return failed("ares can reach every base; expected the gated ones to be missing")
        location = min(behind_gates, key=lambda p: p.distance_to(bot.start_location))
        access = bot.expansions
        if access.is_reachable(bot, location):
            return failed(f"{location.rounded} reported reachable")
        blockage = access.blocked[location]
        rocks = {r.type_id for r in blockage.rocks or []}
        if blockage.kind != "rocks" or UnitTypeId.DESTRUCTIBLEEXPEDITIONGATE6X6 not in rocks:
            return failed(f"blocked as {blockage.kind!r} by {rocks or 'nothing'}, expected an expedition gate")
        return passed(f"{len(behind_gates)} gated bases; {location.rounded} needs the gate broken")


SCENARIOS = [ExpansionBaseline, ExpansionHostile, ExpansionWalled, ExpansionRocks]
