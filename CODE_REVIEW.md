# Code inefficiencies and cleanup review

Reviewed on 2026-10-05. Base commit: `f26b304`, with uncommitted changes present.

This is a documentation-only review. No recommendations below have been implemented.
The review covers Zasz's `bot/`, `scenarios/`, `scripts/`, tests, entry points, packaging,
configuration, and documentation. The pinned `ares-sc2` submodule was inspected where
needed to verify integration behavior; this is not an exhaustive audit of that upstream
project or its compiled dependencies.

Findings describe the working tree, including the existing combat changes. Some source
files changed during inspection, so function names are the durable references; line
numbers are navigation hints from the review. Existing modified files were preserved:
`benchmarks/efficiency.csv`, `benchmarks/scenarios.csv`, `bot/army.py`, `bot/settings.py`,
`bot/targeting.py`, `bot/zasz.py`, `tests/test_efficiency.py`, and `tests/test_threats.py`.

## How to use this review

- **High:** incorrect reuse/state accounting, unreliable measurements, or substantial
  unnecessary work in frequently executed code. Address before relying on optimization results.
- **Medium:** useful reductions in work, duplication, or maintenance risk.
- **Low:** small cleanup or future-facing improvements.
- **Reproduced:** exercised with a small offline probe against the current code.
- **Confirmed:** visible in the implementation and its callers. Gameplay impact has not
  necessarily been reproduced in StarCraft II.
- **Profile first:** the repeated work is visible, but its runtime significance is unmeasured.

No speedup percentages are claimed. Existing caches, vectorized calculations, spatial
queries, and scheduling intervals already help; recommendations concern the remaining
work and the correctness of sharing it. In particular, do not indiscriminately remove
per-unit checks or extend cache lifetimes: positions, health, membership, and reservations change.

## Suggested order

| Order | Work | Findings |
|---|---|---|
| 1 | Fix cache ownership, slot accounting, and connectivity invariants | 03–07, 20 |
| 2 | Make measurement and run identity trustworthy | 19, 26–29, 33 |
| 3 | Stop unnecessary diagnostics and stale-state accumulation | 01–02, 23, 31 |
| 4 | Profile a long single-game run and reduce repeated expensive work | 08–15 |
| 5 | Consolidate production paths, scenarios, and data contracts | 16–18, 21–25, 30, 32, 34–44 |

