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

1. **Measurement first (tooling only; the bot doesn't change)** — 26, 29 + 28, 33; optional 34, 32. Done in
   `cfbdb7e` (26, 28, 29, 33, 34; 32 not done).
2. **Tests guarding the risky code** — 16 + 44 (selector tests on `pick_tiered`; slot accounting, route cache,
   scout cleanup, benchmark helpers). Done in `e9c7c75`. Known bugs are pinned as strict xfails (07 x3, 03 x2,
   02 x1): a phase 4 fix makes its xfail "XPASS(strict)" fail — then remove the mark.
3. **Perf/cleanup; scenario numbers must stay identical** — 01 + 23, 08 + 09, 12, 10, 15, 14, 13 (step 1), 18,
   11, 17. Batch them; judge them by the regression rule below (same on average, not number for number). Done
   in `61c9ec6`..`9bf51ae` (13 step 1 only; 10 without the shared breach-graph structure; 18 without moving
   expiry to one pass per step).
4. **Behavior-changing fixes, one per benchmark run** ← next — 03, 04 (needs 03), 02, 07, 20, 05, 06, 21 (after 20),
   24, 25, then 19 (changes the efficiency metric; compare efficiency only between runs that both have it).
5. **Structure/recordings/packaging/docs** — 22 (army.py split, last; orders must stay identical), 37–40, 41,
   42, 35, 36, 27, 30, 31, 43 (docs last).

### Phase 1 (done): how runs are labelled now

- `scripts/run_meta.py`: commit (`-dirty` for any change outside `benchmarks/`, untracked files included),
  `sc2_build` and a unique `run_id` per invocation, on every row of all three CSVs. Run directories are named
  by run id. `run_scenarios` compares each result with the last *earlier run* (any commit) of the same
  scenario/side/seed/build.
- `benchmark.py --seed N` (random if omitted, printed): a race-balanced cycle through every map x race pair,
  game seed N + i; the plan is printed first. Same `--seed`, `--games` and maps = same matchups. Exits 1 on any
  crash. With the 7 Linux maps, `--games 21` plays every pair once.

### Phase 4 plan (agreed 2026-10-05, running overnight on branch `review-phase4-behavior`)

Order: 03, 04 (needs 03), 02, 07, 20, 05, 06, 21, 24, 25 (measure role switches first), 19. One commit per fix;
remove the matching strict xfail marks as tests start passing. Check per fix: scenarios `--repeat 3` + `benchmark.py
--games 6 --seed 1 --jobs 6` (`--games 21` for 20 and 21), compared with the previous fix's run, by the regression
rule below. Worse: one reworked attempt, else revert and note why here. Push after each group: (03, 04, 02),
(07, 20, 05), (06, 21, 24), (25, 19). Stop after phase 4 (phase 5 waits for the user).

Phase 4 status: 03, 04, 02, 07, 05 kept (checks: same or better; 03+04 6/6 vs Hard at a third of phase 3's losses).
06 reworked: strict corner rule only for safe areas; plugs/breaches/widening keep 8-connected labels (strict ones made
efficiency_open_army_free leave every run). 20 reverted: it made that scenario's army leave ~40% of runs (0 of 15 before);
the full games were fine (20/21), so grouping is worth another try once that's understood. A raid's gather clock now
follows its moving site (found on the way). Ultralove vs Protoss is the recurring non-win (ties in about half the runs,
on every version): worth its own look.

## Baseline to compare against (Linux, SC2 build 75689, code `cfbdb7e`, 2026-10-05)

Bot code is identical to `b8a4d22` (the earlier baseline, rows labelled `fb600ec-dirty`).

- After phase 3 (`9bf51ae`, run `20261005-225558-3610`, same `--seed 1`): 6/6 wins (the Ultralove Protoss game
  won at 1189 s), bot time per game loop lower in 5 of 6 games (7.5–16.1 ms; 20.2 ms in the 20-minute game).
  Scenarios: the repeatable ones identical, the noisy ones the same on average.
- Full games vs Hard, `benchmark.py --games 6 --seed 1 --jobs 6` (run `20261005-220447-62fd`): 5 wins, 1 tie,
  0 crashes. **Protoss on UltraloveAIE_v2 (game seed 3) ties at the 20 min limit** (lost 34k resources;
  replay kept) — a matchup the old baseline didn't play, so not a regression, but worth a look. The wins:
  447–723 s, 8.6–15.9 ms per game loop, max step 111–184 ms. Rerun with the same `--seed 1` to compare.
- Scenarios (top side; status / game s / zerglings lost; run `20261005-220234-9d7a`): all PASS except
  **wall_bunker FAIL 150 s / 21 — expected, leave it alone** (a tight race even played optimally).
  Exactly repeatable: tank_dive 6.8/6, alternate_target 85.5/2, wall_techlab 35.0/3, wall_bays 29.2/4; the rest
  pass with 0 lost.
- **Not exactly repeatable, even on identical code with a fixed seed** (4 runs at `cfbdb7e`): 
  efficiency_cramped_main 106.9–111.5 / 16–17, efficiency_cramped_more_zealots 132.1–140.2 / 19–24,
  efficiency_open_army 15.5–16.1 / 9–11, efficiency_open_army_free 57.8–66.3 / 26–34. Judge these over a few
  repeats (`--repeat 3`), by their average.
- **efficiency_open_army_free fails now and then on any code**: about 1 run in 6 times out at 120 s having
  lost only 2 (the squad leaves the fight), sometimes one loses ~40. Baseline `cfbdb7e`: 4 FAIL of 28 runs;
  phase 3 `9bf51ae`: 5 of 27. A single FAIL there isn't a regression; compare fail rates over ~18 runs.
- **What counts as a regression (the user's rule):** results don't have to be identical, for any phase. A
  change is fine if it's the same *on average*: one run much better and another worse is fine. It's a regression
  if it's significantly worse on average, or worse across the board (most scenarios/matchups worse, even a
  little), or a new FAIL or crash. Losses (resources lost) matter most, time less. Full-game win rates over a
  few games are noisy: compare per matchup with the same `--seed`. Windows runs (build 97563) aren't comparable.
