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

from scenarios import (
    army,
    decisions,
    defense,
    efficiency,
    expansions,
    walls,
)
from scenarios.base import Outcome, Scenario, ScenarioBot, failed, passed

ALL: list[type[Scenario]] = [
    *expansions.SCENARIOS,
    *defense.SCENARIOS,
    *army.SCENARIOS,
    *decisions.SCENARIOS,
    *efficiency.SCENARIOS,
    *walls.SCENARIOS,
]

# Scenarios that play out command for command the same on the same code and seed (the rest vary a little
# even then, from the game itself): a refactor's order logs must match on these (`scripts/compare_orders.py`)
REPEATABLE = [
    "alternate_target",
    "custom_decisions",
    "efficiency_deep_base",
    "efficiency_open_field",
    "efficiency_ramp",
    "expansion_baseline",
    "expansion_hostile",
    "expansion_rocks",
    "expansion_walled",
    "fight_winnable",
    "retreat_losing_fight",
    "spores_vs_air",
    "wall_techlab",
]

__all__ = ["ALL", "REPEATABLE", "Outcome", "Scenario", "ScenarioBot", "failed", "passed"]