This expands the existing `army-cpu` (#22), `combat-estimates` (#21), and
`damage-efficiency` (#13) items in [todo.md](todo.md), rather than replacing their scope.

## Combat, pathfinding, and runtime work

### 01. Debug work runs in normal games, and timing lists never drain — High / Confirmed

**Locations:** [bot/army.py](bot/army.py), `Army.control` (~373), `log_debug` (~492),
`route_map`, `route_ok`, `control_squad`, `fight`, `note`.

`control()` appends a timing sample on every call; route checks append more samples.
Only `log_debug()` clears these lists, but it returns immediately when `self.debug` is
false, the normal-game default. Normal games also build per-unit explanation strings,
target notes, safety diagnostics, and dynamic `COUNTS` keys even when neither debug
logging nor the overlay consumes them. Counters containing rounded costs/times can gain
new keys throughout a game.

**Opportunity:** gate diagnostic-only work on an explicit diagnostics setting, or use
bounded/aggregate statistics. Preserve `CHOKE_NOTES`' decision-cache role and scenario
consumers of `TARGET_NOTES`; those are not currently purely visual data. Verify identical
orders with diagnostics on/off and bounded memory over a long game.

**Verification (2026-10-05, commit `a781714`; not applied):** the finding is real, but "High" overstates
it; Low–Medium fits better (unbounded growth, small cost).

What holds up:

- `Army.debug` defaults to `False` (`Army.__init__`); only scenarios set it (`ScenarioBot.on_step`,
  and two efficiency scenarios). `run.py` and `ladder.py` never do, so ladder games run with it off.
- `control()` appends to `step_ms` every step, route checks append to `route_ms` (`route_map`,
  `route_ok`, `control_squad`), and only `log_debug()` clears them. `log_debug()` is called every
  step but returns at once when debug is off, so in a ladder game neither list ever drains.
- `COUNTS` is incremented unconditionally at 66 sites and cleared only in `log_debug()`. Keys with
  rounded numbers in them (`"breach considered (lose ~{lost:.0f})"`, `"bunker fight (lose ~{lost:.0f})"`,
  the squad-scout timings) keep adding keys over a game.
- In `fight()`, the `ai.mediator.is_position_safe` query only feeds the `UNSAFE {category}` counter
  and the `IN RANGE` text of the unit's note; nothing decides on it. Note strings and `name_of` text
  are built every step for every zergling.

Why it is overstated:

- `UNIT_NOTES` is cleared every step, and `last_note` is pruned on each death even with debug off
  (`on_death`), so neither grows.
- Measured cost is small. A `cProfile` of one `wall_bunker` game (~1,700 bot steps, debug on, so an
  upper bound) put the army's control at ~19 s, and the diagnostic pieces at under 1 s:
  `is_position_safe` 66,026 calls / 0.59 s (all callers, not only the diagnostic one), `name_of`
  6,518 / 0.07 s, `note` 8,317 / 0.02 s.
- Memory: at `GameStep: 2` a 20-minute game is ~13,400 bot steps, so `step_ms` holds ~13,400 floats
  and `route_ms` a few times that: a few hundred KB to ~1 MB. `COUNTS` is bounded by its distinct
  keys (hundreds to low thousands). Wasteful, not a crash or timeout risk; it is not what makes big
  fights exceed the step budget (#22).

Proposed fix (a few lines; orders should be identical with debug on or off):

1. With debug off, don't keep timing samples: skip the `step_ms` / `route_ms` appends, or clear both
   lists (and `COUNTS`) at the start of each step in `_control`.
2. In `fight()`, compute `unsafe` (the `is_position_safe` call) only when debug or the overlay is on;
   it feeds nothing else.
3. Optional: build note text only when debug or the overlay is on. Keep `TARGET_NOTES` and
   `CHOKE_NOTES` as they are (decision cache and scenario consumers).

Check: a full benchmark before and after (same seeds) to confirm identical results, plus memory
over a long game.

### 02. Expired caches and dead-unit/squad state lack systematic cleanup — High / Confirmed

**Locations:** [bot/army.py](bot/army.py), `Army.__init__`, `on_death`, `reach_map`,
`send_squad_scout`, `avoided`; [bot/targeting.py](bot/targeting.py),
`BlockTracker.forget_dead` (~282).

`reach_cache` retains old `ReachMap` objects for visited areas; each can retain sparse
graphs and multiple dense distance fields. Expiration causes replacement only when that
area is visited again. Other maps retain dead tags or obsolete entries, including
`last_damaged`, `enemy_state`, `enemy_moved`, `backed_off`, `breach_notes`, and
`avoid_reasons`. `BlockTracker` drops dead attackers but retains their expired target
entries while they live. `helps` is a growing scenario history.

More than memory is involved: `squad_scouts` is not pruned when a squad dies or merges,
and `send_squad_scout()` treats every stored plan as a claimed scouting destination.

**Opportunity:** explicit lifecycle cleanup for dead squads/units, TTL/LRU bounds for
heavy maps, and bounded optional histories. Preserve enemy memory that is still tactically
relevant. Test death/merge during scouting and memory retention across many fight areas.

### 03. Route cache shares a squad-dependent answer globally — High / Reproduced

**Location:** [bot/army.py](bot/army.py), `Army.route_map` (~836).

The danger zones depend on whether the supplied `units` beat each enemy group, but
`self.routes` is a single map reused for `PLAN_SECONDS`, irrespective of those units.
The first caller determines every later squad's danger policy. A strong force can therefore
produce a route that a weaker squad should avoid; the reverse needlessly blocks a strong force.

**Probe:** a strong force followed by a weak force at the same game time received the
same map object. The enemy's cell was open in that cached map and blocked in a fresh map
for the weak force.

**Opportunity:** share topology while keying danger policy by the relevant threat/force
signature, or define an explicitly common conservative policy. Invalidate when avoidance
changes. Test the same calls in both orders.

### 04. Failed safe routing falls through to ordinary movement — High / Confirmed

**Locations:** [bot/army.py](bot/army.py), `push_target` (~747), `squad_goal`,
`control_squad` (~1342).

`push_target()` returns the enemy main after rejecting every candidate, even if the main
failed its safety/reachability checks. In `control_squad()`, a `None` route waypoint leaves
the original target intact and movement continues through `move()`/`PathUnitToTarget`.
Patrol selection checks destination safety and ordinary connectivity but not a safe route
to the selected destination. These paths can undo the purpose of `RouteMap`.

**Opportunity:** represent “no safe destination/route” explicitly and choose a defined
hold/replan behavior. Check a map where the only route crosses a known tank position.

### 05. Reachability caches omit inputs that affect their answers — High / Confirmed; size issue reproduced

**Locations:** [bot/army.py](bot/army.py), `reach_map` (~1217);
[bot/targeting.py](bot/targeting.py), `open_sides` (~846), `plugs` (~558),
`set_danger` (~595), `TargetSlots.capacity` (~168).

- `Army.reach_map()` keys on a rounded squad center and age, although construction uses
  the actual friendly starting cells and enemy obstacles. Different squads in the same
  coarse cell can reuse a map reached from the other side of a wall.
- `open_sides(target, attacker_radius)` caches only by target tag. A probe returned `8`
  for both radii 0.375 and 1.0 after the smaller query; a fresh map returned `28` for 1.0.
  Mixed sizes are currently a future-facing concern because the controlled army is zerglings.
- `plugs(units, targets)` caches one set regardless of the supplied sets.
- `set_danger()` accepts the first caller's enemy zones for a timestamp, even when a
  later squad sharing the map supplies different enemies. Its radii also depend on the attacker.
- `TargetSlots.settled` includes attacker radius but not range, although range affects spots.

**Opportunity:** document the exact scope of each cache and key or invalidate on all
relevant inputs. Prefer a deliberate shared fight snapshot over accidental first-caller
ownership. Add caller-order and two-sides-of-a-wall cases before widening cache reuse.

### 06. Connectivity labels disagree with the actual movement graph — High / Reproduced

**Locations:** [bot/targeting.py](bot/targeting.py), `_squeezed` (~574),
`_build_graph` (~707), `set_danger`, `breaches` (~852);
[bot/army.py](bot/army.py), `widen_plan` (~1650).

Reach searches and graphs reject a diagonal squeezed between blocked cells when an
enemy unit blocks a corner. `scipy.ndimage.label(..., structure=np.ones((3, 3)))` joins
all diagonal neighbors without that rule. Safe-area and breach/widening classification
can consequently describe cells as connected even though pathfinding cannot connect them.

**Probe:** two diagonal open cells separated by one enemy-blocked orthogonal corner
had `reached=False` across the corner but the same nonzero safe-area label.

**Opportunity:** use the same adjacency policy for labels, reach searches, and paths.
Retain the existing regression that allows corner gaps between buildings.

### 07. Slot accounting can release or consume the wrong capacity — High / Reproduced

**Locations:** [bot/targeting.py](bot/targeting.py), `Assignments.start/release`
(~111), `TargetSlots.take`, `pick_tiered` (~287);
[bot/army.py](bot/army.py), `fight` (~1973).

Two separate accounting problems:

- `start()` reserves only recent attackers, but `release()` subtracts any attacker's
  remembered share from the target aggregate. A stale attacker sharing a target with a
  recent attacker releases capacity belonging to the recent one. Probe: reserved share
  `0.5` became `0.0` after releasing an attacker that had contributed nothing this step.
- `pick_tiered()` immediately consumes a slot. When `fight()` discovers the chosen target
  is blocked, it picks again or recursively follows a new detour without undoing that
  consumption. A probe picking target 2 then repicking target 3 for the same attacker left
  `taken == {2: 1, 3: 1}`. A detour can also count the same attacker twice on one target.

**Opportunity:** track reservations and committed assignments per attacker; release only
what that attacker owns, and make repicking replace a claim atomically. Verify counts,
angular shares, and assignments agree after repicks and detours.

### 08. Enemy grouping and fight context are rebuilt by many callers — High / Profile first

**Locations:** [bot/squads.py](bot/squads.py), `enemy_groups` (~94), `cluster`;
[bot/army.py](bot/army.py), `squad_goal`, `assign_raids`, `route_map`,
`dangerous_for`, `avoid_armies_we_cannot_beat`.

All these callers build enemy groups independently. `dangerous_for()` can repeat it for
each candidate destination. Grouping scans members of existing groups and is quadratic
in the worst case. Callers then repeat filtering and simulation on the resulting groups.

**Opportunity:** build one enemy/group snapshot per game step and share it. Use the
existing mediator's spatial queries where appropriate. Cache raw group information
separately from squad-specific win judgments. Measure group builds and sim calls per step.

### 09. Sizing loops repeat identical combat simulations — High / Confirmed; profile impact

**Locations:** [bot/squads.py](bot/squads.py), `needed` (~124), `choose_helpers`
(~348); [bot/army.py](bot/army.py), `strike_size` (~1163), `assign_raids`;
[ares combat manager](ares-sc2/src/ares/managers/combat_sim_manager.py), `can_win_fight`.

The comfortable-win and minimum-win passes evaluate the same forces again even though
the simulator returns an enum that can be compared to both thresholds. `choose_helpers()`
continues testing identical effective forces after `room` is filled. `strike_size()` can
test the full even-sized pool in its loop and immediately afterward. The inspected ares
manager invokes its simulator on each request; it does not memoize these calls.

**Opportunity:** compute each distinct force's verdict once per evaluation, reuse it for
both thresholds, and stop adding simulations when effective participants no longer change.
Do not assume binary-search monotonicity for this approximate simulator without checking it.

### 10. Danger updates discard expensive derived work every step — High / Profile first

**Location:** [bot/targeting.py](bot/targeting.py), `ReachMap.set_danger` (~595),
`safe_step` (~678), `breach_step` (~646).

Each new timestamp regenerates coordinate grids, rasterizes zones, relabels safe areas,
and clears safe/breach fields even if the zone geometry is unchanged. A breach field also
builds a weighted sparse graph for each distinct goal cell. Stationary enemies and many
attack spots are a particularly repetitive workload.

**Opportunity:** retain coordinate arrays per map, compare danger geometry/version before
invalidating, and share graph structure or appropriate goal fields. Keep per-step mutable
slot/busy data separate. Profile field builds in the stationary gateway and bunker scenarios.

### 11. Graph construction and distance-field traversal are duplicated — Medium / Confirmed

**Location:** [bot/targeting.py](bot/targeting.py), `_build_graph` (~707),
`_open_graph` (~986), `safe_step`, `breach_step`, `approach_step`, `group_field`,
`walk_to`, `walk_distance`, `RouteMap.distance/reaches/waypoint`.

Graph index generation, neighbor slices, weights, Dijkstra setup, coordinate conversion,
and downhill traversal recur with small differences. The safe and breach downhill loops
are essentially identical. This makes topology fixes easy to apply inconsistently.

**Opportunity:** extract small graph/field primitives with explicit corner, cost, and
blocked-start policies. Preserve the intentional differences between local reachability
and global danger routing. Share labels/coordinate arrays when their semantics match.

### 12. Per-unit targeting repeats work shared by the fight — High / Profile first

**Locations:** [bot/army.py](bot/army.py), `fight`, `safe_work` (~2392),
`covered_work` (~2362); [bot/targeting.py](bot/targeting.py), `pick_tiered`.

Every attacker rebuilds fighter/worker/structure lists and danger-zone arguments, even
when `set_danger()` immediately returns. `safe_work()` repeatedly filters the same target
spots by safe region and danger; `pick_tiered()` revisits candidate lists for best tier,
current target, ordering, room, and fallback. Some nearby scans traverse every friendly
unit. `covered_work()` already demonstrates a useful per-map step cache.

**Opportunity:** share immutable classifications and safe-spot results keyed by region
and attacker geometry. Keep availability checks live because earlier attackers consume
slots. Count per-unit full scans before adding more persistent caches.

### 13. Patrol visibility and squad centers are recomputed more than necessary — Medium / Profile first

**Locations:** [bot/squads.py](bot/squads.py), `Patrol.update` (~286),
`Squad.squad_position` (~183), `SquadBook.update/_merge`;
[bot/army.py](bot/army.py), `assign_raids`, `control_squad`;
[bot/overlay.py](bot/overlay.py), `draw` (~65).

Patrol visibility tests every spot against potentially every own unit every army step.
Each `squad_position` access sums the squad again; it appears inside sorting/filtering and
per-unit fight calls. Raid staging recomputes `center_of(site.defenders)` inside the
maximum-distance generator. Overlay drawing separately rescans all units for each squad.

**Opportunity:** compute centers once for each stable membership snapshot, use a tag index
for overlay membership, and profile a slower or spatially indexed patrol update. Recompute
centers after strike units are removed; a cache made before that mutation is not equivalent.

### 14. Observation and actions duplicate economy queries — Medium / Confirmed

**Locations:** [bot/observation.py](bot/observation.py), `observe` (~114);
[bot/actions/macro.py](bot/actions/macro.py), `macro_plan`;
[bot/actions/upgrades.py](bot/actions/upgrades.py), `remaining_gas`;
[bot/zasz.py](bot/zasz.py), `on_step`.

Observation searches for the next expansion every step; an expansion decision searches
again in `macro_plan()`. Path reachability has a cache, but placement/safety iteration is
still repeated. `workers_per_gas()` and `extractors_wanted()` independently calculate
remaining upgrade gas. Observation separately scans units/structures for each fixed type
and reconstructs field metadata/zero-filled dictionaries.

**Opportunity:** share step-local economy facts and the chosen expansion internally while
keeping the policy interface purely observational. Reuse count dictionaries or ares' type
indexes. Preserve resource changes and safety checks that occur between observation and execution.

### 15. Expansion blockage checks rebuild the same grids per destination — Medium / Profile first

**Location:** [bot/actions/pathing.py](bot/actions/pathing.py),
`ExpansionAccess._find_blockage` (~86).

When blockers change or cached checks expire, each destination can construct a fresh
normal grid, another without rocks, and a terrain-only grid. Several blocked expansion
candidates therefore repeat map-wide grid construction during one search. The first fresh
grid is intentional because ares' grid can lag structure changes.

**Opportunity:** share the three grid variants within one blocker snapshot/check batch,
and consider connectivity labeling for the initial yes/no checks. Retain actual paths
for explaining which rocks or structures block a route. Do not replace fresh geometry
with the stale mediator grid merely to reduce calls.

### 16. Production target selection and the tested selector have diverged — High / Confirmed

**Locations:** [bot/targeting.py](bot/targeting.py), `TargetSlots.pick` (~209),
`pick_tiered` (~287); [tests/test_efficiency.py](tests/test_efficiency.py), targeting tests.

`TargetSlots.pick()` duplicates nearest/deepest/current-target selection, but its only
repository callers are tests. Production `fight()` uses `pick_tiered()`, whose reservation
and assignment behavior differs. Several reassuring target-selection tests therefore
exercise a parallel implementation rather than the live path.

**Opportunity:** consolidate a small common selector or retire the unused path, and move
behavior assertions to `pick_tiered()` plus its `fight()` integration. Include assignments
across steps, blocked repicks, and detours; do not merely duplicate tests for both algorithms.

### 17. Remove or explicitly justify leftover fields, parameters, and helpers — Low / Confirmed

**Locations:** [bot/settings.py](bot/settings.py), `MIN_OPEN_SIDES` (~129);
[bot/targeting.py](bot/targeting.py), `ReachMap.breach_caps` (~478),
`walk_distance` (~824); [bot/army.py](bot/army.py), `breach_caps` (~1542);
[scenarios/efficiency.py](scenarios/efficiency.py), `keep_alive_spot` (~150).

- `MIN_OPEN_SIDES` has no loaded reference in first-party code/tests.
- `ReachMap.breach_caps` is initialized but never read or assigned by callers; the live
  cache is `Army.breach_evals`.
- `ReachMap.walk_distance()` and `_near_fields` have test callers but no production callers.
- `Army.breach_caps()` accepts `units`, `threats`, and `present` without using them.
- `keep_alive_spot()` accepts `layout` without using it.
- `TargetSlots.pick()` is the larger test-only path described in finding 16.

**Opportunity:** remove obsolete surfaces or label intentionally retained experimental APIs.
This is call-site evidence, not a claim that externally invoked hooks or every unreferenced
symbol are safe to delete.

### 18. Avoid-area maintenance is duplicated and hidden in queries — Medium / Confirmed

**Location:** [bot/army.py](bot/army.py), `turned_back` (~1230),
`mark_avoid` (~1299), `avoided` (~1334), `blocked`.

`turned_back()` calculates geometry then duplicates `mark_avoid()`'s replacement, append,
counter, and reason bookkeeping. Every `blocked()` query calls `avoided()`, which filters
and replaces the stored list and allocates a second list. Candidate loops repeatedly do
the same expiration work, and apparently read-only queries mutate shared state.

**Opportunity:** one insertion function and one expiration pass per step, followed by
read-only queries against the prepared zones. Prune matching reason entries at the same time.

### 19. Damage efficiency loses the target during the bot's own move orders — High / Confirmed

**Locations:** [bot/efficiency.py](bot/efficiency.py), `DamageTracker.update` (~120);
[bot/army.py](bot/army.py), detours, `work_from_cell`, `work_spot`, approach movement;
[bot/targeting.py](bot/targeting.py), `Assignments`.

The metric recognizes commitment only when `unit.order_target` is an integer enemy tag.
The targeting system now intentionally keeps a target while issuing point-based move
orders for approach/detours/safe spots. During those orders the tracker deletes the
engagement, even though the attacker still has an assignment. Repeated attack/move cycles
can repeatedly restart the one-second commitment window and omit real stalls.

**Opportunity:** reconcile the metric with recent valid assignments and actual orders,
while still excluding deliberate travel and retreat. Test attack → detour → blocked →
attack transitions. Keep raw damage/kill-time measurements beside the efficiency ratio.
Also make `reset()` semantics explicit: it resets totals/window but retains engagements,
enemy health, and `last_time`, so a reset is not a fresh measurement state.

### 20. Clustering depends on iteration order and never merges bridged groups — High / Reproduced

**Locations:** [bot/squads.py](bot/squads.py), `cluster` (~82);
[bot/army.py](bot/army.py), `unseen_circles` (~348), similar first-match grouping.

A new unit joins the first cluster it touches, without merging other clusters it also
touches. At x positions `[0, 4, 2]` with radius 3, the result is `[[0, 2], [4]]`, although
the unit at 2 links both ends. Different input order produces one group. That affects
combat sizing, sites, danger estimates, and strike allocation.

**Opportunity:** define whether groups mean connected components or another intentional
clustering rule. For connected components, merge bridged groups, preferably using spatial
neighbors. Add input-permutation tests and preserve deterministic output ordering.

### 21. Combat estimates have several inconsistent entry points — Medium / Confirmed

**Locations:** [bot/squads.py](bot/squads.py), `wins`;
[bot/army.py](bot/army.py), `fight_result`, `strike_result`, `through_bunkers`,
`evaluate_breach`, `widen_plan`, `choke_worth`.

Raid/help sizing calls the raw simulator via `wins()`. Squad fighting accounts for
neutralized tanks and bunkers; strike sizing knows bunkers but handles tanks differently.
Breach, widening, and choke judgments separately implement “time to kill × covering DPS”
with related geometry and survivor calculations. Threshold differences can be intentional,
but the mechanical assumptions are distributed and hard to compare.

**Opportunity:** share raw estimates and threat/coverage calculations behind explicit
policies. Keep engage/leave thresholds separate from mechanics. Validate bunker cargo,
tank minimum range, current health, and choke geometry before changing strategic thresholds.

### 22. Army state and control flow are too concentrated — Medium / Confirmed

**Locations:** [bot/army.py](bot/army.py), `Army`, `fight`;
[bot/squads.py](bot/squads.py), `Squad`, `Strike`, `Rally`.

At review time `army.py` exceeded 2,600 lines; `fight()` was 282 lines, `control_squad()`
162, and `update_strikes()` 136. Scheduling, scouting, spatial caches, simulations,
target selection, orders, and diagnostics share one mutable object. Squad state changes
through repeated tuple assignments, with string job names and positional tuple plans
(`squad_scouts`, widening, breach evaluations). `Strike.reach` is typed as `object`.

**Opportunity:** extract responsibilities incrementally around existing data flow:
fight context, route/reach cache ownership, squad transitions, and diagnostics. Use small
named state records and typed job values. Preserve ordering: planning, reservations,
strikes, and per-unit execution have observable dependencies.

### 23. Module-global diagnostics also hold behavioral state — Medium / Confirmed

**Locations:** [bot/army.py](bot/army.py), `COUNTS`, `CHOKE_NOTES`,
`TARGET_NOTES`, `UNIT_NOTES`, `REACH_DUMPED`; [bot/overlay.py](bot/overlay.py), `draw`;
[scenarios/army.py](scenarios/army.py), imported notes.

All `Army` instances share these objects; `self.counts = COUNTS` is an alias. Sequential
games in one process can inherit counts/dump suppression, and one instance's clearing
can affect another. `CHOKE_NOTES` is also used to decide whether to fight, not only to draw.
This coupling makes disabling diagnostics and testing multiple bots unsafe to do casually.

**Opportunity:** move per-game/per-step state under the owning bot and pass an explicit
diagnostic view to the overlay/scenarios. Keep decision caches separate from presentation.

### 24. Worker absence and failed placements need explicit fallbacks — Medium / Confirmed

**Locations:** [bot/actions/building.py](bot/actions/building.py),
`affordable_on_arrival`; [bot/actions/defense.py](bot/actions/defense.py), `build_spores`.

If minerals are below the structure cost, `affordable_on_arrival()` calls
`ai.workers.closest_to()` without checking that any workers remain. A surviving hatchery
with no drones can reach this path through a build decision. In `build_spores()`, the
first base with a desired spot causes a return even if placement or worker selection
fails, preventing later bases from being tried in that call and potentially on every step.

**Opportunity:** define no-worker behavior and distinguish “build issued” from “candidate
failed.” Retry other eligible bases and throttle repeated unsuccessful placement requests.
Use offline edge-case tests before SC2 recovery scenarios.

### 25. Injector assignment can churn and repeats every step — Medium / Profile first

**Location:** [bot/actions/queens.py](bot/actions/queens.py), `pair_injectors`, `control_queens`.

Every step greedily assigns each ready hatchery its closest remaining queen from scratch.
Small movement during defense can swap assignments and turn previous injectors into
creep queens. It also repeats distance work even when bases and roles have not changed.

**Opportunity:** keep valid queen-to-hatchery assignments until death, a base/role change,
or an explicit reassignment condition. Verify recovery after defense and queen loss;
measure role changes as well as CPU before treating this as a performance win.

## Benchmarks, scenarios, and recordings

### 26. Commit identification is duplicated and results do not identify the executed code reliably — High / Confirmed

**Locations:** [scripts/benchmark.py](scripts/benchmark.py), `current_commit` (~168);
[scenarios/efficiency.py](scenarios/efficiency.py), `current_commit` (~34);
[scripts/run_scenarios.py](scripts/run_scenarios.py), `current_commit` (~145).

Benchmark and efficiency functions are exact duplicates that manually read `.git/HEAD`
and loose ref files. They miss packed refs/worktrees and do not mark dirty source.
The scenario runner uses Git properly, but its dirty check includes tracked benchmark
CSVs. Writing results alone can make the next run “dirty,” while different source changes
under the same commit all collapse to the same `-dirty` identifier. `compare()` then
excludes previous runs with that identical identifier.

**Opportunity:** one run-metadata helper recording commit, relevant source/config diff
fingerprint, dependency revision, and run ID. Exclude generated results from the code
fingerprint and determine metadata once in the parent process.

### 27. Efficiency results are written concurrently and lack comparison keys — High / Confirmed

**Locations:** [scenarios/efficiency.py](scenarios/efficiency.py), `FIELDS`,
`StructureAssault.report` (~113); [scripts/run_scenarios.py](scripts/run_scenarios.py),
worker pool and parent result handling.

Each worker independently checks whether `efficiency.csv` exists and appends to it.
Fresh parallel runs can race to write the header; writes have no cross-process
coordination. Unlike `scenarios.csv`, efficiency rows omit seed, side, SC2 build, explicit
status, and run ID. The side exists only in the message, and undefined efficiency is
stored as zero (`t.efficiency or 0`), conflating “not measured” with 0% efficiency.

**Opportunity:** return efficiency metrics in `Report` and let the parent serialize all
results. Include stable join/comparison keys and preserve missing values. Test concurrent
results with a fresh file and compare the same seeded scenario across revisions.

### 28. Benchmark pairing fails to cover the map/race cross product — High / Reproduced by construction

**Location:** [scripts/benchmark.py](scripts/benchmark.py), `main` (~326), matchup creation.

Zipping a cycle of maps with a cycle of races gives only `lcm(map_count, race_count)`
distinct pairs, rather than their cross product. With three maps and three races, even
nine games repeat the same three pairings three times; six combinations never run.
Shuffling maps changes which pairings are missed but does not solve coverage.

**Opportunity:** cycle a balanced product of map/race pairs, with a deterministic shuffle
or explicit schedule. Test the planned matchups without launching games.

### 29. Benchmarks are not reproducible in the same way scenarios are — Medium / Confirmed

**Locations:** [scripts/benchmark.py](scripts/benchmark.py), `main`, `play_game`;
[scripts/run_scenarios.py](scripts/run_scenarios.py), `make_repeatable`.

Benchmark map shuffling is unseeded, `run_game()` gets no game seed, and rows do not record
one. The scenario runner seeds SC2/Python/NumPy and UUID generation in isolated workers,
but that machinery is not shared by benchmarks. Before/after benchmark differences
therefore mix behavior changes with start/map/randomness changes.

**Opportunity:** add a run seed and per-game seeds to benchmark planning and metadata;
retain an explicitly random mode for broader sampling. Compare paired runs with the same
schedule before drawing performance or win-rate conclusions.

### 30. CSV history handling repeatedly scans or rewrites accumulated data — Medium / Confirmed

**Locations:** [scripts/run_scenarios.py](scripts/run_scenarios.py), `load_results`,
`compare` (~175); [scripts/benchmark.py](scripts/benchmark.py), `open_results` (~249).

Every scenario result constructs a complete list of matching historical rows and takes
the last one, repeating a history scan per result. Benchmark history is fully loaded to
derive win-time summaries. A changed benchmark header rewrites the original file in place
and silently drops extra columns, so interruption can damage the historical baseline.

**Opportunity:** index the needed recent baselines in one pass; stream summary inputs where
possible. Make schema migrations explicit and atomic, preserving removed columns or an
original copy. This matters as the checked-in history grows, not for a tiny initial CSV.

### 31. Full logs and timing samples are retained longer than needed — Medium / Confirmed

**Locations:** [scripts/benchmark.py](scripts/benchmark.py), `BenchmarkZasz.step_times`,
`play_game`, `run_matchups`; [scripts/run_scenarios.py](scripts/run_scenarios.py),
`run_scenario`, `reports`, `futures`.

Workers buffer complete logs in `StringIO`; ordered result collection leaves later
completed logs waiting behind earlier games. Futures remain in the list/dictionary after
consumption, and scenario `Report` objects retain their log strings through the whole run.
All tasks are submitted up front. Benchmark timing keeps every sample although final
reporting only needs count, sum, and maximum.

**Opportunity:** aggregate timings, spool large logs to per-run files, keep a bounded
submission window, and drop consumed futures/log payloads. Preserve matchup-order reporting
as a deliberate user-facing behavior. Avoid changing the fresh-process isolation just to
save startup overhead; python-sc2 process state is the stated reason it exists.

### 32. Watchdog coverage does not guarantee a stalled run can finish — Medium / Confirmed gap; failure mode needs exercising

**Locations:** [scripts/run_scenarios.py](scripts/run_scenarios.py), worker timer and
`future.result()`; [scripts/benchmark.py](scripts/benchmark.py), `run_matchups`;
[scenarios/seeds.py](scenarios/seeds.py), `probe`, `_probe_side`.

Scenario protection is a thread requesting an interrupt in the same worker. The parent
waits on `future.result()` without a deadline. This is not a hard timeout if the worker
cannot process the interrupt or cleanup hangs. Benchmarks and seed probes lack comparable
wall-clock protection; a game-time limit does not resolve a frozen SC2 connection.

**Opportunity:** give the parent explicit worker deadlines and cleanup ownership, shared
across all game-launching tools. Test a deliberately nonresponsive worker and subprocess
cleanup. Distinguish user cancellation from watchdog failure instead of turning both
`KeyboardInterrupt` cases into a scenario error.

### 33. Invalid/failed benchmark runs can be misleadingly successful — Medium / Confirmed

**Locations:** [scripts/benchmark.py](scripts/benchmark.py), `main`, `print_summary`;
[scripts/run_scenarios.py](scripts/run_scenarios.py), argument handling.

The benchmark exits nonzero only for timing limits, so `Crash` rows alone can still yield
exit code 0. Zero games or no installed maps leads to an empty result set and division by
zero in `win_rate()`. Race arguments have no choices validation. Negative/zero run counts
are not consistently rejected across tools; zero scenario repeats can report `0/0 passed`.

**Opportunity:** validate counts, map availability, and races before any launches, and
define an explicit failure policy for crashes versus ordinary losses. Test CLI planning
and exit codes using synthetic rows, without running SC2.

### 34. Run identity and scenario keys can collide — Medium / Confirmed

**Locations:** [scripts/benchmark.py](scripts/benchmark.py), run directory naming;
[scripts/run_scenarios.py](scripts/run_scenarios.py), `runs`, `replays`, `futures`.

Run directories use timestamps with one-second precision. Two invocations in the same
second collide at `mkdir()`. Repeated positional scenario names create identical `run`
tuples, so the futures dictionary overwrites an earlier entry even though its task was
already submitted. Those tasks also share a replay filename.

**Opportunity:** use a unique run ID and per-execution sequence ID; either reject duplicate
names or support them explicitly. Keep display labels separate from execution identity.

### 35. Importing the scenario registry creates leaked temporary directories — Medium / Confirmed

**Locations:** [scenarios/decisions.py](scenarios/decisions.py),
`CustomDecisions.__init__`, module-level `SCENARIOS`;
[scenarios/__init__.py](scenarios/__init__.py), `ALL`;
[scripts/run_scenarios.py](scripts/run_scenarios.py), registry lookup.

`SCENARIOS = [CustomDecisions()]` calls `tempfile.mkdtemp()` at import. Every freshly
spawned worker imports the registry, even for unrelated scenarios; `--list` imports it too.
There is no matching cleanup. Scenario objects also hold mutable phase state in a global
registry, so direct repeated execution in one process would reuse that state.

**Opportunity:** register factories/classes and construct only the selected scenario.
Create managed temporary storage at setup and close/remove it in teardown. Verify listing
and unrelated scenarios create no recording directories.

### 36. Scenario setup duplicates phase machines and issues one debug request per spawn — Medium / Confirmed

**Locations:** [scenarios/efficiency.py](scenarios/efficiency.py),
`StructureAssault.on_step`, `CrampedMain.on_step`, `OpenArmy.on_step`;
[scenarios/walls.py](scenarios/walls.py), `WalledMarines.on_step`;
[scenarios/__init__.py](scenarios/__init__.py), `ScenarioBot.spawn`.

The same spawn → clear starting units → spawn attackers → reset metrics → fight sequence
is copied across scenarios. Layout-search loops recur and layouts are sometimes recomputed
after spawning. Each `await bot.spawn()` sends a separate debug-create request even though
the underlying call accepts a list of spawn specifications.

**Opportunity:** share a small staged-assault helper and batch independent spawns within
each phase. Compute/store intended layouts once. Preserve intentional next-step barriers,
tech-lab retries, and game keep-alive ordering; parallelizing every await would break these.
Replace the broad `TypeError` suppression around the cramped-main reach diagnostic with
an explicit compatibility decision or remove it when old-code comparisons are retired.

### 37. Recorder cleanup depends on the normal game-ending callback — Medium / Confirmed

**Locations:** [bot/recording.py](bot/recording.py), `DecisionRecorder.close`, `_write`;
[scripts/benchmark.py](scripts/benchmark.py), `play_game`;
[bot/zasz.py](bot/zasz.py), `on_end`.

The benchmark catches game failures but only removes the log sink in `finally`; it does
not close/flush the recorder if `on_end()` was skipped. Buffered tail records can be lost
on abnormal worker termination. A recorder can also be used after `close()`: `_write()`
reopens the path with `"w"`, truncating the previous recording.

**Opportunity:** explicit recorder ownership and a terminal closed state, with cleanup
in the runner's `finally`. Add a public flush operation instead of the scenario reaching
into `recorder._file`. Decide whether abnormal closure writes a marked end record or
leaves a clearly unfinished stream.

### 38. Recording schema metadata is present but not enforced — Medium / Confirmed

**Locations:** [bot/recording.py](bot/recording.py), `_write`, `read_records` (~69);
[bot/observation.py](bot/observation.py), `from_dict`;
[bot/decisions/__init__.py](bot/decisions/__init__.py), `from_dict`, `to_vector`.

`read_records()` ignores header version and declared feature/decision layouts. Observation
loading defaults missing features, but decision loading requires every field. Caller
metadata can overwrite reserved header keys through `header | self.meta`. A malformed or
truncated JSON line has no contextual/recovery handling. Reading always materializes all
steps, which becomes expensive for larger training datasets.

**Opportunity:** validate supported schemas, reserve structural header keys, define
decision migrations/defaults, and offer a streaming iterator with filename/line errors.
Make truncated-tail recovery an explicit option, not silent acceptance of arbitrary corruption.

### 39. Frozen decision/observation objects still contain mutable, partly unchecked data — Medium / Confirmed

**Locations:** [bot/decisions/__init__.py](bot/decisions/__init__.py), `Decision`;
[bot/observation.py](bot/observation.py), `Observation`;
[bot/recording.py](bot/recording.py), `_last_decision`;
[bot/actions/macro.py](bot/actions/macro.py), `spawn_composition`.

Frozen dataclasses prevent field reassignment but not dictionary mutation. Reusing and
mutating one `Decision.army` mapping can mutate the recorder's saved comparison object,
causing a change to be missed. Decisions do not reject unknown army keys, invalid shares,
or duplicate/out-of-space upgrades; serialization/vector conversion can then disagree
about what the decision contains. Action priority follows dict insertion order, whereas
the vector follows `ARMY_UNITS`, a future issue when more unit types are added.

**Opportunity:** validate at the policy boundary, snapshot or freeze mappings, and encode
priority explicitly if it is part of the action space. Use one schema description for
vector names and values. Avoid repeatedly searching the upgrade tuple when building vectors.

### 40. The learning/replay interface omits policy history — Medium / Confirmed

**Locations:** [bot/decisions/scripted.py](bot/decisions/scripted.py),
`ScriptedDecisions.attacking`, `should_attack`;
[bot/observation.py](bot/observation.py), `Observation`;
[README.md](README.md), decisions section.

The README calls decisions a pure function of observation, but attack hysteresis uses
`self.attacking`. Identical observations between the start/stop thresholds can produce
different decisions depending on previous steps. The observation has no previous attack
state. Independent training samples or replayed individual observations cannot reproduce
that behavior from the current feature vector alone.

**Opportunity:** document the stateful contract and either expose previous policy state
or train/replay sequences with explicit resets. Keep hysteresis; removing it simply to
match the documentation would alter strategy.

## Structure, packaging, and maintenance

### 41. Bootstrapping and imports duplicate integration setup — Low / Confirmed

**Locations:** [run.py](run.py), top-level bot construction;
[scripts/benchmark.py](scripts/benchmark.py), [scripts/run_scenarios.py](scripts/run_scenarios.py),
[tests/conftest.py](tests/conftest.py), [scenarios/seeds.py](scenarios/seeds.py), path setup;
[bot/decisions/__init__.py](bot/decisions/__init__.py), circular re-export;
[scenarios/__init__.py](scenarios/__init__.py), registry imports.

Several entry points repeat knowledge of the ares source/helper layout. `run.py` constructs
a bot even when imported or invoked just for help. Package initializers import implementations
that import definitions back from those initializers, requiring special import ordering.
First-party modules also import libraries available only transitively through ares.

**Opportunity:** centralize the small bootstrap contract or package the entry points,
instantiate bots inside execution functions, and move shared contracts out of registry
initializers. Declare/document direct dependencies where Zasz uses their APIs. Keep ladder
archive import paths working; an editable local install alone does not solve deployment.

### 42. Ladder archives include avoidable binaries and fragile download selection — Medium / Confirmed

**Location:** [scripts/create_ladder_zip.py](scripts/create_ladder_zip.py),
`add_tree` (~31), `add_linux_cython_extensions` (~41), `main` (~54).

Filtering removes Windows/macOS binaries but still includes every Linux `sc2_helper`
Python ABI in the tree (3.10–3.13) for a 3.12 target. Wheel selection matches a
`cp312-cp312-manylinux` substring without validating architecture. Every build redownloads
the wheel with no explicit timeout or local cache, and writes the final archive before
the network stage succeeds. Failure can replace a previous usable artifact with a partial zip.
The debug precondition uses `assert`, which is skipped when Python runs with optimization.

**Opportunity:** select explicit ABI/platform/architecture tags, include only target
binaries, cache and verify the downloaded artifact, use timeouts, and build to a temporary
archive before atomically replacing the final file. Use an ordinary validation error for
the debug check. Test archive contents without uploading or launching a ladder game.

### 43. Documentation and experiment inventory lag the live implementation — Low / Confirmed

**Locations:** [README.md](README.md), [docs/progress.md](docs/progress.md),
[bot/army.py](bot/army.py), module docstring;
[bot/decisions/scripted.py](bot/decisions/scripted.py), pool comment;
[scripts/benchmark.py](scripts/benchmark.py), module docstring;
[experiments/](experiments/).

Examples of concrete drift:

- README layout lists `bot/army_plan.py`, which no longer exists, and strategy prose
  still describes the old single-army/scout arrangement.
- The army docstring says ares groups these squads; the live path uses `SquadBook`.
- `docs/progress.md`'s explicitly dated current-state section predates later squad work
  and refers to `STRUCTURE_FILL`, now `TARGET_FILL`.
- The pool comment says saving stays true while ordered, while `pool_wanted()` returns
  false when `pool_ordered` is true.
- Benchmark documentation calls its CSV gitignored, although it is tracked.
- The archive script's usage names an old `Zasz-ML` folder.

**Opportunity:** retain historical notes as history but provide one maintained current
architecture/status description. Add an index identifying each experimental patch's base
revision, purpose, and whether it is superseded before deleting or applying any patch.

### 44. Tests cover useful primitives but leave the riskiest orchestration untested offline — Medium / Confirmed

**Locations:** [tests/](tests/), especially `test_efficiency.py`, `test_squads.py`,
`test_macro.py`; [bot/army.py](bot/army.py); [scripts/](scripts/).

The existing suite covers many geometry and policy primitives, but has no dedicated
offline coverage of route-cache ownership, dead-scout cleanup, the complete assignment
repick flow, or benchmark/runner lifecycle. `test_macro.py` tests composition conversion,
not spending orchestration. SimpleNamespace unit factories recur with different supported
attributes, making integration assumptions easy to miss. Several targeting assertions
exercise the unused selector discussed in finding 16.

**Opportunity:** prioritize behavioral regressions for the reproduced findings, then
small fake-runner tests for run planning, metadata, failures, and recording cleanup.
Share only the common test-unit model; keep specialized scenario details explicit.
Add modest call-count or size-scaling checks where meaningful, rather than brittle
machine-specific millisecond limits. No new tests were added in this review.

## Validation performed

- `.venv\Scripts\python.exe -m ruff check --no-cache .` — passed.
- `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` — **78 passed**.
- Read-only AST/call-site analysis found the duplicated commit functions, leftover
  settings/parameters, test-only selectors, and large function boundaries.
- Small in-memory probes reproduced route reuse across force strengths, stale reservation
  release, double slot consumption on repick, attacker-radius cache reuse, order-dependent
  clustering, and the connectivity-label mismatch. These probes did not modify source files.
- No SC2 games, benchmark sweeps, deployments, dependency updates, or implementation
  changes were made. Performance estimates still need a controlled single-game profile.

Before implementing the combat changes, capture a seeded baseline of both sides of the
tank, wall, cramped-main, and open-army scenarios plus a long `--jobs 1` game. Compare
orders/targets, losses, kill time, and per-stage CPU/memory. Passing the present offline
suite is a useful baseline, not evidence that the uncovered state interactions are correct.
