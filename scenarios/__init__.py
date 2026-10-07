"""Staged scenarios: start a game, set up a situation with debug commands, check what the bot does.

Each scenario runs the real `ZaszBot` against the built-in AI (VeryEasy, so it stays out of the way)
and is a small class:

- `map`, `enemy_race`, `time_limit` (game seconds)
- `setup(bot)`: called once on the first step; spawn units, reveal the map, ...
- `on_step(bot)`: optional upkeep every step (e.g. keep an enemy tank sieged)
- `check(bot)`: called every step; return `passed()` / `failed(...)` to finish, or None to keep going
- `on_timeout(bot)`: what running out of time means (a failure unless overridden)
- `decisions()` / `recorder()`: optionally run the bot with other decisions, or record the game
- `teardown(bot)`: called once the outcome is known (e.g. to remove temporary files)

The registry (`ALL`) holds the classes: a run builds only the scenario it plays, so importing the
registry (e.g. `--list`) sets nothing up.

Debug quirks found so far:
- Spawned units appear on the next step.
- Spawning `SIEGETANKSIEGED` gives an unsieged tank; use `siege_enemy_tanks` (needs
  `debug_control_enemy`), and keep calling it, the built-in AI unsieges them.
- Enemy units in the fog are unknown to the bot, as in a real game; `debug_show_map` to reveal.
- The built-in AI controls spawned enemy units: it unsieges tanks and can lift off barracks (use
  engineering bays for walls). Enemy supply depots failed to spawn on our high ground.
- A spawn that doesn't fit is moved to the nearest free spot (or dropped), so leave margin.
- Each run uses a fixed game seed per side (`scenarios/seeds.py`), so it plays out the same way
  every time; `--side both` covers both sides, `--random` gives random starts.

Run them with `scripts/run_scenarios.py`.
"""

import os
import time
from collections import Counter
from dataclasses import dataclass

from ares.dicts.cost_dict import COST_DICT
from sc2.data import Race
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bot import ZaszBot
from bot.army import ARMY_TYPES
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
    # False: no new units hatch during the scenario (larva and eggs are removed every step), so a
    # fight is decided by the units it spawned, not by reinforcements walking in from the main
    reinforcements: bool = True

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

    def teardown(self, bot: "ScenarioBot") -> None: ...


class ScenarioBot(ZaszBot):
    """ZaszBot with a scenario attached: sets it up, checks it every step, leaves when decided."""

    def __init__(self, scenario: Scenario):
        super().__init__(decisions=scenario.decisions(), recorder=scenario.recorder())
        self.scenario = scenario
        self.outcome: Outcome | None = None
        self.lost: Counter[UnitTypeId] = Counter()  # our army units that died, by type

    async def on_unit_destroyed(self, unit_tag: int) -> None:
        await super().on_unit_destroyed(unit_tag)
        unit = self._units_previous_map.get(unit_tag)
        if unit is not None and (unit.type_id in ARMY_TYPES or unit.type_id == UnitTypeId.QUEEN):
            self.lost[unit.type_id] += 1

    def stats(self) -> dict:
        """What the fight cost us (and what it did), for comparing runs beyond pass/fail."""
        score = self.state.score if getattr(self, "state", None) else None

        def value(unit_type: UnitTypeId) -> int:
            cost = COST_DICT.get(unit_type)
            if cost is None:
                return 0
            total = cost.minerals + cost.vespene
            return total // 2 if unit_type == UnitTypeId.ZERGLING else total  # the table prices a pair

        return {
            "units_lost": sum(self.lost.values()),
            "army_lost": sum(value(t) * n for t, n in self.lost.items()),
            "damage_taken": round(score.total_damage_taken_life + score.total_damage_taken_shields) if score else 0,
            "damage_dealt": round(score.total_damage_dealt_life + score.total_damage_dealt_shields) if score else 0,
        }

    async def on_step(self, iteration: int) -> None:
        if self.outcome:
            return
        await super().on_step(iteration)
        self.slow_down()
        if iteration == 0:
            self.army.debug = True  # log what the squads decide (scenario logs are only shown on request)
            await self.scenario.setup(self)
            return
        await self.scenario.on_step(self)
        hatching = [u.tag for u in self.units.of_type({UnitTypeId.LARVA, UnitTypeId.EGG})]
        if hatching and not self.scenario.reinforcements:
            await self.client.debug_kill_unit(hatching)
        if self.time >= self.scenario.time_limit:
            self.outcome = self.scenario.on_timeout(self)
        else:
            self.outcome = self.scenario.check(self)
        if self.outcome:
            self.scenario.teardown(self)
            await self.client.leave()

    # --- helpers for setting up scenarios ---

    @property
    def enemy_id(self) -> int:
        return 3 - self.player_id

    async def spawn(self, unit_type: UnitTypeId, position: Point2, amount: int = 1, enemy: bool = False) -> None:
        owner = self.enemy_id if enemy else self.player_id
        await self.client.debug_create_unit([[unit_type, amount, position, owner]])

    def slow_down(self) -> None:
        """`--slow X` (ZASZ_SLOW): play X times slower than real time, to watch. The game waits for the
        bot every step in normal (not realtime) runs, so pausing here changes nothing it decides."""
        factor = float(os.environ.get("ZASZ_SLOW", "0") or 0)
        if factor <= 0:
            return
        step_seconds = self.client.game_step / 22.4  # game loops per step, at faster speed
        now = time.perf_counter()
        wait = step_seconds * factor - (now - getattr(self, "_last_step", now))
        if wait > 0:
            time.sleep(wait)
        self._last_step = time.perf_counter()

    def siege_enemy_tanks(self) -> None:
        for tank in self.enemy_units(UnitTypeId.SIEGETANK):
            tank(AbilityId.SIEGEMODE_SIEGEMODE)


from scenarios import (  # noqa: E402 (they import the classes above)
    army,
    decisions,
    defense,
    efficiency,
    expansions,
    walls,
)

ALL: list[type[Scenario]] = [
    *expansions.SCENARIOS,
    *defense.SCENARIOS,
    *army.SCENARIOS,
    *decisions.SCENARIOS,
    *efficiency.SCENARIOS,
    *walls.SCENARIOS,
]
