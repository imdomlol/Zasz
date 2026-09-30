"""Glue for AI Arena / LadderManager games.

Adapted from python-sc2/examples/competitive/__init__.py.
"""

import argparse
import asyncio

import aiohttp
from loguru import logger
from sc2.client import Client
from sc2.main import _play_game
from sc2.portconfig import Portconfig
from sc2.protocol import ConnectionAlreadyClosed


def run_ladder_game(bot):
    parser = argparse.ArgumentParser()
    parser.add_argument("--GamePort", type=int, nargs="?")
    parser.add_argument("--StartPort", type=int, nargs="?")
    parser.add_argument("--LadderServer", type=str, nargs="?")
    parser.add_argument("--OpponentId", type=str, nargs="?")
    parser.add_argument("--RealTime", action="store_true")
    args, _unknown = parser.parse_known_args()

    host = args.LadderServer or "127.0.0.1"
    bot.ai.opponent_id = args.OpponentId

    portconfig = None
    if args.StartPort is not None:
        ports = [args.StartPort + p for p in range(1, 6)]
        portconfig = Portconfig()
        portconfig.server = [ports[1], ports[2]]
        portconfig.players = [[ports[3], ports[4]]]

    result = asyncio.run(join_ladder_game(host, args.GamePort, bot, args.RealTime, portconfig))
    return result, args.OpponentId


async def join_ladder_game(host, port, player, realtime, portconfig):
    async with aiohttp.ClientSession() as session:
        ws_connection = await session.ws_connect(f"ws://{host}:{port}/sc2api", timeout=120)
        client = Client(ws_connection)
        try:
            return await _play_game(player, client, realtime, portconfig, None)
        except ConnectionAlreadyClosed:
            logger.error("Connection was closed before the game ended")
            return None
        finally:
            await ws_connection.close()
