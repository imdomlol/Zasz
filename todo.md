# Zasz todo

The one list of open work. Each `##` section is a **potential branch**: one idea, scoped so it can be
reviewed and merged on its own. Branch off `squads` with the name given (never commit to `main`). When a
branch is done (scenarios + benchmark judged as in CLAUDE.md), remove its section here, or the items it
finished, and add a line to `docs/progress.md` if it's a milestone.

Numbers are fixed IDs (sections refer to each other by them), so finished items leave gaps. Impact is
rated against where Zasz is now (zergling-only; 19-21 of 21 vs Hard, Protoss ties the usual non-win):
**High** = likely to move win rates at Harder+ or unblock the learning work; **Medium** = clear benefit,
but narrow or indirect; **Low** = nice to have / tooling.

## Branches, in suggested order

| # | Branch | Idea | Impact | Depends on |
|---|---|---|---|---|
| R | `review2-phase1..4` | **Next.** The 2026-10-07 repository review, in 4 phases (section below) | High | - |
| 20 | `squads-polish` | Open squad behaviors: help flip-flop, raids that leave, pushes into the unknown, strike sizing | High | - |
| 21 | `combat-estimates` | Fix the sim's blind spots: approach under fire, chokes, fog, bunkers; enemy groups | High | - |
| 24 | `protoss-ties` | Find out why games vs Protoss (Ultralove, Magannatha) tie at 20 minutes | High | - |
| 22 | `army-cpu` | Keep the army's step time in budget in long games with big armies | High | - |
| 18 | `safe-build-location` | Build at a safe base, not always the main (rebuilding into a lost main) | High | - |
| 19 | `creep-to-natural` | Connect main and natural creep down the ramp (queen movement) | Medium | - |
| 13 | `damage-efficiency` | Ramp throughput, a better idle signal, the mutalisk cap | Medium | - |
| 15 | `external-opponents` | Play against other people's bots | High | - |
| 23 | `run-robustness` | Benchmark/scenario runner: replay-save crash, a watchdog that really fires | Low | - |
| 8 | `trade-tracking` | Ally vs enemy losses (resource trades) | Medium | - |
| 9 | `mining-rate` | Mining rate per drone | Low | - |
| 10 | `army-value` | Army value: ours vs the enemy's | Medium | - |
| 14 | `supply-targeting` | Target supply buildings when the enemy is near its supply cap | Medium | `army-value` |
| 2 | `army-variety` | More unit types + army composition logic | High | - |
| 3 | `army-upgrading` | Upgrades tied to army composition | Medium | `army-variety` |
| 11 | `decision-explanations` | Explain decisions in chat (the in-game overlay exists) | Medium | `trade-tracking` |
| 12 | `mutalisk-harass` | Mutalisk worker harass, decided by expected value | Medium-High | `army-variety`, `army-value` |
| 1 | `replay-summary` | Watch replays on Linux | Low | - |
| 6 | `static-defense-placement` | Better spore / spine placement | Low-Medium | - |
| 7 | `drone-escape-building` | Drone saves itself by starting a building | Low-Medium | - |

## Repository review 2026-10-07 (R01-R21), in phases

A review of `79f1097`, each finding checked against the code with offline probes. Planned order (starting waits for the user): phases
1 -> 4, one branch per phase (`review2-phase1-measurement`, `review2-phase2-refactors`,
`review2-phase3-behavior`, `review2-phase4-runners`). Rank = expected impact (1 = most). Within a phase, one
commit per item; judge each as in CLAUDE.md ("Judging a change").

### Phase 1: trust the measurements (tooling only; the bot doesn't change)

R13 before R14/R15 (they add CSV columns). Ends with a **new baseline**: scenarios `--repeat 3` and
`benchmark.py --games 21 --seed 1 --jobs 6`; later phases compare against it (R14 changes benchmark variance).

