"""Import paths for the scripts, tests and scenario workers: our bot, then ares (`ares-sc2/src`) and its
`sc2_helper` (`ares-sc2`), as run.py sets them up for the ladder (which keeps its own copy: the zip
has no scripts/).

Importing this module adds them. Whoever imports it first needs the repository root on `sys.path`:

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import scripts.bootstrap  # noqa: F401
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORT_PATHS = [ROOT, ROOT / "ares-sc2" / "src", ROOT / "ares-sc2"]


def add_import_paths() -> None:
    """Put IMPORT_PATHS first on `sys.path`, in order (once each)."""
    paths = [str(p) for p in IMPORT_PATHS]
    sys.path[:] = paths + [p for p in sys.path if p not in paths]


add_import_paths()
