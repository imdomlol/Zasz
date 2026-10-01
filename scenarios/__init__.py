"""Staged scenarios: start a game, set up a situation with debug commands, check what the bot does.

Each scenario runs the real `ZaszBot` against the built-in AI (VeryEasy, so it stays out of the way)
and is a small class:

- `map`, `enemy_race`, `time_limit` (game seconds)
- `setup(bot)`: called once on the first step; spawn units, reveal the map, ...
- `on_step(bot)`: optional upkeep every step (e.g. keep an enemy tank sieged)
- `check(bot)`: called every step; return `passed()` / `failed(...)` to finish, or None to keep going
- `on_timeout(bot)`: what running out of time means (a failure unless overridden)
- `decisions()` / `recorder()`: optionally run the bot with other decisions, or record the game

Debug quirks found so far:
- Spawned units appear on the next step.
- Spawning `SIEGETANKSIEGED` gives an unsieged tank; use `siege_enemy_tanks` (needs
  `debug_control_enemy`), and keep calling it, the built-in AI unsieges them.
- Enemy units in the fog are unknown to the bot, as in a real game; `debug_show_map` to reveal.
- The built-in AI controls spawned enemy units: it unsieges tanks and can lift off barracks (use
  engineering bays for walls). Enemy supply depots failed to spawn on our high ground.
- A spawn that doesn't fit is moved to the nearest free spot (or dropped), so leave margin.
- Start locations are random: run with `--repeat` to cover both sides.

Run them with `scripts/run_scenarios.py`.
"""

from dataclasses import dataclass

from sc2.data import Race
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bot import ZaszBot
from bot.decisions import Decisions
from bot.recording import DecisionRecorder


@dataclass
class Outcome:
    passed: bool
    message: str = ""


def passed(message: str = "") -> Outcome:
    return Outcome(True, message)


def failed(message: str) -> Outcome:
    return Outcome(False, message)


class Scenario:
    name: str = ""
    description: str = ""
    map: str = "PersephoneAIE_v4"
    enemy_race: Race = Race.Terran
    time_limit: float = 60

    async def setup(self, bot: "ScenarioBot") -> None: ...

    async def on_step(self, bot: "ScenarioBot") -> None: ...

    def check(self, bot: "ScenarioBot") -> Outcome | None:
        return None

    def on_timeout(self, bot: "ScenarioBot") -> Outcome:
        return failed(f"nothing decided within {self.time_limit:.0f}s")

    def decisions(self) -> Decisions | None:
        """Decisions to run the bot with (default: the scripted ones)."""
        return None

    def recorder(self) -> DecisionRecorder | None:
        return None


class ScenarioBot(ZaszBot):
    """ZaszBot with a scenario attached: sets it up, checks it every step, leaves when decided."""

    def __init__(self, scenario: Scenario):
        super().__init__(decisions=scenario.decisions(), recorder=scenario.recorder())
        self.scenario = scenario
        self.outcome: Outcome | None = None

    async def on_step(self, iteration: int) -> None:
        if self.outcome:
            return
        await super().on_step(iteration)
        if iteration == 0:
            await self.scenario.setup(self)
            return
        await self.scenario.on_step(self)
        if self.time >= self.scenario.time_limit:
            self.outcome = self.scenario.on_timeout(self)
        else:
            self.outcome = self.scenario.check(self)
        if self.outcome:
            await self.client.leave()

    # --- helpers for setting up scenarios ---

    @property
    def enemy_id(self) -> int:
        return 3 - self.player_id

    async def spawn(self, unit_type: UnitTypeId, position: Point2, amount: int = 1, enemy: bool = False) -> None:
        owner = self.enemy_id if enemy else self.player_id
        await self.client.debug_create_unit([[unit_type, amount, position, owner]])

    def siege_enemy_tanks(self) -> None:
        for tank in self.enemy_units(UnitTypeId.SIEGETANK):
            tank(AbilityId.SIEGEMODE_SIEGEMODE)


from scenarios import army, decisions, defense, expansions  # noqa: E402 (they import the classes above)

ALL: list[Scenario] = [*expansions.SCENARIOS, *defense.SCENARIOS, *army.SCENARIOS, *decisions.SCENARIOS]
