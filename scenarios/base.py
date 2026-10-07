"""The scenario framework: `Scenario` (what to set up and check), `ScenarioBot` (ZaszBot running one),
`Outcome`. How to write a scenario: see `scenarios/__init__.py`."""

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
        await self.spawn_all([(unit_type, position, amount, enemy)])

    async def spawn_all(self, spawns: list[tuple[UnitTypeId, Point2, int, bool]]) -> None:
        """(unit type, position, amount, enemy) each, in one debug request, created in list order. Only
        for spawns that don't need to wait for each other (e.g. a tech lab needs its building first)."""
        if spawns:
            owners = {False: self.player_id, True: self.enemy_id}
            await self.client.debug_create_unit([[t, n, p, owners[enemy]] for t, p, n, enemy in spawns])

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
