# Zasz

A Zerg StarCraft II bot built on [ares-sc2](https://aressc2.github.io/ares-sc2/), which extends
[python-sc2](https://github.com/BurnySc2/python-sc2).

Strategy: make 16 drones and a spawning pool, then keep expanding whenever the drones are about to fill
every base (or patches mine out), up to 70 drones. Research zergling speed, make a queen per hatchery
(injecting larva), pump zerglings, and attack once there are 12. Until then, zerglings defend the base.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and StarCraft II. AI Arena runs bots on Python 3.12, which
`.python-version` pins.

```sh
git clone --recursive <repo-url> Zasz     # --recursive pulls the ares-sc2 submodule
cd Zasz
uv sync                                   # creates .venv with ares-sc2 and dev tools
```

Already cloned without `--recursive`? Run `git submodule update --init`.

Ladder maps go in `<StarCraft II install>/Maps/` (here: `F:\Games\Blizzard\StarCraft II\Maps\`).
Download them from https://aiarena.net/wiki/maps/.

Developing on Linux (headless SC2, faster benchmarks): see [docs/linux-setup.md](docs/linux-setup.md).

## Run

Run from the repo root (ares reads `config.yml` from the working directory):

```sh
uv run python run.py                                   # vs Easy, random race, PersephoneAIE_v4
uv run python run.py --difficulty Hard --race Terran --map TorchesAIE_v4 --realtime
```

Replays are saved to `replays/`.

## Benchmark

Play several games against the built-in AI and get win rates by race and map. Run it before and after
a change to catch regressions:

```sh
uv run python scripts/benchmark.py                      # 6 games vs Medium, all races, all maps
uv run python scripts/benchmark.py --games 12 --difficulty Hard --races Terran Protoss
uv run python scripts/benchmark.py --games 30 --jobs 6  # 6 games at a time (~1 core, ~1.5 GB each)
```

With `--jobs`, games run in parallel, but logs and results are still reported in matchup order, the same
as a sequential run. Bot behavior doesn't depend on wall-clock speed (games aren't realtime), so results
are comparable either way.

Every game is appended to `benchmarks/results.csv` with the git commit, so you can compare versions.

| Commit | Opponent | Result | Notes |
|---|---|---|---|
| `12faa25` | Medium | 4/6 | baseline: one-point attacks, no upgrades |
| `467ba00` | Medium | 6/6 | squads + combat sim, per-unit targeting, upgrades, creep; all wins in 6.5-8.6 min |

## Layout

```
bot/
├── zasz.py            # ZaszBot: wires decisions and actions together each step
├── settings.py        # all tunables (POOL_AT_DRONES, EXPAND_WHEN_DRONES_WITHIN, ...)
├── decisions/         # WHAT to do: build order, when to expand/attack
│   ├── __init__.py    #   `Decisions` interface
│   └── scripted.py    #   hand-written rules (a learned model would sit next to this)
├── actions/           # HOW to do it well: stays scripted
│   ├── drones.py      #   BuildDrones (counts eggs), drone_target
│   ├── building.py    #   just-in-time builder drones
│   └── queens.py      #   one queen per hatchery, injects
└── army.py            # attack / defend / all-in
run.py                 # entry point for local and ladder games
ladder.py              # AI Arena connection glue
config.yml             # ares settings (overrides ares-sc2/src/ares/config.yml)
scripts/
├── benchmark.py       # win-rate benchmark
└── create_ladder_zip.py
ares-sc2/              # git submodule, pinned to a known-good commit
```

From ares, Zasz uses `Mining` (speed mining, worker distribution), `AutoSupply`, `BuildStructure`,
`ExpansionController` and `SpawnController`, ordered by a `MacroPlan` so higher priorities get the
minerals first. Every `ZaszBot` hook must call `super()` or ares stops working.

## Development

```sh
uv run ruff check --fix .    # lint
uv run ruff format .         # format
```

Updating ares: `git -C ares-sc2 pull`, run the benchmark, then commit the new submodule pointer.

## Uploading to AI Arena

```sh
uv run python scripts/create_ladder_zip.py
```

This writes `Zasz.zip` with the bot plus ares, python-sc2, map_analyzer and the **Linux** build of
cython_extensions (downloaded from PyPI), so it can be built on Windows. `Debug` in `config.yml` must be
`False`. Upload it on aiarena.net as a Python Zerg bot.
