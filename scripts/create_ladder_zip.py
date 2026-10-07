"""Build Zasz.zip for AI Arena (Linux x86_64, Python 3.12).

AI Arena preinstalls python-sc2's dependencies (numpy, scipy, aiohttp, loguru, ...) but not
ares-sc2, so we bundle: our bot, ares (+ its sc2_helper), the ares fork of python-sc2,
map_analyzer and cython_extensions. cython_extensions is compiled, so its Linux wheel is downloaded from PyPI
(checked against PyPI's hash and cached in `.cache/`); this lets the zip be built on any OS.

Only the ladder's binaries go in: Linux, and of the per-version ones only Python 3.12's. The zip is
written next to the old one and swapped in at the end, so a failed build leaves the old zip as it was.

Usage (from the repository root):  uv run python scripts/create_ladder_zip.py [--out PATH]
"""

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import re
import urllib.request
import zipfile
from pathlib import Path

import map_analyzer
import sc2
import yaml

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
PYTHON_TAG = "cp312"  # AI Arena runs Linux x86_64, Python 3.12
PLATFORM = re.compile(r"manylinux[\w.]*_x86_64")
TIMEOUT_S = 60

TOP_LEVEL_FILES = ["run.py", "ladder.py", "config.yml", "ladderbots.json"]
SKIP_DIRS = {"__pycache__", "pickle_gameinfo"}
# Windows/macOS binaries and build sources aren't needed on the ladder
SKIP_SUFFIXES = (".pyd", ".c", ".pyx", ".pxd")
SKIP_FILES = {"ares-sc2/sc2_helper/test.py"}
# A compiled module built for one Python version, e.g. `sc2_helper.cpython-311-x86_64-linux-gnu.so`
VERSIONED_SO = re.compile(r"\.cpython-(\d+)-")


def wanted(arc_name: str) -> bool:
    """Whether a file (by its path in the zip) is needed on the ladder."""
    name = arc_name.rsplit("/", 1)[-1]
    if arc_name in SKIP_FILES or name.endswith(SKIP_SUFFIXES) or "darwin" in name:
        return False
    version = VERSIONED_SO.search(name)
    return version is None or f"cp{version.group(1)}" == PYTHON_TAG  # untagged/abi3 ones load on any version


def add_tree(zf: zipfile.ZipFile, src: Path, arc_root: str) -> None:
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        arc_name = f"{arc_root}/{rel.as_posix()}"
        if path.is_dir() or SKIP_DIRS.intersection(rel.parts) or not wanted(arc_name):
            continue
        zf.write(path, arc_name)


def pick_wheel(files: list[dict]) -> dict:
    """The CPython 3.12, Linux x86_64 wheel among a PyPI release's files."""
    for f in files:
        parts = f["filename"].removesuffix(".whl").split("-")
        if len(parts) >= 5 and parts[-3] == PYTHON_TAG and parts[-2] == PYTHON_TAG and PLATFORM.search(parts[-1]):
            return f
    names = ", ".join(f["filename"] for f in files) or "none"
    raise SystemExit(f"no {PYTHON_TAG} manylinux x86_64 wheel in this release (files: {names})")


def linux_wheel(package: str) -> bytes:
    """The package's Linux wheel for the installed version, from the cache or PyPI."""
    version = importlib.metadata.version(package)
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:
        wheel = pick_wheel(json.load(resp)["urls"])
    cached = CACHE / wheel["filename"]
    sha256 = wheel["digests"]["sha256"]
    if cached.exists() and hashlib.sha256(cached.read_bytes()).hexdigest() == sha256:
        print(f"Using cached {wheel['filename']}")
        return cached.read_bytes()
    print(f"Downloading {wheel['filename']}")
    with urllib.request.urlopen(wheel["url"], timeout=TIMEOUT_S) as resp:
        data = resp.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise SystemExit(f"{wheel['filename']}: download doesn't match PyPI's sha256")
    CACHE.mkdir(exist_ok=True)
    cached.write_bytes(data)
    return data


def add_wheel(zf: zipfile.ZipFile, wheel: bytes, package_dir: str) -> None:
    wheel_zip = zipfile.ZipFile(io.BytesIO(wheel))
    for name in wheel_zip.namelist():
        if name.startswith(f"{package_dir}/") and wanted(name):
            zf.writestr(name, wheel_zip.read(name))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, help="Where to write the zip (default: <MyBotName>.zip here)")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config.yml").read_text())
    if config["Debug"]:
        raise SystemExit("Set `Debug: False` in config.yml before uploading")

    # The network first: if it fails, nothing has been written
    cython_extensions = linux_wheel("cython-extensions-sc2")

    out = args.out or ROOT / f"{config['MyBotName']}.zip"
    temp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in TOP_LEVEL_FILES:
            zf.write(ROOT / name, name)
        add_tree(zf, ROOT / "bot", "bot")
        add_tree(zf, ROOT / "ares-sc2" / "src" / "ares", "ares-sc2/src/ares")
        add_tree(zf, ROOT / "ares-sc2" / "sc2_helper", "ares-sc2/sc2_helper")
        add_tree(zf, Path(sc2.__file__).parent, "sc2")
        add_tree(zf, Path(map_analyzer.__file__).parent, "map_analyzer")
        add_wheel(zf, cython_extensions, "cython_extensions")
    os.replace(temp, out)

    print(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
