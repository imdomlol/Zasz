# Zasz

A Zerg StarCraft II bot built on [ares-sc2](https://aressc2.github.io/ares-sc2/), which extends
[python-sc2](https://github.com/BurnySc2/python-sc2).

Strategy: make 16 drones and a spawning pool, then keep expanding whenever the drones are about to fill
every base (or patches mine out), up to 70 drones. Research zergling speed, make a queen per hatchery
(injecting larva) plus creep queens, pump zerglings, and attack once there are 16 (until the army drops
below 8). Zerglings move in squads of about 8, each with its own job: patrolling where we've had no
vision longest, raiding enemy bases with as many zerglings as the combat sim says they need, helping
a squad in a fight it's losing (only when the helpers together turn it), defending our bases. Squads
fight or retreat on their own, go around armies they can't beat, and gather outside the enemy's
reach to go in together. Overlords watch the enemy's expansions from safe spots.

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

Current state, verified results and open items: see [docs/progress.md](docs/progress.md).

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
uv run python scripts/benchmark.py                      # 6 games vs Hard, all races, all maps
uv run python scripts/benchmark.py --games 12 --difficulty VeryHard --races Terran Protoss
uv run python scripts/benchmark.py --games 30 --jobs 6  # 6 games at a time (~1 core, ~1.5 GB each)
uv run python scripts/benchmark.py --record             # also save (observation, decision) pairs
```

With `--jobs`, games run in parallel, but logs and results are still reported in matchup order, the same
as a sequential run. Bot behavior doesn't depend on wall-clock speed (games aren't realtime), so results
are comparable either way.

Every game is appended to `benchmarks/results.csv` with the git commit, so you can compare versions.

**Step time** is measured on every game the way the AI Arena ladder measures it (the bot's own
time per step, not time waiting on SC2) and checked against the ladder's limits: a step over 30 s
loses the game by timeout, and the average per game loop should stay under the 40 ms the ladder
reports against. The summary shows the worst game against each limit; a game over a limit keeps
its replay and the run exits with 1. Parallel games slow each other down, so check a game that's
over the limit with `--jobs 1`.

If the bot crashes in a game, that game is recorded as `Crash` (with the error) and the run carries on.
Replays are saved to `replays/benchmarks/<run>/`. By default (`--replays notable`) only losses, ties,
crashes and unusually slow wins (over 1.5x the usual win time at that difficulty) are kept. Use
`--replays all` or `--replays none` to change that. Each crash also gets a `.log` with its traceback and
game log.

| Commit | Opponent | Result | Notes |
|---|---|---|---|
| `12faa25` | Medium | 4/6 | baseline: one-point attacks, no upgrades |
| `467ba00` | Medium | 6/6 | squads + combat sim, per-unit targeting, upgrades, creep; all wins in 6.5-8.6 min |
| `75aba21` | Medium | 6/6 | overlord scouting, tank fixes, fighters-before-buildings; wins in 6.6-8.8 min, no crashes |
| `931c39e` | Hard | 6/6 | from a sweep of every difficulty: 6/6 up to Hard, then Harder 3/6 (3 ties), VeryHard 4/6 |
| `9bcfdcb` | Hard | 6/6 | tanks-and-scouting + no-path crash fix; wins in 7.1-9.4 min, no crashes |
| `59432bf` | Hard | 6/6 | skip unreachable expansions; wins in 7.5-10.4 min, no crashes |
| `1cc08d0` | Hard | 6/6 | learning interface (observe -> decide -> act), same decisions as before; wins in 6.4-9.6 min |
| `9bf51ae` | Hard | 20/21 | squads, after the review's performance fixes (Linux, `--games 21 --seed 1`); 1 tie, 128k lost |
| `d6e38de` | Hard | 19/21 | the review's behavior fixes (same run); 2 ties, 101k lost (-22%), 10.8 ms per game loop |

The default opponent used to be Medium. It's now Hard, because Medium was 6/6 every time and no longer
caught regressions.

## Scenarios

Staged situations that check one behavior each in a few seconds of game time (the bot is
debug-spawned into the situation against a passive VeryEasy AI). Faster and less noisy than a
benchmark, so run them after any change to the bot:

```sh
uv run python scripts/run_scenarios.py                  # all of them, enemy main at the top; exits 1 on a failure
uv run python scripts/run_scenarios.py --side both      # each from both sides (~100 s)
uv run python scripts/run_scenarios.py tank_dive -v     # one scenario, with its game log
uv run python scripts/run_scenarios.py efficiency_cramped_main --side both --replays all
uv run python scripts/run_scenarios.py --random --repeat 3   # random starts, to look for flaky ones
uv run python scripts/run_scenarios.py --list
```

**Repeatable runs.** Every run uses a fixed game seed for its side, so the same code plays a
scenario out exactly the same way every time (start location, the built-in AI's choices, and our
own randomness: ares' creep spots and squad ids are seeded from it too). That makes runs comparable
number for number, not just pass/fail: each result goes to `benchmarks/scenarios.csv` (units and
resources lost, damage taken and dealt, game time, commit; `-dirty` for uncommitted code), and is
printed next to the last earlier run of the same scenario, side, seed and StarCraft II build:

```
PASS  efficiency_cramped_main[top]  ... (80s game; lost 22 units (550 resources), damage taken 732)
      vs af4d1c8-dirty: army lost 550 (same), damage taken 732 (same), game time 80s (same)
```

A side is where the *enemy's* main is ("top"/"bottom", or "left"/"right" on maps laid out that
way). Which seed gives which side depends on the map and the StarCraft II build, so it's found once
with short probe games and kept in `scenarios/seeds.json` per build (a Windows client finds its own
on first use).

**Watching a scenario.** `--replays all` keeps every replay (named like
`efficiency_cramped_main[top].SC2Replay`); open them in the game on Windows. On Windows you can
also watch one live at normal speed in the game window:

```sh
uv run python scripts/run_scenarios.py efficiency_cramped_main --side bottom --realtime
```

Each one lives in `scenarios/` as a small class (`setup`, `check`, `on_timeout`); see
`scenarios/__init__.py` for how to write one and the debug quirks to watch for. Replays of failed
scenarios are kept in `replays/scenarios/`. If StarCraft II crashes or freezes mid-scenario, a
watchdog reports it as an ERROR and the run carries on (`--watchdog SECONDS` to change the limit).

**Damage efficiency** (`efficiency_*` scenarios): 30 zerglings vs structures on open ground, in a
4-deep base and up the enemy's main ramp. They pass when everything is dead and report kill time,
damage per second, and how much of the army's damage potential landed (delivered vs blocked vs
idle, from `bot/efficiency.py`), appended to `benchmarks/efficiency.csv` with the commit, side, seed
and status. The `efficiency_cramped_*` and `efficiency_open_army*` scenarios do the same against
zealots and stalkers, with and without a grid of gateways around them:

```sh
uv run python scripts/run_scenarios.py efficiency_open_field efficiency_deep_base efficiency_ramp --repeat 2
```

Real games log the same summary at the end ("Army damage: ...").

## Decisions and the learning interface

Each step the bot runs one loop, built so that the scripted rules can be swapped for a learned
model without touching anything else:

```
observe(ai) -> Observation -> decisions.decide(obs) -> Decision -> actions carry it out
```

- **`Observation`** (`bot/observation.py`): everything decisions get to see, as a flat set of
  numbers with a fixed shape: economy, bases, tech, our units/structures/upgrades, and what we know
  about the enemy. `to_vector()` / `FEATURE_NAMES` give the model input.
- **`Decision`** (`bot/decisions/__init__.py`): every *what* choice, as a fixed action space:
  what to save for (nothing / pool / expansion), drone target, army proportions, upgrade wish
  list, attack or defend, queens, spores per base. `to_vector()` / `DECISION_NAMES` give the
  model output. The fixed lists behind both shapes are in `bot/spaces.py`.
- **`Decisions.decide(obs)`** only gets the observation, never the bot. `ScriptedDecisions` is the
  hand-written rules; a model implements the same method and is passed as
  `ZaszBot(decisions=...)` (the `custom_decisions` scenario does exactly that).
- **Actions stay scripted** (`bot/actions/`, `bot/army.py`): worker choice, timing, placement and
  micro. A new *what* choice gets a `Decision` field instead of living in an action.
- **Recording** (`bot/recording.py`): `benchmark.py --record` saves each game's (observation,
  decision) pairs as JSON lines in `data/decisions/<run>/`; `read_records()` loads them back
  (`iter_steps()` streams a large one), e.g. to train a model to imitate the scripted rules (or
  anything else recorded). Reading checks the format version and feature names.

Decisions only see the observation, plus whatever state the policy keeps itself: the scripted
rules remember whether they're attacking (start at 16 zerglings, stop below 8), so between those
counts the same observation can give either answer. A recording replayed in order reproduces it; a
model trained on single steps can't see it. Either way the rules need no game to test: they have
offline unit tests (`uv run pytest`, no StarCraft II needed).

## Layout

```
bot/
├── zasz.py            # ZaszBot: observe -> decide -> act, each step
├── settings.py        # all tunables (POOL_AT_DRONES, EXPAND_WHEN_DRONES_WITHIN, ...)
├── observation.py     # Observation + observe(): what decisions get to see
├── spaces.py          # the fixed unit/upgrade/race lists behind observations and decisions
├── recording.py       # save / load (observation, decision) pairs
├── efficiency.py      # damage efficiency: how much of the army's damage potential lands
├── targeting.py       # target slots: spread melee attackers over structures (fill from the back)
├── overlay.py         # debug drawing of what the army is doing (--overlay)
├── decisions/         # WHAT to do
│   ├── __init__.py    #   the rules every policy follows
│   ├── types.py       #   Decision (the action space) and the Decisions interface
│   └── scripted.py    #   hand-written rules (a learned model would sit next to this)
├── actions/           # HOW to do it well: stays scripted
│   ├── macro.py       #   a Decision's spending: supply, pool/expansion timing, drones, army
│   ├── drones.py      #   BuildDrones (counts eggs), drone_target
│   ├── building.py    #   just-in-time builder drones, next expansion spot
│   ├── pathing.py     #   why an expansion can't be reached (rocks, enemy wall, ...)
│   ├── upgrades.py    #   gas, tech buildings and research for the upgrade wish list
│   ├── queens.py      #   inject + creep queens; queens defend (our only anti-air unit)
│   ├── defense.py     #   spore crawlers once enemy air shows up
│   └── scouting.py    #   overlords posted at enemy expansions, kept out of anti-air
├── squads.py          # SquadBook: squads of zerglings and their jobs (patrol, raid, help, defend)
└── army.py            # plans the jobs (raids, help, strikes, rallies) and fights: fight or retreat, targets
run.py                 # entry point for local and ladder games
ladder.py              # AI Arena connection glue
config.yml             # ares settings (overrides ares-sc2/src/ares/config.yml)
scenarios/             # staged scenarios (expansions, defense, army, decisions, efficiency), run by scripts/run_scenarios.py
tests/                 # offline unit tests (uv run pytest)
scripts/
├── benchmark.py       # win-rate benchmark
├── run_scenarios.py   # scenario runner
├── run_meta.py        # commit, StarCraft II build and run id on every result row
├── bootstrap.py       # import paths for the scripts and tests (run.py keeps its own for the zip)
└── create_ladder_zip.py
benchmarks/            # result CSVs (tracked; union-merged across machines)
experiments/           # dropped army experiments, kept as patches (see its README)
docs/                  # Linux setup, progress notes
ares-sc2/              # git submodule, pinned to a known-good commit
```

From ares, Zasz uses `Mining` (speed mining, worker distribution), `AutoSupply`, `BuildStructure`,
`ExpansionController` (via `ExpandTo`) and `SpawnController`, ordered by a `MacroPlan` so higher priorities get the
minerals first. Every `ZaszBot` hook must call `super()` or ares stops working.

## Development

```sh
uv run ruff check --fix .    # lint
uv run ruff format .         # format
uv run pytest                # offline unit tests (~1 s)
uv run python scripts/run_scenarios.py   # staged in-game scenarios (~40 s)
```

Updating ares: `git -C ares-sc2 pull`, run the benchmark, then commit the new submodule pointer.

## Uploading to AI Arena

```sh
uv run python scripts/create_ladder_zip.py
```

This writes `Zasz.zip` with the bot plus ares, python-sc2, map_analyzer and the **Linux** build of
cython_extensions (downloaded from PyPI once, then cached in `.cache/`), so it can be built on Windows.
Only the ladder's binaries go in (Linux, Python 3.12), and a failed build leaves the previous zip as it
was (`--out PATH` writes elsewhere). `Debug` in `config.yml` must be `False`. Upload it on aiarena.net
as a Python Zerg bot.