- **R11 (rank 5): the order-log check can pass with no evidence.** `scripts/compare_orders.py` exits 0 when
  both directories are missing or empty, two empty logs read "same (0 commands)", and a crashed game leaves a
  partial log that looks complete. Fix: `ScenarioBot` writes an end line (`# end <status> loop=<n>
  commands=<k>`) when the outcome is decided; `run_scenarios` writes `DIR/manifest.json` (run id, commit,
  build, each run's status, `complete`); add `REPEATABLE` (the 13 scenarios in CLAUDE.md) to the scenarios
  package. `compare_orders` defaults to that set (`--only NAME...` for a subset) and fails on a missing
  directory, an incomplete manifest, a missing expected scenario, a log without its end line, or nothing
  compared; it ends with "N scenarios, M commands compared". Also: `scenarios/army.py` reads the same
  `ZASZ_ORDER_LOG` variable as a "1" flag; give one of them another name. Tests: each failure exits 1; two
  complete identical logs exit 0.
- **R13 (rank 6): merged CSVs are read with the first header only.** `run_meta.read_rows` skips embedded
  header lines but keeps the first header: an old-then-new merge reads new rows as `{None: [...]}`, and
  `ensure_header` then crashes on startup. A real git union merge also duplicated old rows (an upgraded
  header pads old rows with a trailing comma, so they no longer match the other machine's copy). No CSV has an
  embedded header today; the next new column would cause one. Fix: track the active header per segment and
  map rows to the union of known columns by name; exit with a clear message on unknown fields; on upgrade
  rewrite only the header line (don't pad old rows). Tests: old->new and new->old merges keep every value;
  `ensure_header` succeeds; a reordered embedded header maps by name.
- **R20 (rank 7): no CI.** Add `.github/workflows/ci.yml`: checkout with the `ares-sc2` submodule, Python
  3.12, `uv sync --locked` (needs GitHub access for the git-pinned python-sc2 and map_analyzer), `uv run ruff
  check .`, `uv run ruff format --check .`, `uv run python -m pytest -q`. No StarCraft II. Add each fixed
  bug's regression test as it lands.
- **R14 (rank 10): benchmark games aren't seeded on our side.** Scenarios fix the hash seed and seed
  `random` (`run_scenarios.make_repeatable`); the benchmark seeds only the game. The main source of
  run-to-run difference is Python's hash order (unit-type enums hash by name, so sets of them iterate
  differently per process); `random` matters in one place (ares' fallback creep-tumor spot); `uuid` and NumPy
  random don't affect play. Fix: one shared seeding setup for both runners, applied before the bot is
  created, with `PYTHONHASHSEED=0` for workers; `--jobs 1` needs a fresh process per game (comes with R12).
  New columns at the end: `jobs`, `host`, `python` (step times from `--jobs 6` and `--jobs 1` look alike in
  the CSV today). Test: both modes see `random` seeded from the game seed; workers see `PYTHONHASHSEED=0`.
- **R15 (rank 12): loss columns measure different things.** Benchmark `lost_value` = minerals lost in army +
  economy only (no gas, no technology); `killed_value` = the game's minerals+gas value of everything killed;
  scenario `army_lost` = minerals+gas of our army units and queens. Comparisons over time are fine (same
  definition throughout); a killed/lost ratio isn't, and it matters once gas units arrive (#2). Fix: keep
  `lost_value`, document it; add `lost_minerals_total`, `lost_vespene_total`, `lost_army_total`,
  `lost_tech_total` at the end. Test: a fake score gives the expected columns, `lost_value` unchanged.
- **R16 (rank 15): comparisons use one row, or the wrong pool.** `run_scenarios.compare` shows only the
  previous run's last repeat (an ERROR row can be the baseline, its blanks read as 0). The benchmark's
  slow-win replay rule pools every Hard win across races, maps and builds (Protoss wins are kept too often,
  slow Zerg wins missed). Fix: compare against the previous run's group (n, fails, mean/median lost, damage,
  time); key win times by (difficulty, race, map, build) with a (difficulty, race) fallback under 5 samples.
  Tests: repeats 10/10/100 report mean 40, n=3; an ERROR row is never the baseline.
- **R21 (rank 20): docs overstate guarantees.** README says scenarios play "exactly the same way every time"
  (only 13 do) and that a watchdog reports freezes (it doesn't fire; R12); `bot/settings.py` says it holds all
  tunables, but `bot/efficiency.py`, `bot/actions/upgrades.py` (`GAS_PER_EXTRACTOR`), `bot/micro.py`
  (`DIVE_FIRST`, `PRIORITY_STRUCTURES`) and some inline numbers (`strikes.py`, `routes.py`) live elsewhere.
  Fix the README wording (link CLAUDE.md's "Judging a change"); say the settings hold the strategy tunables.

### Phase 2: speed and refactors that change no order

Check: the order logs of the 13 repeatable scenarios identical (with R11's checks), plus a `--jobs 1`
benchmark for timing.

- **R18 (rank 3): reach-map flood fill in a Python loop** (`ReachMap.__init__`, `bot/targeting.py`), built
  per squad, per enemy group for strikes (every 0.5 s, uncached) and for breach evaluation: 1.5 / 5.8 / 13 ms
  at 40x40 / 80x80 / 120x120. An exact replacement: `scipy.ndimage.label` with side neighbours, joined across
  allowed diagonal steps (the `_squeezed` rule) with `csgraph.connected_components`, seeds from units on
  blocked cells kept as now. Identical on 700 random grids (226 of them exercised the corner rule); 8-30x
  faster. Add an equivalence test against the old loop (corners blocked by units vs buildings, both sides of
  a wall). Likely part of the 0.5-0.9 s worst steps (#22). Don't retry sharing maps between squads (#22).
- **R17 (rank 19): debug notes built every step.** `fight()` always calls `note_options()`, `note()` always
  formats and stores text, `_control` copies notes into `last_note`: ~0.1 ms per game loop at 100 zerglings
  (~1%). Careful: `note_options` calls `slots.capacity()` for targets in every tier, which settles the shared
  per-step capacity cache for later squads and strikes: removing that call changes orders. Fix: keep the
  capacity calls (comment why), gate only the note writes, the `name_of` strings and the `last_note` copy on
  `Army.diagnostics`; move `CHOKE_NOTES` (a real decision cache) out of `army_notes` as e.g.
  `CHOKE_VERDICTS`. Scenarios read the notes with debug on, so they keep working. Order logs identical with
  debug on and off.
- **R09 (rank 18): observation counts can be changed.** `Observation` is frozen but `own_units` etc. are
  plain dicts the policy could change before the recorder writes them. (Changing the constructor's input
  already can't.) Fix: wrap the dict `__post_init__` already builds in `MappingProxyType`, as `Decision.army`
  does (~0.1 us per step). Test: assigning into `own_units` raises; `to_dict()` still plain numbers.
- **R08 (rank 17): `Decision` validates only `army` and `upgrades`.** `Decision(build='pool')`,
  `attack='false'` (truthy: the army attacks, and it's copied into the next observation),
  `drone_target=nan`, `queens=-1` are all accepted. Fix: require a `Build`, a real bool for `attack`, ints
  (not bool) >= 0 for the counts, with the field named in the error; `from_dict` stays strict (no coercion).
  Test: each case raises, directly and via `from_dict`.
- **R10 (rank 16): the recording reader accepts damaged files.** An empty file reads as an empty recording;
  `decisions` in the header isn't checked; a renamed body feature silently reads as 0; unknown or missing
  record types are skipped or raise a bare `KeyError`; a second header mid-file replaces the first; a step
  after the end record is accepted; body errors have no file:line. Fix: require a header and both name lists
  (decisions against `DECISION_NAMES`); enforce header, steps, at most one end; reject unknown `obs` keys
  (missing ones stay allowed for older files); wrap step errors with file:line; keep `skip_unfinished_line`.
- **R19, first step (rank 21):** replace the queen/spore state attached through `ai.__dict__.setdefault`
  (`bot/actions/queens.py`, `bot/actions/defense.py`) with a small object owned by the bot; prune histories
  of dead tags that never shrink (`last_damaged`, `enemy_state`, `enemy_moved`, `most_hitting`,
  `breach_notes`, `scouted_at`).

### Phase 3: behavior fixes, one benchmark run each

In this order; each judged against the previous fix (scenarios `--repeat 3` + 21 games `--seed 1`). Worse:
one reworked try, else revert and note why here. Watch passivity (R04, R02, R03 make the army more cautious)
and `efficiency_open_army_free`'s fail rate over ~18 runs (the reverted experiments failed there).

1. **R04 (rank 1): avoid areas made by a cannon or bunker clear the next step.** `clear_empty_avoid_areas`
   (`bot/avoid.py`) checks only `ai.enemy_units`, which never contains structures. Areas from "choke not
   worth it", "nothing to do safely" and "no help possible" can be made by static defense; their center is
   usually visible from the back-off line, so they last ~1 step, the site unblocks, and raids walk back into
   its range. ("Army can't beat" areas come from army units only; "scout needed" and "unseen fire" have their
   own rules.) Fix: count visible (not memory), armed enemy structures for those threat-based areas only
   (stricter: store each area's threat tags). Test: an area made by a cannon stays until the cannon is gone
   or 45 s pass. Risk: an unloaded bunker counts as armed; a cannon at a base the army could take holds it
   off 45 s.
2. **R02 (rank 2): rejected raid sites still get spare squads.** `assign_raids` skips a site the sim says
   can't be staffed, but `no_idle_squads` gets the full site list and sends spare squads to the nearest site's
   *center*: they bounce off it every 2 s plan. A skipped site also doesn't mark raids "not going well", and
   spares joining a gathering raid walk to the center, not its staging point. Fix: pass only staffed raids
   (with their current target, staging if gathering) to the fallback; if every site was rejected, keep
   patrolling; set `raids_ok = False` on a skip. Tests: an unstaffable site gets no raiders; a spare joining
   a gathering raid gets the staging point.
3. **R01 (rank 4): help counts helpers on their way twice.** `Army.members` already puts squads helping a
   fight into `Battle.present` (and `room()` subtracts them); `coordinate_help` then adds the en-route
   helpers' units to `present` again and subtracts their count from `room` again. Doubled health makes help
   stop too early; doubled room makes "no help possible" (freeing the fighting squad, marking an avoid area)
   more likely. Fix: combine by unit tag; subtract only newly added units. Not the main cause of #20's help
   flip-flop (that's no hysteresis around the help bar). Test: no duplicate tags reach `choose_helpers`,
   `room == battle.room`.
4. **R03 (rank 9): patrols keep walking at danger that cut their route.** On "no route",
   `control_squad` falls back to ares' path (danger only a cost). A patrol's spot is route-checked only when
   picked, so a group moving across the way makes it bounce. Fix: re-pick a patrol's spot when the route to
   it is cut (same for squad scouts). Not "hold on no route": the route map also says "no route" for any
   target more than 4 cells inside danger, which is every raid on a defended site; holding would stop raids
   from arriving. Raids/scouts: route to the nearest danger edge (wider search), hold only if that edge is
   unreachable; fighting squads untouched. Tests: a patrol facing a new barrier gets a new spot; a raid deep in
   danger still gets a waypoint.
5. **R07 (rank 8): breach widening plans are shared across fights.** `self.widening` and
   `assignments.deferred` (`bot/breach.py`) are army-wide while breach results are cached per fight area. A
   second fight's evaluation wipes the first fight's plan (an ordinary breach clears both; no options drops
   every plan not among its own; no options clears deferred); the first recreates it a second later with a
   new start time, so the 30 s give-up never fires, and its cached result still sends zerglings to the
   building while the target is no longer deferred. Rare (needs a partly walled, still target); unmeasured.
   Fix: key plans by fight area, expire each on its own, build `deferred` from all live plans. Single-fight
   scenarios keep identical order logs. Test: A's plan and timer survive B's evaluation.
6. **R05 (rank 13): an overlord scout abandoned under fire keeps flying to its old spot.** `Scouting.on_damage`
   drops the assignment, but the go-home branch needs `is_idle`; with no danger on the air grid (cloaked or
   burrowed shooter, splash) the old move continues. A finished or expired request also leaves the overlord
   flying onto the spot. Fix: a `returning` set, sent home once safe. Test: a non-idle overlord gets a home
   path after `on_damage`.
7. **R06 (rank 14): expansion checks miss a depot raising or lowering.** `ExpansionAccess` fingerprints
   blocker tags only; a raised depot keeps its tag, so a cached "reachable" lasts up to 10 s. Mined-out
   minerals aren't in the fingerprint either. Fix: fingerprint `(tag, type_id, position)` plus the mineral
   count; keep creep tumors excluded. Test: raising a depot triggers a recheck next step.

### Phase 4: runner robustness

- **R12 (rank 11): no deadline enforced from outside a game.** The scenario watchdog calls
  `_thread.interrupt_main()`, which can't wake `asyncio.run` waiting on SC2's socket (probe: still hung 7 s
  later): it never fires. Parents wait on `future.result()` with no timeout; the benchmark has no wall-clock
  limit and runs `--jobs 1` games in the parent; results are read in submission order, so games finished
  behind a stuck one aren't written, and one dead worker breaks the pool. Fix: one subprocess per game under a
  parent-side deadline (kill the process group, SC2 included); write rows as games finish, keyed by game
  index, ordered summary at the end; same path for `--jobs 1` (gives R14 its fresh process). Includes #23's
  replay-save crash. Test: a fake worker blocking on a socket returns ERROR within its deadline, leaves no
  child process, and later games are still recorded.
- **R19, the rest:** typed state owners for routes and fight plans, one subsystem at a time, order logs
  identical for each move. Low priority.

## Watch list (not branches)

- **`wall_bunker` fails, expected**: a tight race even played optimally. Ideas for it are under #20.
- **`efficiency_open_army_free` fails now and then on any code** (about 1 run in 6: the squad leaves the
  fight). Compare fail rates over ~18 runs, not single runs.
- **`TANK_NEUTRALIZED_BY = 3`** (`bot/settings.py`): sieged tanks with 3+ zerglings inside their minimum
  range are left out of the sim (which ignores minimum range). A judgement call; revert if it misleads.
- **ares' combat sim**: no minimum range, positions barely matter, assumes clumped units. Keep in mind
  when tuning `FIGHT_AT` (see #21).
- **The built-in AI surrenders** when clearly losing, so a win can come with few kills.
- **Untested in real games**: spore crawlers (games end before enemy air), overlord scouts under pressure.
- **Torches' expedition gates** (2000 HP, 3 armor) wall off 4 edge bases per side; ares leaves them out of
  our expansions. Rock/gate breaking (`ExpansionAccess.blocked` lists the rocks per base) would open them,
  and backdoor rocks on LeyLines and Magannatha.

## `squads-polish` (#20): Open squad behaviors

**Idea:** The squad army (jobs, help, rallies, strikes, routes, breaches) is in and benchmarked. These are
the behaviors still seen going wrong in scenarios and games.

- **Help flip-flop:** help sends and frees a squad repeatedly while the verdict hovers around the help bar
  (no hysteresis: helpers are freed as soon as the verdict reaches `HELP_WIN_AT`). Review R01 fixes a
  related double count, not this.
- **Raids that leave:** a squad freed by "leave" turns into a patrol inside the raid's site and marks
  "no help possible" avoid areas. Proposed: raid squads stay on their raid; patrols fighting at a raid's
  site join it.
- **Chasing up a ramp:** `efficiency_open_army_free` still loses ~30 zerglings chasing an army up a ramp
  held by zealots. Needs #21's choke estimate, or a rule not to follow an army up its ramp.
- **`wall_bunker`:** zerglings that don't fit around the full bunker hit the bays beside it, under its
  fire. Saved ideas in `experiments/free-hits-two-pass-unit-breach.patch`: overflow only takes free hits,
  two-pass target assignment (idle zerglings first), breaching through a unit as well as a building.
- **Pushes into the unknown** spread out but still walk the whole army into a defended position. Ideas:
  a vanguard (one squad a few tiles ahead), or stopping at the edge of vision until something has looked.
  (Squads with no safe route: review R03, phase 3.)
- **Strike sizing:** strikes send the fewest zerglings that win, which sent 2, then 6, then 12 into a
  defended natural. Add a minimum (a full squad) or a margin.
- **Squad scouts** with no overlord coming have no limit beyond 60 s; check real games.
- **A mineral-wall map** from the benchmark (Magannatha or Ley Lines): identify it and add a scenario.
- **`Army.committed`** (scenario-only "fight no matter what") is still used by
  `efficiency_cramped_more_zealots`; revisit once #21 makes the sim's verdict on that fight better.

**Impact:** High: these are where zerglings die for nothing. **Blockers:** each needs its scenario; judge
on the regression set (`--repeat 3`) and a 21-game benchmark.

## `combat-estimates` (#21): Fix the combat sim's blind spots

**Idea:** Many bad commits trace back to the sim's verdict being wrong for melee units, not to the
decision logic. Every fight, strike, raid and help judgement goes through one `combat_estimate`
(`bot/combat.py`: tank neutralization and the bunker path); correct it further where we can estimate better.

- **Approach under fire:** the sim assumes everyone fights at once. Melee units first cross the ranged
  enemies' range: estimate the damage taken on the way (time to cross x their dps) and take those
  zerglings off before judging. The sim called 2 zerglings vs 2 sieged tanks VICTORY_EMPHATIC.
- **Sieged tanks:** splash and the long approach.
- **Chokes:** only as many zerglings as fit around the reachable enemies fight at once (a ramp held by
  zealots). A first try broke the gateway grid by calling sealed fights hopeless: leave free work and
  breaches alone, and only judge *entering* a choke. Per target, `choke_worth` already refuses melee units
  with 1-2 open sides when they cost more than they're worth; the squad's verdict still ignores chokes.
- **Fog:** enemies revealed by firing are only part of a position. The partly-seen rule holds back now;
  the estimate could also assume unseen support.
- **Bunkers:** calibrate the two-step estimate (assumes 4 marines inside).
- **Shared arithmetic (no behavior change):** `evaluate_breach`, `widen_plan`, `choke_worth` and
  `through_bunkers` each compute "seconds to kill x covering DPS / health"; one helper would do.
- **Enemy groups:** `cluster` (`bot/squads.py`) adds a unit to the first cluster it touches and never
  merges clusters, so a chain of enemies can be judged as two halves, and the split can flip between
  steps. Connected components fix it, but a try made `efficiency_open_army_free`'s army leave ~40% of
  runs (0 of 15 before; full games were fine). Understand that first.
- **Reach-map regions:** `set_danger`, `breaches` and `widen_plan` label regions with plain 8-adjacency,
  while the reach search refuses a diagonal step next to an enemy unit. A pocket touching our side only
  through such a diagonal counts as reached, so its breach is missed. Narrow geometry; one labelling
  helper with the search's adjacency would fix it.

**Impact:** **High.** It feeds every fight decision. **Blockers:** estimates that are too pessimistic make
the army passive (the choke try did). Measure every change on the seeded scenarios *and* a benchmark.

## `protoss-ties` (#24): Games vs Protoss that tie at 20 minutes

**Idea:** The recurring non-win: Ultralove vs Protoss ties in about half of all 21-game runs on every
version, Magannatha vs Protoss sometimes, losing 15-35k resources over 20 minutes. Watch the kept
replays (`replays/benchmarks/<run>/`, on Windows) and find what the army does wrong: likely feeding into
a defended position the sim misjudges (#21), or not finishing a base.

**Impact:** High: it's the main gap to 21/21 vs Hard. **Blockers:** needs watching replays (Windows) or
#1's summaries.

## `army-cpu` (#22): Keep the army's step time in budget

**Idea:** 21 games vs Hard average 9.6 ms per game loop (24% of AI Arena's 40 ms), but a 20-minute game
with a big army reaches ~20 ms with 6 games in parallel, and single steps up to ~0.5-0.9 s. Find what
grows with army size and cache or trim it.

**Revision:** Profile a long game (`--jobs 1`, headless): per-squad reach maps, approach and group fields
(Dijkstra per target), strikes (a reach map per enemy group every 0.5 s), region labels, patrol vision
checks, `fight_result` sims per squad. Ideas: spread heavy work over steps; one reach map per fight area
(a try shared per walled-off region cut army time 12% but broke `efficiency_open_army_free`).
Review R18 (phase 2) replaces the reach-map flood fill (8-30x faster, identical results) and R17 trims the
per-step debug notes; profile again after those.
**Impact:** **High** for the ladder: a step over 30 s loses outright, and the 40 ms average is reported.
**Blockers:** keep behavior identical: the order logs must match (see CLAUDE.md).

## `safe-build-location` (#18): Build at a safe base, not always the main

**Idea:** Zasz builds everything in its starting main. If the main is lost (or under attack), even when
the game is still recoverable, it keeps sending drones into the main to rebuild things like the spawning
pool, where they die. Seen in VeryHard games where the main fell.

**Cause:** placement is tied to `ai.start_location`: the pool (`bot/actions/macro.py`), tech buildings for
upgrades (`UpgradeController(..., base_location=ai.start_location)` in `bot/actions/upgrades.py`), and
`AutoSupply` (harmless for Zerg).

**Fix ideas:**
- One helper, e.g. `build_base(ai)`: our *ready* hatchery that's safest (no known enemies nearby, farthest
  from the known enemy army), preferring the main only when it's safe. Use it for the pool, tech and
  spores/spines.
- Don't send a builder drone through danger: skip or delay a build whose path crosses known enemy fire.
- If the pool died and every base is unsafe, wait (save the money) instead of rebuilding into the enemy.
- Check queens/injects and drone rallies don't keep walking into the lost main.

**Impact:** High in the games it happens. **Blockers:** ares' `BuildStructure` for Zerg places near
`base_location` towards the map center, so a non-main base needs a placement on that base's creep and
out of the mineral line; a scenario: kill our main's buildings, leave the natural, see where the pool goes.

## `creep-to-natural` (#19): Connect main and natural creep down the ramp

**Idea:** Make sure creep from the main runs down the ramp and joins the natural's, so queens (slow off
creep) move between the two bases quickly. Usually one tumor, but consistently missed: creep queens use
ares' `QueenSpreadCreep` (`bot/actions/queens.py`) toward the *enemy*, nothing aims at our own gap.

**Fix ideas:** before other spreading, walk the ground path main -> natural (ares `find_raw_path`), find
the first point without creep, and have a queen (the main's injector, or the first creep queen) place a
tumor at the last creep point before it; repeat until the path is on creep. Later: natural to third.
**Impact:** Medium, cheap. **Blockers:** tumors need creep and an unblocked spot (don't block the ramp). A
scenario can check every point on the path has creep after N minutes.

## `damage-efficiency` (#13): How much of the army's damage actually lands

**Idea:** `bot/efficiency.py` measures delivered / blocked / idle damage potential (logged at game end; the
`efficiency_*` scenarios measure it per layout). Open:

- **Ramp throughput** (~15% blocked): traffic on the ramp, not target choice. Ideas: hold the back of the
  army out of range instead of queueing; send only as many up as the top can use. Measure with
  `efficiency_ramp`.
- **Idle ~24% everywhere** is mostly the melee wind-up; a better "swinging" signal would make it meaningful.
- **Stutter step during cooldown**: not tried; the metric can decide.
- **Mutalisks** (#12): an overkill cap for ranged attackers in `attack_capacity`, and a scenario
  (mutalisks vs workers/structures) to measure bounce and spread.
- **Real games read lower than scenarios** (45% vs 60-70%): maybe real congestion in messy fights; worth a
  scenario with enemy *units* (zerglings vs a marine ball).

**Impact:** Medium. **Blockers:** real games are noisy (queens, regeneration, repair); compare in scenarios.

## `external-opponents` (#15): Play against other people's bots

**Idea:** Stop tuning only against the built-in AI. Two levels, cheapest first:
1. **Ladder:** upload Zasz to AI Arena and let it play the ranked ladder. The pieces exist
   (`scripts/create_ladder_zip.py`, `ladder.py`, `ladderbots.json`): check the zip runs there, set up an
   account and bot entry. Results come back as match history and replays.
2. **Local bot-vs-bot:** `--opponent <bot folder>` in `scripts/benchmark.py`. python-sc2's `BotProcess`
   launches any bot that speaks the ladder protocol; opponents from AI Arena (downloadable ones) or GitHub,
   kept in a gitignored `opponents/` folder, launched from their `ladderbots.json`.

**Impact:** **High.** Real bots punish different weaknesses (rushes, cannon/bunker rushes, mass air), the
ladder gives a rating over time, and the most realistic training data. **Blockers:** only downloadable
bots, respect licenses (never commit them); each bot brings its own runtime (give each its own venv, or
AI Arena's Docker setup); two SC2 instances per game (fewer `--jobs`); the ladder's map pool, Python 3.12,
`Debug: False`; check step time in bot-vs-bot games too.

## `run-robustness` (#23): Benchmark and scenario runner robustness

Covered by review R12 (phase 4); kept here for the replay-save detail.

- **Replay save crash:** a 20-minute tie makes a replay over python-sc2's 4 MB websocket limit (4.6-5.3 MB),
  so saving it fails and the game is logged as "Crash" though the bot was fine. Raise the client's
  message limit, or record the result before saving and treat a failed save as a warning.
- **Watchdog:** `run_scenario`'s timer calls `_thread.interrupt_main()`, which may not wake a main thread
  blocked on a socket; it has never fired. The parent waits on `future.result()` with no timeout, and
  `benchmark.run_matchups` and `seeds.probe` have no wall-clock limit. One worker dying fails the whole
  pool (every pending run recorded as ERROR). Proposed: a parent-side deadline that kills the worker's
  process tree, and one subprocess per game. Test with a fake worker that sleeps forever.

**Impact:** Low (tooling), but a hung run wastes a night.

## `trade-tracking` (#8): Ally vs enemy losses (resource trades)

**Idea:** Total ally vs enemy losses, to understand wasted resources, and as a model feature.

**Revision:** `state.score` already has killed and lost values by category (the benchmark records
`killed_value` and `lost_value`). Missing: *per-engagement* trades, by snapshotting the values when a squad
engages and disengages ("this fight cost X and killed Y"), for #11 and the model. Add as `Observation`
fields. **Impact:** Medium. **Blockers:** where an engagement starts and ends; enemy losses in the fog
aren't reported.

## `mining-rate` (#9): Mining rate per drone

**Idea:** Resources mined per drone, so a model can plan around income. `collection_rate_minerals/vespene`
is the game's income rate (used by `affordable_on_arrival`); per drone = rate / mining drones. Add the
"expected" rate from patch saturation (ares' `Mining` knows assignments), so the gap shows oversaturation,
long-distance mining or harass. **Impact:** Low (cheap model feature). **Blockers:** the game's rate lags.

## `army-value` (#10): Army value: ours vs the enemy's

**Idea:** Our army's value vs the enemy's known army, as an `Observation` feature for the attack decision
(ares' combat sim already does this per fight). **Impact:** Medium, more once there's more than one unit
type (#2). **Blockers:** **fog**: the enemy value undercounts what we haven't scouted; needs a "last seen"
age or scouting confidence. Changing ares' own advice means wrapping ares code (a pinned submodule).

## `supply-targeting` (#14): Target supply buildings when the enemy is near its supply cap

**Idea:** Estimate the enemy's supply used and cap; when the margin is under what one supply building gives,
put that building type ahead of production in the target priority (today fixed: `PRIORITY_STRUCTURES` in
`bot/micro.py`).
- **Cap:** sum known providers (max 200): depot / pylon / overlord 8; command center / orbital / planetary
  15, nexus 15, hatchery / lair / hive 6 (townhalls count, which makes them even better targets near the
  cap). ares' memory keeps structures seen in the fog.
- **Used:** supply of every known enemy unit plus an estimate for unseen workers.
- **Rule:** if `cap - used < 8`, add the provider type to the front (a capped number of zerglings).
  Strongest vs Protoss: a pylon also powers production; rank pylons by the production they power.

**Impact:** Medium. **Blockers:** fog makes both numbers uncertain (needs confidence); Zerg supply is
overlords (zerglings can't hit air); Terran depots in a wall also open the way in.

## `army-variety` (#2): More unit types + army composition logic

**Idea:** From zergling-only to a composition that answers what we've scouted (roach/ravager vs ground,
hydra/queen vs air, banelings vs light units). `SpawnController` takes a composition dict and
`Decision.army` carries it: add units to `bot/spaces.py` (`ARMY_UNITS`, `OWN_UNIT_TYPES`) and have `decide`
set `Decision.army` from the observation. The real work: tech path (lair, roach warren, hydra den), gas
timings, and army control that copes with ranged units (squads assume melee).
**Impact:** **High**: zerglings alone stall against Harder+ (ties, losses to tanks and air). **Blockers:**
`bot/micro.py` targeting is tuned for melee; `upgrades.py` only takes gas for upgrades. Split into stages
(mutalisks first). The harass *decision* is #12, their upgrades #3.

## `army-upgrading` (#3): Upgrades tied to army composition

**Idea:** Derive the upgrade wish list from the composition (melee / missile / flyer, plus unit upgrades).
`Decision.upgrades` already drives gas, tech and research, so this is mostly a mapping table once #2
exists (new upgrades go in `bot/spaces.py` `UPGRADES`). **Impact:** Medium. **Blockers:** #2; +3 needs hive.

## `decision-explanations` (#11): Explain decisions in chat

**Idea:** Say key decisions and their reasons in chat (attack because army value is higher; expand at 16
drones; skip an expansion because rocks block it; harass because ...) and their outcomes (lost 100, cost
the enemy 145 + mining). The visual version exists: `bot/overlay.py` (`--overlay`) draws squads, jobs,
raids, strikes, breaches, avoid areas and back-off lines; `Army.debug` logs the counts behind decisions.

**Revision:** a small `explain(event, reason, outcome)`: always to the log, a short chat line for key
decisions only (saved in the replay). Outcomes from #8's trades. **Impact:** Medium as a debugging tool.
**Blockers:** chat spam (rate-limit, opt-in); opponents read chat on the ladder; attributing outcomes.

## `mutalisk-harass` (#12): Mutalisk worker harass, decided by expected value

**Idea:** Harass when expected enemy loss > expected own loss. Enemy loss = workers killed x (50 + mining
lost while rebuilding) + supply/production lost; own loss = mutas expected to die vs known anti-air
(spores, turrets, cannons, army) within the time the enemy army needs to respond. ares' air grid and
`KeepUnitSafe` handle avoidance. **Impact:** Medium-High at Harder+. **Blockers:** needs a spire (#2/#3);
muta micro (clumping, magic box) is its own project; #10's fog problem.

## `replay-summary` (#1): Watch replays on Linux

**Idea:** The headless build can't render. Watch on Windows (benchmarks keep notable replays), or Wine.
More useful: `scripts/replay_summary.py` (sc2reader) printing build timings, army supply over time and
engagements, so most replays never need watching. **Impact:** Low directly, Medium as an enabler (#24).
**Blockers:** sc2reader gives events, not full game state.

## `static-defense-placement` (#6): Better spore / spine placement

**Idea:** Spores go between hatchery and minerals (`bot/actions/defense.py`). Place each where it covers
the most patches and the queens; spines on creep facing the likeliest attack path, built only when
scouting shows pressure. **Impact:** Low-Medium. **Blockers:** needs creep; spines aren't built at all yet.

## `drone-escape-building` (#7): Drone saves itself by starting a building

**Idea:** A drone about to die morphs into a building, cancelled later: cancelling returns the drone and
75% of the cost, so a save costs ~19-25 minerals vs 50 + lost mining for a new drone. Pick what helps if the
threat stays (spine vs ground, spore vs air; an extractor is cheapest when a geyser is in reach).
**Impact:** Low-Medium (1-3 drones per harass). **Blockers:** a morphing building starts with low HP, so
under fire it often dies too (drone *and* full cost): needs a "will it survive" estimate; spine/spore need
creep and a pool; the cancel needs tracking.
