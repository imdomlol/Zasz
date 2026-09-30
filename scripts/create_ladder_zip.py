"""Build Zasz.zip for AI Arena (Linux, Python 3.12).

AI Arena preinstalls python-sc2's dependencies (numpy, scipy, aiohttp, loguru, ...) but not
ares-sc2, so we bundle: our bot, ares (+ its sc2_helper), the ares fork of python-sc2,
map_analyzer and cython_extensions. cython_extensions is compiled, so its Linux wheel is downloaded from PyPI;
this lets the zip be built on any OS.

Usage (from the Zasz-ML folder):  .venv/Scripts/python scripts/create_ladder_zip.py
"""

import importlib.metadata
import io
import json
import urllib.request
import zipfile
from pathlib import Path

import map_analyzer
import sc2
import yaml

ROOT = Path(__file__).resolve().parent.parent
TARGET_TAG = "cp312-cp312-manylinux"  # AI Arena runs Linux x86_64, Python 3.12

TOP_LEVEL_FILES = ["run.py", "ladder.py", "config.yml", "ladderbots.json"]
SKIP_DIRS = {"__pycache__", "pickle_gameinfo"}
# Windows/macOS binaries and build sources aren't needed on the ladder
SKIP_SUFFIXES = (".pyd", ".c", ".pyx", ".pxd")


def add_tree(zf: zipfile.ZipFile, src: Path, arc_root: str) -> None:
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_dir() or SKIP_DIRS.intersection(rel.parts):
            continue
        if path.name.endswith(SKIP_SUFFIXES) or "darwin" in path.name:
            continue
        zf.write(path, f"{arc_root}/{rel.as_posix()}")


def add_linux_cython_extensions(zf: zipfile.ZipFile) -> None:
    version = importlib.metadata.version("cython-extensions-sc2")
    with urllib.request.urlopen(f"https://pypi.org/pypi/cython-extensions-sc2/{version}/json") as resp:
        files = json.load(resp)["urls"]
    wheel = next(f for f in files if TARGET_TAG in f["filename"])
    print(f"Downloading {wheel['filename']}")
    with urllib.request.urlopen(wheel["url"]) as resp:
        wheel_zip = zipfile.ZipFile(io.BytesIO(resp.read()))
    for name in wheel_zip.namelist():
        if name.startswith("cython_extensions/") and not name.endswith(SKIP_SUFFIXES):
            zf.writestr(name, wheel_zip.read(name))


def main() -> None:
    config = yaml.safe_load((ROOT / "config.yml").read_text())
    assert not config["Debug"], "Set `Debug: False` in config.yml before uploading"

    out = ROOT / f"{config['MyBotName']}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in TOP_LEVEL_FILES:
            zf.write(ROOT / name, name)
        add_tree(zf, ROOT / "bot", "bot")
        add_tree(zf, ROOT / "ares-sc2" / "src" / "ares", "ares-sc2/src/ares")
        add_tree(zf, ROOT / "ares-sc2" / "sc2_helper", "ares-sc2/sc2_helper")
        add_tree(zf, Path(sc2.__file__).parent, "sc2")
        add_tree(zf, Path(map_analyzer.__file__).parent, "map_analyzer")
        add_linux_cython_extensions(zf)

    print(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
