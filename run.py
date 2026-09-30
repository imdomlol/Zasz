import argparse
import sys
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
    parser.add_argument("--replay", default="replays/last_game.SC2Replay", help="Where to save the replay")
    args = parser.parse_args()

    Path(args.replay).parent.mkdir(parents=True, exist_ok=True)
    run_game(
        maps.get(args.map),
        [bot, Computer(Race[args.race], Difficulty[args.difficulty])],
        realtime=args.realtime,
        save_replay_as=args.replay,
    )


if __name__ == "__main__":
    main()
