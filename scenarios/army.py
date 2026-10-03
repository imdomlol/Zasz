"""Army control (`bot/army.py`): target priority and when to fight."""

from sc2.ids.unit_typeid import UnitTypeId

from bot.army import TARGET_NOTES
from bot.targeting import attack_capacity
from scenarios import Outcome, Scenario, ScenarioBot, failed, passed

TANKS = {UnitTypeId.SIEGETANK, UnitTypeId.SIEGETANKSIEGED}
GAP_SECONDS = 1.0  # tank_dive: how long one open slot on the tank may last while zerglings are on marines


class TankDive(Scenario):
    """Zerglings dive a sieged tank behind marines: the tank gets attackers first, as many as fit
    around it, and only zerglings that don't fit take the marines. Judged by the targeting itself
    (each zergling's attack order every step), not by what dies first: with more zerglings than fit
    around the tank, the overflow can kill the marines before the tank falls, which is fine."""

    name = "tank_dive"
    description = "Zerglings dive a sieged tank behind marines first (DIVE_FIRST), overflow takes the marines"
    time_limit = 40
    reinforcements = False  # only the units it spawns fight

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
        self.gap_since: float | None = None  # since when a zergling is on a marine while the tank has room
        self.first_on_tank: float | None = None
        self.first_on_marine: float | None = None
        self.most_on_tank = 0
        self.capacity = 0

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
        lings = bot.units(UnitTypeId.ZERGLING)
        if not tanks:
            return passed(
                f"tank dead at {bot.time:.0f}s; tank first targeted at {self.first_on_tank or 0:.1f}s, "
                f"a marine at {self.first_on_marine or 0:.1f}s; most on the tank {self.most_on_tank}/{self.capacity}"
            )
        if not lings:
            return failed("all zerglings died, tank still alive")
        tank = tanks.first
        # The bot's own capacity for the tank (its slots, capped by the open cells around it), else the
        # plain ring size
        noted = TARGET_NOTES.get(tank.tag)
        self.capacity = (noted[1] if noted and noted[1] else None) or attack_capacity(lings.first, tank) or len(lings)
        marines = {m.tag for m in bot.enemy_units(UnitTypeId.MARINE)}
        on_tank = sum(1 for z in lings if z.order_target == tank.tag)
        on_marines = sum(1 for z in lings if z.order_target in marines)
        self.most_on_tank = max(self.most_on_tank, on_tank)
        if on_tank and self.first_on_tank is None:
            self.first_on_tank = bot.time
        if on_marines and self.first_on_marine is None:
            self.first_on_marine = bot.time
        # A zergling on a marine while the tank still has room is not diving. One open slot is fine for up
        # to GAP_SECONDS (a zergling on the tank died; its replacement has to be picked and walk round);
        # two or more open slots, or a gap that lasts, is a failure
        open_slots = self.capacity - on_tank
        if on_marines and open_slots > 0:
            self.gap_since = self.gap_since if self.gap_since is not None else bot.time
            if open_slots >= 2 and bot.time - self.gap_since > 0.5 or bot.time - self.gap_since > GAP_SECONDS:
                return failed(
                    f"{on_marines} zerglings on marines at {bot.time:.1f}s while the tank had room "
                    f"({on_tank}/{self.capacity} on it, for {bot.time - self.gap_since:.1f}s)"
                )
        else:
            self.gap_since = None
        return None

    def on_timeout(self, bot: ScenarioBot) -> Outcome:
        return failed(
            f"tank still alive after {self.time_limit:.0f}s: {bot.units(UnitTypeId.ZERGLING).amount} zerglings, "
            f"most on the tank {self.most_on_tank}/{self.capacity}"
        )


class RetreatFromLosingFight(Scenario):
    reinforcements = False  # only the units it spawns fight
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
    reinforcements = False  # only the units it spawns fight
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


class AlternateTarget(Scenario):
    """No map reveal, like a real game: the zerglings head for the enemy, run into an army they
    can't beat at the natural, and have to find (and kill) an unprotected third on their own instead
    of hovering in front of the natural."""

    reinforcements = False  # only the units it spawns fight

    name = "alternate_target"
    description = "Fog: an unbeatable army at the enemy natural; zerglings find and kill an unprotected base"
    time_limit = 180

    def __init__(self) -> None:
        self.third = None
        self.lings_at_start = 24
        self.third_seen = False

    async def setup(self, bot: ScenarioBot) -> None:
        await bot.client.debug_control_enemy()
        natural = bot.mediator.get_enemy_nat
        # Their closest expansion well away from the natural (out of the army's reach); on some maps
        # the third sits right next to the natural, inside siege tank range
        self.third = next(
            (spot for spot, _ in bot.mediator.get_enemy_expansions[2:] if spot.distance_to(natural) > 30),
            bot.mediator.get_enemy_expansions[2][0],
        )
        # An army 24 zerglings can't beat, at their natural, between us and their main
        toward_us = natural.towards(bot.start_location, 1) - natural
        await bot.spawn(UnitTypeId.SIEGETANK, natural + toward_us * 3, amount=2, enemy=True)
        await bot.spawn(UnitTypeId.MARINE, natural + toward_us * 6, amount=16, enemy=True)
        # ...and a third base with nothing defending it
        await bot.spawn(UnitTypeId.COMMANDCENTER, self.third, enemy=True)
        ours = bot.mediator.get_own_nat
        await bot.spawn(UnitTypeId.ZERGLING, ours.towards(bot.game_info.map_center, 8), amount=self.lings_at_start)

    async def on_step(self, bot: ScenarioBot) -> None:
        # Keep the army in place once we can see it (we can only command what we see)
        bot.siege_enemy_tanks()
        for marine in bot.enemy_units(UnitTypeId.MARINE):
            marine.hold_position()

    def third_cc(self, bot: ScenarioBot):
        return bot.enemy_structures(UnitTypeId.COMMANDCENTER).closer_than(3, self.third)

    def check(self, bot: ScenarioBot) -> Outcome | None:
        if bot.time < 3:
            return None
        lings = bot.units(UnitTypeId.ZERGLING).amount
        if lings < self.lings_at_start // 2:
            return failed(f"lost {self.lings_at_start - lings} of {self.lings_at_start} zerglings (fed into the army?)")
        self.third_seen |= bool(self.third_cc(bot))
        # Killed = we found it, and now we can see the spot and it's gone
        if self.third_seen and bot.is_visible(self.third) and not self.third_cc(bot):
            return passed(f"base found and killed at {bot.time:.0f}s with {lings} zerglings left")
        return None

    def on_timeout(self, bot: ScenarioBot) -> Outcome:
        state = "found but alive" if self.third_seen else "never found"
        return failed(
            f"third {state} after {self.time_limit:.0f}s, {bot.units(UnitTypeId.ZERGLING).amount} zerglings left"
        )


SCENARIOS = [TankDive(), RetreatFromLosingFight(), FightWinnable(), AlternateTarget()]
