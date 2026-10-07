"""Offline tests (no StarCraft II needed): `uv run pytest`.

Same import paths as run.py (`scripts/bootstrap.py`): our bot, then ares and its `sc2_helper`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.bootstrap  # noqa: E402, F401
