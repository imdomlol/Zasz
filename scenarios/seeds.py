"""Fixed game seeds per map and side, so scenario runs are repeatable.

StarCraft II picks start locations (and the built-in AI's choices) from the game's random seed. With
a fixed seed the same scenario plays out the same way (or nearly: see `scenarios.REPEATABLE`), so runs
can be compared by their numbers (resources lost, time), not just pass/fail.

A side is named after where the *enemy's* main is (where the efficiency scenarios fight): "top" or
"bottom" on maps whose mains are above each other, "left" or "right" otherwise. `--side top` also
means "left" on such maps (the first side), `--side bottom` "right".

Which seed gives which side depends on the map and the StarCraft II build (a Windows client is a
newer build than the Linux one), so it's looked up once with short probe games and remembered in
`seeds.json`, keyed by build.
"""

import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from scripts.run_meta import sc2_build

SEEDS_FILE = Path(__file__).with_name("seeds.json")
FIRST, SECOND = "first", "second"  # --side top/left, bottom/right
PROBE_BATCH = 4  # probe games at once
MAX_SEED = 40


def side_name(enemy_start, own_start) -> str:
    """ "top"/"bottom" (mains above each other) or "left"/"right", for where the enemy main is."""
    dx, dy = enemy_start[0] - own_start[0], enemy_start[1] - own_start[1]
    if abs(dy) >= abs(dx):
        return "top" if dy > 0 else "bottom"
    return "right" if dx > 0 else "left"


def which(side: str) -> str:
    return FIRST if side in ("top", "left") else SECOND


def load() -> dict:
    try:
        return json.loads(SEEDS_FILE.read_text())
    except FileNotFoundError:
        return {}


def seed_for(map_name: str, side: str) -> tuple[int, str]:
    """(seed, side name) for this map and side ("top"/"bottom"/"left"/"right"); probes if unknown."""
    build = str(sc2_build())
    table = load()
    sides = table.get(build, {}).get(map_name)
    if not sides or which(side) not in sides:
        sides = probe(map_name)
        table.setdefault(build, {})[map_name] = sides
        SEEDS_FILE.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n")
    entry = sides[which(side)]
    return entry["seed"], entry["side"]


def probe(map_name: str) -> dict:
    """Play seeds 1, 2, ... until both sides have one: {first/second: {"seed": n, "side": name}}."""
    print(f"Finding fixed seeds for {map_name} (short probe games)...", flush=True)
    found: dict = {}
    seed = 1
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(PROBE_BATCH, mp_context=context, max_tasks_per_child=1) as pool:
        while len(found) < 2 and seed <= MAX_SEED:
            batch = list(range(seed, seed + PROBE_BATCH))
            seed += PROBE_BATCH
            for s, side in zip(batch, pool.map(_probe_side, [(map_name, s) for s in batch]), strict=True):
                found.setdefault(which(side), {"seed": s, "side": side})
    if len(found) < 2:
        raise RuntimeError(f"seeds 1-{MAX_SEED} on {map_name} all start on the same side")
    return found


def _probe_side(args: tuple[str, int]) -> str:
    """Start a game with this seed, see where the mains are, leave."""
    map_name, seed = args
    from loguru import logger
    from sc2 import maps
    from sc2.bot_ai import BotAI
    from sc2.data import Difficulty, Race
    from sc2.main import run_game
    from sc2.player import Bot, Computer

    import scripts.bootstrap  # noqa: F401 (this worker's import paths, as the parent's)

    logger.remove()

    class Probe(BotAI):
        side = ""

        async def on_step(self, iteration: int) -> None:
            Probe.side = side_name(self.enemy_start_locations[0], self.start_location)
            await self.client.leave()

    run_game(
        maps.get(map_name),
        [Bot(Race.Zerg, Probe()), Computer(Race.Terran, Difficulty.VeryEasy)],
        realtime=False,
        random_seed=seed,
    )
    return Probe.side
