# Zasz — notes for Claude

Zerg StarCraft II bot on [ares-sc2](ares-sc2/) (python-sc2), for the AI Arena ladder (Python 3.12, lockstep games:
average bot time must stay under 40 ms per game loop, the bot acts every 2 loops). Work happens on branch `squads`.
Setup and layout: README.md; Linux (headless SC2): docs/linux-setup.md.

```sh
uv sync                                                   # once; submodule: git submodule update --init
uv run python -m pytest -q                                # offline tests (~3 s)
uv run ruff check bot tests scripts && uv run ruff format bot tests scripts
uv run python scripts/run_scenarios.py                    # staged scenarios, top side; -v for game logs
uv run python scripts/benchmark.py --games 6              # full games vs Hard
```

Scenario and benchmark rows are appended to `benchmarks/*.csv` (union-merged across machines: commit them).
Match the surrounding code's style: plain-language comments and docstrings, settings in `bot/settings.py`.

## Current task: fix the CODE_REVIEW.md findings, in phases

CODE_REVIEW.md has 44 findings, each with a **Verification** section (real severity, evidence, proposed fix,
whether the fix **changes behavior**, a test) and a summary table near the top. Fix in this order:

1. **Measurement first (tooling only; the bot doesn't change)** — 26, 29 + 28, 33; optional 34, 32. ← next
2. **Tests guarding the risky code** — 16 + 44 (selector tests on `pick_tiered`; slot accounting, route cache,
   scout cleanup, benchmark helpers).
3. **Perf/cleanup; scenario numbers must stay identical** — 01 + 23, 08 + 09, 12, 10, 15, 14, 13 (step 1), 18,
   11, 17. Batch them; if any scenario number moves, that change has a bug.
4. **Behavior-changing fixes, one per benchmark run** — 03, 04 (needs 03), 02, 07, 20, 05, 06, 21 (after 20),
   24, 25, then 19 (changes the efficiency metric; compare efficiency only between runs that both have it).
5. **Structure/recordings/packaging/docs** — 22 (army.py split, last; orders must stay identical), 37–40, 41,
   42, 35, 36, 27, 30, 31, 43 (docs last).

### Phase 1 details

- **26** — one shared helper (e.g. `scripts/run_meta.py`) used by `scripts/benchmark.py`,
  `scripts/run_scenarios.py` and `scenarios/efficiency.py` (which reads `.git` by hand and never marks
  `-dirty`): commit via `git rev-parse --short HEAD`, dirty via `git status --porcelain -- . ':!benchmarks'`
  (ignore the CSVs the runs themselves append to). Add `sc2_build` and `run_id` columns to `results.csv` and
  `efficiency.csv` (append at the end so old rows parse; `open_results` rewrites the header — do it via a temp
  file + `os.replace`, see 30), `run_id` to `scenarios.csv`; `compare()` skips rows from the current run.
- **29 + 28** — `benchmark.py --seed` (default random, printed): seeded shuffle of the full map × race product
  (not two zipped cycles), `random_seed=seed + i` passed to `run_game`, `seed` recorded per row; print the
  planned matchups first.
- **33** — validate `--games/--jobs/--repeat >= 1`, `--races` against `Race`, `--maps` against
  `installed_maps()` before launching; exit 1 if any game is a `Crash`; no division by zero on an empty run.
- Add offline tests for the helper and the matchup planner (no SC2). Then rerun the baseline (below) with the
  new tooling to get properly labelled baseline rows.

## Baseline to compare against (Linux, SC2 build 75689, code `b8a4d22`, 2026-10-05)

Rows are labelled `fb600ec` / `fb600ec-dirty` (finding 26). Until 26 is fixed, **commit before benchmarking**.

- Full games vs Hard: 6/6 wins, 0 crashes, 457–612 s, 8.9–11.2 ms per game loop, max step 126–156 ms.
- Scenarios (top side; status / game s / zerglings lost): all PASS except **wall_bunker FAIL 150 s / 21 —
  expected, leave it alone** (a tight race even played optimally). tank_dive 6.8/6, alternate_target 85.5/2,
  efficiency_cramped_main 112.7/15, efficiency_cramped_more_zealots 140.6/17, efficiency_open_army 15.6/10,
  efficiency_open_army_free 58.8/26, wall_techlab 35.0/3, wall_bays 29.2/4; the rest pass with 0 lost.
- A regression: any new FAIL or crash, or a big jump in losses/time. Pure perf/cleanup must reproduce the
  scenario numbers exactly (fixed seeds); behavior-changing fixes may move them. Full-game win rates over a few
  games are noisy: compare per matchup with the same maps/races. Windows runs (build 97563) aren't comparable.
