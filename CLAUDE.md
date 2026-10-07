# Zasz — notes for Claude

Zerg StarCraft II bot on [ares-sc2](ares-sc2/) (python-sc2), zergling-only by choice, for the AI Arena ladder.
Setup and code layout: README.md; Linux (headless SC2): docs/linux-setup.md. Open work: `todo.md` (the only
backlog). Milestones and what they measured: `docs/progress.md`.

**Ladder limits** (AI Arena's match controller): Python 3.12; the bot acts every 2 game loops; 30 s to answer
any request (a timeout loses the game); games capped at 60 min; the average bot time per game loop is reported
against 40 ms (not enforced, but keep well under it).

## Working here

- Work happens on `squads` (`main` is behind it). Each change gets its own branch off `squads`, named after
  its `todo.md` section; push it when done. Merge into `squads` or `main` only when the user asks.
- Match the surrounding code's style: plain-language comments and docstrings; tunables in `bot/settings.py`.
- When a branch finishes a `todo.md` item, remove it there; add a `docs/progress.md` line if it's a milestone.

```sh
uv sync                                            # once; submodule: git submodule update --init
uv run python -m pytest -q                         # offline tests (~1 s)
uv run ruff check . && uv run ruff format bot tests scripts scenarios
uv run python scripts/run_scenarios.py --repeat 3  # staged scenarios (~4 min); -v for game logs
uv run python scripts/benchmark.py --games 21 --seed 1 --jobs 6   # every map x race once vs Hard (~25 min)
uv run python scripts/run_scenarios.py --order-log DIR   # every command sent; compare_orders.py A B
```

Scenario and benchmark rows go to `benchmarks/*.csv`, labelled with the commit (`-dirty` for uncommitted
changes), SC2 build and run id: run on committed code, and commit the CSVs (union-merged across machines).

## Judging a change

- **Regression rule (the user's):** results needn't be identical. A change is fine if it's the same *on
  average*. It's a regression if it's clearly worse on average, worse across the board (most scenarios or
  matchups, even a little), or adds a FAIL or crash. Resources lost matter most, time less.
- **Benchmarks:** compare per matchup with the same `--seed` (same matchups and game seeds), over 21 games.
  Windows runs (SC2 build 97563) don't compare with Linux (75689).
- **Scenarios:** 13 give the same result every run: alternate_target, custom_decisions, the four
  expansion_*, efficiency_deep_base/open_field/ramp, fight_winnable, retreat_losing_fight, spores_vs_air,
  wall_techlab. The rest vary on identical code (tank_dive, wall_bays, wall_bunker, efficiency_cramped_*,
  efficiency_open_army*): judge them over `--repeat 3`. `wall_bunker` always fails (expected);
  `efficiency_open_army_free` fails about 1 run in 6 on any code.
- **Refactors** (nothing should change): the order logs of the 13 repeatable scenarios must be identical
  (`--order-log` before and after, then `scripts/compare_orders.py`).
- A 20-minute tie can be logged as "Crash" by the benchmark (the replay is too big to save): check the log.

**Reference numbers** (Linux, bot code `679b123`): 21 vs Hard `--seed 1`: 19 wins + 2 Protoss ties, ~92-101k
resources lost, ~10 ms per game loop; scenarios `--repeat 3`: ~2,700-2,720 summed resources lost.

## Previous session work

Newest first, at most 3 entries. When you push a branch, add an entry at the top (date, branch, the few
outcomes that matter next time) and drop the oldest; details belong in commits, `todo.md` and `docs/progress.md`.

- **2026-10-07, `docs-consolidate`:** `todo.md` is now the one backlog (review leftovers, old notes and the
  watch list folded in; new #23 `run-robustness`, #24 `protoss-ties`); `notes.md` and `CODE_REVIEW.md`
  removed; `docs/progress.md` holds milestones only.
- **2026-10-07, `review-phase5-structure` + `review-phase5-army-split` (merged into `squads`):** the code
  review is finished. `bot/army.py` is split into parts (`jobs`, `strikes`, `breach`, `routes`, `avoid`,
  `micro`, `combat`, `army_notes`, `army_debug`) with identical orders; recordings and decisions validated;
  ladder zip slimmed. 21 vs Hard: 19 wins + 2 ties, 91.7k lost, 9.6 ms per game loop.
- **2026-10-06, `review-phase4-behavior` (merged):** the review's behavior fixes, -22% resources lost.
  Reverted: connected-component enemy groups and strict safe corners (both made `efficiency_open_army_free`'s
  army leave the fight); see `todo.md` #21 before retrying.
