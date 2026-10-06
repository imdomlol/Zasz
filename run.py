import argparse
import os
import sys
import time
from pathlib import Path

# ares-sc2 is cloned next to this file: `ares` lives in `ares-sc2/src`,
# and its combat simulator `sc2_helper` in `ares-sc2`
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "ares-sc2"))
sys.path.insert(0, str(ROOT / "ares-sc2" / "src"))

from sc2 import maps
from sc2.data import Difficulty, Race
from sc2.main import run_game
from sc2.player import Bot, Computer

from bot import ZaszBot

bot = Bot(Race.Zerg, ZaszBot(), name="Zasz")


class PacedZasz(ZaszBot):
    """`--slow X`: ZaszBot playing X times slower than Faster (the ladder's speed) in real time, to watch.
    The game still waits for the bot every step (not realtime), so pacing changes nothing it decides."""

    def __init__(self, factor: float):
        super().__init__()
        self.factor = factor
        self._last_step: float | None = None

    async def on_step(self, iteration: int) -> None:
        await super().on_step(iteration)
        step_seconds = self.client.game_step / 22.4  # game loops per step, at Faster speed
        now = time.perf_counter()
        if self._last_step is not None and (wait := step_seconds * self.factor - (now - self._last_step)) > 0:
            time.sleep(wait)
        self._last_step = time.perf_counter()


def main():
    if "--LadderServer" in sys.argv:
        from ladder import run_ladder_game

        print("Starting ladder game...")
        result, opponent_id = run_ladder_game(bot)
        print(result, "against opponent", opponent_id)
        return

    parser = argparse.ArgumentParser(description="Run Zasz locally against the built-in AI")
    parser.add_argument("--map", default="PersephoneAIE_v4", help="Map name (must be in your StarCraft II/Maps folder)")
    parser.add_argument("--race", default="Random", choices=["Terran", "Zerg", "Protoss", "Random"])
    parser.add_argument("--difficulty", default="Easy", choices=[d.name for d in Difficulty])
    parser.add_argument("--realtime", action="store_true", help="Play at normal game speed")
    parser.add_argument(
        "--slow", type=float, default=0, help="Watch at Faster speed (1) or this many times slower (e.g. 3)"
    )
    parser.add_argument("--overlay", action="store_true", help="Draw what the army is doing (bot/overlay.py)")
    parser.add_argument("--replay", default="replays/last_game.SC2Replay", help="Where to save the replay")
    args = parser.parse_args()

    if args.overlay:
        os.environ["ZASZ_OVERLAY"] = "1"  # read when drawing (bot/overlay.py)
    player = Bot(Race.Zerg, PacedZasz(args.slow), name="Zasz") if args.slow > 0 else bot

    Path(args.replay).parent.mkdir(parents=True, exist_ok=True)
    run_game(
        maps.get(args.map),
        [player, Computer(Race[args.race], Difficulty[args.difficulty])],
        realtime=args.realtime,
        save_replay_as=args.replay,
    )


if __name__ == "__main__":
    main()
