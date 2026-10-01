"""Offline tests (no StarCraft II needed): `uv run pytest`.

Same import paths as run.py: our bot, then ares (`ares-sc2/src`) and its `sc2_helper` (`ares-sc2`).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "ares-sc2" / "src"), str(ROOT / "ares-sc2")]
