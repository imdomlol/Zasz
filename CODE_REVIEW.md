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

## Verification summary (2026-10-05)

Every finding was checked against the code at commit `810a6e6` (read-only; no fixes applied). 01–03 were
verified directly; 04–44 by independent agents (a different model), with their key claims spot-checked
against the code before being written here. Each finding has a **Verification** section below with the
evidence, a proposed fix (flagged where it **changes behavior**) and a test.

| # | Verdict | Review | Verified | # | Verdict | Review | Verified |
|---|---|---|---|---|---|---|---|
| 01 | real, overstated | High | Low–Medium | 23 | partly real | Medium | Low |
| 02 | real (scout-plan leak; reach-map memory) | High | Medium / Low–Medium | 24 | real, narrow | Medium | Low–Medium (24a), Low (24b) |
| 03 | real | High | **High** | 25 | real, small | Medium | Low |
| 04 | real | High | Medium | 26 | real | High | **High** (benchmark.py), Medium (scenarios) |
| 05 | partly real | High | Medium (latent part Low) | 27 | partly real | High | Low–Medium |
| 06 | real, narrow | High | Low–Medium | 28 | real in principle | High | Low (Medium if maps share a factor with races) |
| 07 | real, one-step effects | High | Low–Medium | 29 | real | Medium | Medium (**High** for before/after use) |
| 08 | real (duplicated work) | High | Medium | 30 | partly real | Medium | Low |
| 09 | real (repeated sims) | High | Medium | 31 | real, negligible | Medium | Low |
| 10 | real, small | High | Low–Medium | 32 | real gap | Medium | Medium |
| 11 | partly real | Medium | Low–Medium | 33 | real | Medium | Medium |
| 12 | partly real (+ new: `far_holder` scan) | High | Medium | 34 | partly real | Medium | Low |
| 13 | real, small | Medium | Low | 35 | real | Medium | Low |
| 14 | partly real | Medium | Low | 36 | partly real | Medium | Low |
| 15 | real, cost unmeasured | Medium | Low–Medium | 37 | partly real | Medium | Low |
| 16 | real (test gap) | High | Medium | 38 | real, no consumer yet | Medium | Low |
| 17 | real | Low | Low | 39 | partly real, latent | Medium | Low |
| 18 | real, negligible cost | Medium | Low | 40 | real (mostly docs) | Medium | Low |
| 19 | real (metric only) | High | Medium | 41 | real | Low | Low |
| 20 | real | High | Medium | 42 | partly real | Medium | Low–Medium |
| 21 | real | Medium | Medium | 43 | real (more stale than listed) | Low | Low |
| 22 | real | Medium | Medium | 44 | real | Medium | Medium |

**Before a before/after benchmark (e.g. on Linux):**

- **26:** `scripts/benchmark.py` records the commit without a `-dirty` marker, so an uncommitted change is
  recorded as the same commit as its baseline. Commit (or label) both runs.
- **29:** benchmark matchups are an unseeded shuffle and games get no seed. Pass the same explicit `--maps`
  and `--races`, with a `--games` count that is a multiple of maps × races, to both runs, and compare per
  matchup.
- **28:** check the map count on that machine: if it shares a factor with the race count, some map/race
  pairs never get played.
- **33:** a crashing bot still exits 0. Read the `Crashes:` line of the summary, not the exit code.

Fixes that change decisions (so a benchmark may legitimately differ): 02 (scout plans), 03, 04, 05, 06, 07,
13 (patrol timing), 20, 21, 24, 25. Pure performance or cleanup fixes (results should be identical): 01, 08,
09, 10, 11, 12, 14, 15, 17, 18, 22, 23. 19 changes only the efficiency metric.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** the finding is real, but it bundles three
things of very different weight under "High": one behavior bug (Medium), one memory bound worth adding
(Low–Medium), and the rest negligible.

1. **Dead or merged scouting squads block their scouting spot for the rest of the game — Medium,
   confirmed in code (not reproduced in a game).** Squad ids are never reused (`SquadBook` counts up).
   `SquadBook.update` deletes squads whose zerglings all died and `_merge` deletes the absorbed squad
   ([bot/squads.py](bot/squads.py)); neither tells `Army`. `squad_scouts` only drops a plan when its
   squad runs `scout_goal` (scout done) or is reassigned to a raid (`assign_raids`), and a deleted squad
   does neither. `send_squad_scout` treats every stored plan as claimed (`busy`), so every expansion
   within 10 tiles of a dead scout's spot is skipped (`"squad scout: skip taken"`) for the rest of the
   game. A scouting squad isn't "busy" for `_merge`, so a merge mid-scout is enough to trigger it.
2. **`reach_cache` keeps every area's map forever — Low–Medium.** Keys are 10-tile areas; an expired
   map is only replaced when a squad uses that area again, and nothing is ever evicted. Measured
   offline (`tracemalloc`): a typical fight window (52×40 tiles) is ~18 KB bare and ~400 KB once its
   walking graph and approach fields for 8 targets are built. A long game touching 50–100 areas would
   keep roughly 20–40 MB of stale maps; the worst case (all ~400 areas of a 200×200 map) ~160 MB, which
   is unlikely. Wasteful, not dangerous.
3. **Everything else is negligible.** `last_damaged`, `enemy_state`, `enemy_moved`, `backed_off`,
   `breach_notes`, `avoid_reasons`, `helps` and `BlockTracker`'s expired entries do keep dead tags or
   stale entries, but each entry is a few dozen bytes per unit/squad/area ever seen: kilobytes to low
   megabytes over a long game. `avoid_reasons` is overwritten when an area at the same center is
   added again, so stale reasons don't change decisions.

Proposed fix:

1. Scout plans: each step, before `send_squad_scout` runs, drop `squad_scouts` entries whose squad id
   is no longer in `self.book.squads`. **This changes behavior** (those spots become scoutable again),
   so a before/after benchmark may legitimately differ in scouting.
2. Reach maps: once a second, evict `reach_cache` entries older than a few `REACH_SECONDS` (e.g. 5 s).
   They would be rebuilt on next use anyway, so decisions should be identical.
3. Small maps: prune dead tags occasionally (e.g. with the existing `forget_dead` pass); tidiness only.

Test: a scouting squad that dies, and one that is merged mid-scout, free their spot; memory stays
bounded across many fight areas in a long game.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real, and "High" is fair: unlike 01 and
02 this is a correctness bug in where squads walk, not a cost. Confirmed by reading the code; the probe
was not re-run, and the in-game effect is unmeasured.

- `route_map` leaves out every enemy group the *calling* units beat (`if units and wins(ai, units,
  g.units): continue`), then stores the result in the single `self.routes`, reused by every caller for
  `PLAN_SECONDS` (2 s). Nothing in the cache records which units it was built for.
- All three callers pass their own squad's units:
  1. Travel waypoints in `control_squad` (every step, for squads pushing, patrolling, raiding or
     scouting). When a strong raid built the map, a small patrol gets a route straight through a group
     it can't beat (that group is only in the map if even the strong squad loses to it).
  2. Destination checks in `squad_goal` and `push_target` use `dangerous_for` and `route_ok`
     together. `dangerous_for` is per squad but only looks at the destination, not the way there, so
     it doesn't cover case 1. `route_ok` uses the shared map: when a weak squad built it, a strong
     squad's destination can be wrongly rejected as unreachable.
  3. `send_squad_scout` measures its round trip on the shared map, with the same mix-up.
- A mismatch is the normal case in a real game: several small patrol squads alongside one or two big
  raid squads, and which one asks first changes every window. A weak squad routed through danger would
  usually still back off on contact (its fight estimate runs once threats are near), after taking fire.

Proposed fix:

1. Cache route maps by the set of enemy groups the squad can't beat (e.g. a frozenset of each such
   group's unit tags), each entry expiring after `PLAN_SECONDS`. There are only a few distinct sets in
   practice (beats everything / nothing / some), so only a few maps get built.
2. `wins()` is a combat sim and `route_map` runs every step per travelling squad, so also cache the
   per-squad "can it beat this group" answers for `PLAN_SECONDS` (keyed by the squad's unit tags and the
   group), or this adds a lot of step time.
3. Don't use one conservative map that avoids every armed group: a raid's destination is inside its
   target's zone, so `route_ok` would always fail for raids.

Test: a strong squad then a weak squad (and the reverse order) at the same game time each get their own
correct map: the weak one routes around the group, the strong one doesn't. Benchmarks before and after
may differ in patrol and raid paths, which is expected.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real; Medium fits better than High. The fallback still uses ares' danger-cost pathing rather than walking blind, but it defeats the intent that a push "doesn't walk back into a known position".

- `push_target` ends with an unchecked `return ai.enemy_start_locations[0]` when every spot fails `fresh` / `dangerous_for` / `can_walk_to` / `route_ok` (spot-checked). The `push` branch of `squad_goal` re-evaluates every step, so a push squad keeps heading for the main.
- In `control_squad`, `routes.waypoint(...)` returns None when there is no route; `target` is then left as is, and the squad moves on ares' grid, where danger is only a cost ("straight through when there's no cheap way round", per the code's own comment).
- Patrol's `dangerous()` check in `squad_goal` has no `route_ok`; harmless when no spot qualifies (the squad goes to `rally_point`). Raids and defends never check `route_ok`, deliberately for raids (the sim says they win).
- Contact is still caught by `can_reach_soon` and the back-off/fight logic: the cost is taking some fire before backing off.

Proposed fix:

1. `push_target` returns None when nothing is acceptable; `squad_goal`/`assign_raids` then keep the squad at its patrol/hold spot or `rally_point`. **Changes behavior.**
2. Add `route_ok` to the patrol `dangerous()` check (uses the shared `route_map`, see 03). **Changes behavior.**
3. Optional: when `waypoint` is None for a travelling squad, hold or back off instead of falling back to a plain move. **Changes behavior.**

Test: a map where the only route to the main crosses a sieged tank's range: the push squad stays out. Benchmarks may differ in push timing.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real. Sharing one `ReachMap` between squads is a real bug today (Medium); the radius/range key part is latent (Low) while the army is zerglings only.

- Reproduced (offline probe): `reach_map` keys on `(round(cx/10), round(cy/10))` and reuses the map for `REACH_SECONDS` (1 s). Its `reached` flood fill is seeded only from the first caller's units. With a wall between two squads in the same 10-tile cell, the map built from the left squad gave `open_sides` 10 for the left enemy and 0 for the right one; the right squad reusing it sees its own enemy as unreachable (`surroundable` False, so it holds or retreats). How often squads sit on opposite sides of a wall that close is unmeasured.
- Latent (probe): `open_sides` returned 10 for radius 0.375 and again 10 for radius 1.0 (a fresh map gives 28), because it caches by target only. Every caller passes a zergling radius today. `settled` is keyed by radius but not range.
- Partly by design: `settled`, `breach_caps`, `plugs` and `set_danger` are deliberately shared (comments say so; `breach_caps` was made one army-wide view to stop squads flip-flopping). A per-squad key everywhere would undo that.

Proposed fix:

1. Key the area cache by its inputs too (flood-fill seed cells or squad id, plus the enemy tags), or seed the flood fill from every army unit in the window. **Changes behavior** for squads on the far side of a wall.
2. Key `open_sides` by `(tag, round(radius, 3))`: no-op today, removes the trap.
3. Document which caches are shared by design.

Test: two squads on opposite sides of a wall in one area cell each see their own enemy as reachable, in both call orders. Benchmarks may differ for fights with several squads close together.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real and reproduced, but the geometry is narrow and the effect small: Low–Medium.

- Probe: a diagonal step where one orthogonal cell is terrain and the other is blocked by an enemy unit. `_squeezed` forbids it, so `reached` is False, but `safe_labels` (`label` in `set_danger`) and the `regions` label in `breaches` give both cells the same label; `safe_step` toward that cell returns None.
- Where it matters: `attack_spots`/`safe_attack_spots` already require `reached`, and `work_from_cell` holds when `safe_step` is None, so safe work can't send a unit into such a pocket. `breaches` and `widen_plan` use `label(walkable)`: a sealed pocket that touches our side only through such a diagonal counts as reached, so its breach or widening is missed.
- Needs terrain/a building on one side of the corner and an enemy unit on the other. Corner gaps between two buildings (commit `4bd2ae1`) are unaffected.

Proposed fix:

1. One labelling helper using the same adjacency as `_build_graph` (`scipy.sparse.csgraph.connected_components` on it), used by `set_danger`, `breaches` and `widen_plan`. **Changes behavior** only in that geometry (more breaches/widening found).
2. Keep the corner-gap regression test.

Test: the probe geometry as a unit test (same label implies reached, or a `safe_step` path). Benchmarks identical except on maps with such corners.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real and reproduced, but each effect lasts one step: Low–Medium, not High. The release half is in the slot code changed today (the count-based version before it had the same flaw).

- Release (probe, spot-checked in `Assignments.release`): `start()` reserves only attackers that picked within `recent` (0.5 s), but `release()` subtracts the attacker's share whenever its target has any reservation. A stale attacker (it travelled or backed off and rejoins `fight()`) therefore removes a recent attacker's reservation: one share too little held for the rest of the step, so a target can be over-filled by one, and `busy()` can read False for a target we are on.
- Repick (probe): on the detour path `pick_tiered` takes a slot, then `detours.plan` recurses into `fight`, which "detour follow"s and calls `take` again (`taken` 2, `used` 1.875 for one attacker). On `blocked, repick` the old target keeps its share for the rest of the step.
- `taken`/`used` are cleared every step, so a phantom claim lasts one step; `assign()` already points the attacker at its new target.

Proposed fix:

1. `start()` records what each attacker actually reserved (`reservation[attacker] = (target, share)`); `release()` pops and subtracts exactly that, so a stale attacker releases nothing.
2. Track this step's claim per attacker; `take()` first undoes an existing claim (subtract `used`, decrement `taken`), so repicks and detour-follow replace rather than add. **Changes behavior** slightly (slots freed a step earlier).

Test: the three probes as unit tests (stale + recent attacker; repick gives one claim; detour take-twice gives one). Benchmarks may differ slightly in fights with detours.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real as duplicated work, but the grouping itself is cheap; the repeated `wins` sims are the cost. Medium, unmeasured.

- `enemy_groups` is rebuilt by: the patrol branch of `squad_goal` (on reselect), `avoid_armies_we_cannot_beat` (every `PLAN_SECONDS`), `route_map` (every 2 s), and `dangerous_for`, which runs every step per push squad and once per candidate spot in `push_target` (~14 spots when the fallback is hit) and again in `send_squad_scout`. Each call re-clusters and re-runs `wins` per group.
- Probe: `cluster` takes 0.13 ms for 30 units, 0.2–0.6 ms for 100–200 — negligible. A worst-case push with several squads is a few dozen calls a step: a few ms.
- Each `wins` is a full `can_win_fight` (ares doesn't memoize). `dangerous_for` repeats it per group per spot with the same squad and groups. Per-sim cost unmeasured; sims weren't among the top entries of the `wall_bunker` profile.

Proposed fix (pure perf, decisions identical):

1. Cache `enemy_groups(ai)` per `ai.state.game_loop`.
2. Memoize `wins` per step, keyed by (own tags, group tags, threshold).
3. In `push_target`, compute the squad's losing groups once and test each spot against them.

Test: count `enemy_groups` builds and `can_win_fight` calls per step in push scenarios; before/after benchmark identical.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real — the same forces are simulated repeatedly. Medium: wasted work, not a bug; per-sim cost unmeasured.

- `needed` (squads.py) is called as `needed(HELP_WIN_AT) or needed(FIGHT_AT)` in `assign_raids`, so a failed first pass re-simulates the same sizes (probe, 30 zerglings: 5 + 5 sims for 5 distinct forces).
- `strike_size` loops both thresholds over the same sizes, then tests the full pool again (probe: pool 30 gave 31 sims for 15 distinct forces). `update_strikes` runs it every step for every group shooting at us.
- `choose_helpers`: once `extra[:room]` is full, every later candidate re-simulates the same force (probe, room 12 / 10 candidates: 21 sims, one force repeated 16 times); the `wins_at_all` pass repeats them under the looser bar.
- With bunkers, `strike_result` goes through `through_bunkers` (the `duel` loop, up to 600 iterations, plus a sim): heavier than a plain sim, so `wall_bunker` is hit hardest.
- Verdicts are deterministic enums, so one evaluation serves both thresholds.

Proposed fix (pure perf, results identical):

1. `strike_size`: evaluate each size once; take the first size at `HELP_WIN_AT`, else the first at `FIGHT_AT` — same sizes, same order, same answer.
2. `needed`: compute each size's verdict once for both thresholds.
3. `choose_helpers`: stop once `len(extra) >= room`; evaluate each force once against both bars.
4. Don't assume monotonicity or binary-search: that would change decisions.

Test: count `can_win_fight` calls in these cases; return values identical on randomized verdict tables; before/after benchmark identical.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real but small: Low–Medium, not High.

- `set_danger` rebuilds the meshgrid, zone raster and `label` once per map per step: 0.2–0.4 ms for windows of 35×30 to 98×78 with 8–12 zones (offline probe). Over ~1,700 steps and a few maps, under ~1.5 s (estimate).
- `breach_step` builds a weighted graph per goal (its cost depends on the goal): 1.0–2.9 ms per goal including the graph, of which the graph is ~0.3–0.7 ms, so sharing structure saves about a third. The `wall_bunker` profile put `breach_step` at ~1 s of ~19 s. `safe_step` builds its graph once per stamp.
- With stationary enemies the zones are identical step to step, but a `ReachMap` lives only `REACH_SECONDS` (~11 steps) anyway.

Proposed fix (pure perf; check invalidation):

1. In `set_danger`, if the zones equal `danger_zones` (within a small tolerance), keep `danger`, `safe_labels`, graphs and the safe/breach fields, and only update the stamp. Keep clearing `step_cache`.
2. Cache the meshgrids per map (the window is fixed).
3. `_build_graph`: build the structure once and apply weights from a cost vector, shared by `breach_step` goals.

Test: a stationary-enemy scenario shows fewer `label`/Dijkstra calls with identical orders; benchmark identical (invalidation must still fire when an enemy moves).

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real — the downhill loop is copy-pasted four times and the two graph builders overlap; the rest isn't shared as claimed. Low–Medium (maintenance, no runtime cost).

- Probe: with no unit-blocked cells, `ReachMap._build_graph(walkable)` and `_open_graph(walkable)` give identical sparse matrices; they differ only by the intended `_squeezed` diagonal rule.
- The downhill walk (lowest neighbour, up to `lookahead` steps) appears in `breach_step`, `safe_step`, `approach_step` and `RouteMap.waypoint`. `breach_step` and `safe_step` differ only in which field they read; `approach_step` adds a blocked-start fallback; `waypoint` uses its own neighbour list.
- Overstated: `group_field` and `walk_to` share little beyond seeding plus `dijkstra(min_only=True)`; `walk_distance` is test-only (see 17).
- Subtle: `safe_step` builds its graph from `walkable & ~danger`, but `_neighbors`/`_squeezed` read `self.walkable`; a shared helper must choose which mask the diagonal rule uses.

Proposed fix (pure refactor):

1. One `build_graph(walk, unit_blocked=None, cost=None)` used by `ReachMap` and `RouteMap`; assert equal matrices in a test.
2. One `descend(field, cell, neighbors, lookahead)` helper for the four loops; keep `waypoint`'s unguarded diagonals unless you deliberately want the corner rule there (that would change behavior).

Test: existing `safe_step`/`approach_step`/`RouteMap` tests; identical orders before/after.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real — the per-unit list building named is negligible, but two other per-unit costs scale badly with army size, one of them in code added today. Medium (synthetic measurements only).

- Negligible: `set_danger` already returns early after the first unit; the per-unit prelude in `fight()` costs ~0.012 ms per unit (probe, 12 armed enemies, 20 structures).
- Real: `safe_work` calls `safe_attack_spots` for every candidate structure for every attacker, and each spot goes through `in_danger` (a Python loop over all zones). Probe: ~1.2 ms per call, ~80% in `in_danger`; for 100 zerglings in the "fighters around but none reachable" branch that is ~120 ms per step (budget ~80 ms per step at 40 ms per loop). Unmeasured in a real game. `covered_work` is cached per step; `safe_work` is not.
- Real, not in the review: `Assignments.far_holder` (slot take-over, added today) scans every assigned attacker army-wide (spot-checked), and runs inside `has_room` whenever a target is full, which `pick_tiered` calls per candidate per tier per unit. Probe: all targets full, 100 zerglings 6 ms per step, 200 zerglings 25 ms per step — quadratic.
- The "nearby scans traverse every friendly unit" point is the `crowd` list in `fight()`, built only when a unit becomes blocked: rare.

Proposed fix (perf only):

1. Cache each structure's danger-free spots in `ReachMap.step_cache`, like `covered_work`; per unit only filter by safe area. (A numpy cell-based danger test is faster but can differ at circle edges; keep the exact point test if orders must be identical.)
2. Keep per-target holder lists in `Assignments` (updated in `assign`/`drop`) so `far_holder` scans only that target's holders.
3. Don't cache `has_room` results: they change as attackers take slots.

Test: offline probe comparing `safe_work` results before/after caching (identical); benchmark orders identical, step time lower.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real but small: Low (a few ms per step at most).

- `Patrol.update` checks every patrol spot against every unit with sight each army step. Probe: 80 spots with 100/250/400 own units took 1.7/2.6/3.8 ms per step; `PATROL_FRESH` is many seconds, so every few steps would do.
- `Squad.squad_position` is recomputed on every access; `control_squad` passes it per unit into `fight()`, quadratic per squad (probe: 40-unit squad 0.5 ms per step; squads are usually 8–12). `SquadBook.update`/`_merge` also call it repeatedly.
- Overlay rescans only run with the overlay on. The review is right that `_split` and strike removal change membership, so a cached center must be invalidated.

Proposed fix:

1. Hoist `position = squad.squad_position` out of the per-unit loops in `control_squad` (pure perf, identical behavior).
2. Update patrol visibility every N steps (**changes behavior** slightly in patrol timing).
3. Optional: cache `squad_position` per step, invalidated in `update`/`_merge`/`_split`.

Test: `tests/test_squads.py`; benchmark identical for step 1, may differ slightly for step 2.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real, overstated: Low.

- `observe()` calls `next_expansion_location` every step and `macro_plan` again only on `Build.EXPAND`; both use the same `ExpansionAccess` cache, and the loop returns at the first acceptable base.
- `observe()` counts 4 unit types and 8 structure types (~12 filter scans per step); `remaining_gas` is computed twice per step (6 upgrades, trivial).
- Hazard if sharing state: `drone_target` (inside `observe()`) reads `ai.workers_per_gas`, which `on_step` updates after `observe()`, so the observation sees last step's value; moving that earlier would change decisions.
- Missed by the review: `drone_target` filters `mineral_field.closer_than(10, th)` per townhall every step (unmeasured).

Proposed fix:

1. Compute `remaining_gas` once in `on_step` and pass it on (pure perf).
2. Let `macro_plan` reuse the expansion location found for the observation, stored on `ai` rather than in `Observation` (whose layout the recording schema depends on).
3. The `workers_per_gas` one-step lag is a behavior change; leave it for a pure cleanup.

Test: `tests/test_observation.py`, `tests/test_macro.py` unchanged; benchmark identical.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** the mechanism is real, cost unmeasured; Low–Medium. The review missed a likelier cause of rechecks.

- `_find_blockage` builds up to 3 grids and runs up to 3 A* searches per destination; each grid copies the pathing grid and loops over all structures in Python (read, not timed). Usually one grid and one search.
- Rechecks fire after `EXPANSION_RECHECK_SECONDS` (10 s) or when `_forget_if_blockers_changed` sees a different tag set. That fingerprint includes creep tumors, which queens spread continuously, so every new tumor clears `_checked` — yet tumors never block these paths.
- Sharing the terrain-only grid across destinations is correct; the structure-dependent grids are safe to share only within one step.

Proposed fix (perf only):

1. Leave creep tumors out of the fingerprint (or hash only `ExpansionAccess.BLOCKING` types): the biggest lever, same answers.
2. Cache the two structure-dependent grids per game loop and the terrain-only grid permanently.
3. Don't switch the first check to connectivity labels without confirming they agree with the A* grid's diagonal rules.

Test: an offline test with a fake `ai`/`map_data` counting grid builds; full-game benchmark identical.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real, but a testing gap rather than a runtime bug: Medium.

- `TargetSlots.pick` has no production caller (spot-checked; the `pick` call in `army.py` is `Patrol.pick`). `fight()` uses `pick_tiered`, which has no test; the 5 selector tests in `tests/test_efficiency.py` exercise `pick`.
- They diverge (probe): a zergling ordered on a bay, with a marine in a higher tier: `pick` keeps the bay, `pick_tiered` switches to the marine. `test_keeps_current_target_while_it_has_room` asserts the opposite of live behavior for that case.
- `pick` never calls `Assignments.assign`, so it doesn't exercise cross-step stickiness, reservations or slot take-over; nor block filtering, the flank case or tier ordering.

Proposed fix:

1. Rewrite the 5 tests against `pick_tiered` (real `BlockTracker`, fake units), plus: tier beats stickiness; a kept target survives `Assignments.start`; all-blocked gives `flank=True`; full targets fall back to the closest in the best tier; far-holder take-over.
2. Then delete `TargetSlots.pick`. No decision changes (nothing in production calls it).

Test: `uv run python -m pytest -q`; benchmarks unaffected.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real; Low is right.

- `MIN_OPEN_SIDES` (`bot/settings.py`) is referenced nowhere (spot-checked).
- `ReachMap.breach_caps` is initialised to None and never read or assigned; the live cache is `Army.breach_evals`.
- `ReachMap.walk_distance` and `_near_fields` are used only by tests: a leftover of the old "within a 10-tile walk" rule.
- `Army.breach_caps(ai, reach, units, enemies, threats, present)` uses only `ai`, `reach` and `enemies`.
- `keep_alive_spot(bot, layout)`: `layout` unused (4 callers in `scenarios/`). `TargetSlots.pick`: see 16.

Proposed fix (no behavior change):

1. Delete `MIN_OPEN_SIDES` and `ReachMap.breach_caps`.
2. Drop the unused parameters of `Army.breach_caps` and `keep_alive_spot`, updating callers.
3. Remove `walk_distance`/`_near_fields` and their tests, or keep them with a comment; update `notes.md` accordingly.

Test: pytest; import `scenarios.walls`/`scenarios.efficiency` offline; benchmarks unaffected.

### 18. Avoid-area maintenance is duplicated and hidden in queries — Medium / Confirmed

**Location:** [bot/army.py](bot/army.py), `turned_back` (~1230),
`mark_avoid` (~1299), `avoided` (~1334), `blocked`.

`turned_back()` calculates geometry then duplicates `mark_avoid()`'s replacement, append,
counter, and reason bookkeeping. Every `blocked()` query calls `avoided()`, which filters
and replaces the stored list and allocates a second list. Candidate loops repeatedly do
the same expiration work, and apparently read-only queries mutate shared state.

**Opportunity:** one insertion function and one expiration pass per step, followed by
read-only queries against the prepared zones. Prune matching reason entries at the same time.

**Verification (2026-10-05, commit `810a6e6`; not applied):** duplication and hidden mutation are real; cost negligible: Low.

- `turned_back` repeats `mark_avoid`'s counter, replacement filter, append and `avoid_reasons` write; it could end with `self.mark_avoid(ai, center, radius, reason)` (same default `AVOID_SECONDS`): an exact refactor.
- `avoided()` rewrites `self.turned_back_at` on every call, and `blocked()` calls it. Probe: `blocked()` costs 0.7–5.2 µs with 0–8 zones.
- Hazard in the review's proposal: direct readers of `turned_back_at` (`find_partly_seen`, `send_squad_scout`, the overlay) see expired entries until the first `avoided()` call in a step. A single expiry pass at step start must run after `clear_empty_avoid_areas`, which extends an expired "scout needed" zone while an overlord is on its way.
- `avoid_reasons` is never pruned (memory side: see 02).

Proposed fix:

1. `turned_back` calls `mark_avoid` (identical behavior).
2. One `expire_avoid_areas` pass at step start (after `clear_empty_avoid_areas`), `avoided()`/`blocked()` read-only, pruning `avoid_reasons` in the same pass. Changes behavior only in the one-step timing edge above.

Test: offline test creating zones, advancing time, checking `blocked()` and pruned reasons; benchmark identical.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real, but it skews the efficiency metric, not gameplay: Medium. Today's changes make it more frequent.

- `DamageTracker.update` counts a unit only when `order_target` is a visible enemy tag. A move order has a `Point2` target, so the engagement is dropped and that step counts for nothing; the tracker never reads `Assignments`.
- The bot issues such moves while a target is still assigned: `work_from_cell`, `work_spot` (breach and covered work, added today), "step away (gap)" and the hold moves. Spot walking makes move-then-attack cycles more common than when the review was written.
- Effects: walks are excluded from potential (right for deliberate travel, but it hides a unit stuck with a move order), and each move-to-attack switch builds a new `_Engagement`, restarting `COMMIT_SECONDS` (1 s) so about 1 s of real attacking is dropped from potential per cycle. Net direction of the error isn't fixed; size unmeasured.
- `reset()` clears only `total`/`window`; the three scenario calls happen at scenario start, so it's harmless today.

Proposed fix (metric only, no decision changes):

1. Expose the army's assignments; when the order target is a point but the unit is assigned to a visible enemy, keep the engagement (and its clock) and count nothing for that step.
2. Drop an engagement only after ~1 s without an assignment; optionally add a "walking" bucket next to blocked/idle.
3. Make `reset()` clear `engagements` and `last_time`, or document that it doesn't.

Test: offline: attack, a few steps of point-moves, attack again: the clock doesn't restart and the walk isn't potential. Efficiency %, blocked and idle will legitimately change; kill time and dmg/s must not. Compare efficiency only between runs on the same side of the fix.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real and reproduced, but a group split rather than a crash: Medium.

- `cluster` (`bot/squads.py`) appends a unit to the first cluster it touches and never merges clusters (spot-checked). Probe: x = [0, 4, 2], radius 3 gives `[[0, 2], [4]]`.
- Probe on 2,000 random scatters (8–30 points in 60×60, radius 10): the group count differed from the true connected components in 64% of cases, by ~1.4 groups on average (sparser than a real army, so this overstates real frequency).
- Callers: `enemy_groups` (radius 10), `enemy_sites` (12), `update_strikes` (10), and `Army.unseen_circles` (its own copy of the same loop, radius 6). Input order comes from ares' cached army, which has no guaranteed order, so a split can flip between steps.
- Consequence: a chain of enemies can be judged as two halves by `wins`/sizing. Whether that causes wrong fights is unmeasured.

Proposed fix:

1. Make `cluster` return connected components (union-find), in a deterministic order. **Changes behavior** (fewer, larger groups: sizing, strikes and raids change).
2. Make `unseen_circles` use the same helper.

Test: every permutation of [0, 4, 2] gives one group; property test against a connected-components reference. Benchmarks may differ; judge on outcomes, not identical orders.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real; Medium is fair — an inconsistency risk, not a bug on its own.

- `squads.wins` calls `can_win_fight` directly, with no tank or bunker handling; it is used by raid/help/push sizing and `evaluate_breach`.
- `fight_result` drops sieged tanks we're on top of (`tank_neutralized`) and uses `through_bunkers` (without a `reach`); `strike_result` uses `through_bunkers` (with a `reach`) but not `tank_neutralized`.
- Not verified: whether `enemy_groups` ever contains bunkers (it reads ares' cached army, which probably excludes structures); if not, the bunker gap in sizing is smaller than stated.
- `evaluate_breach`, `widen_plan`, `choke_worth` and `through_bunkers` each reimplement "seconds to kill × covering DPS / health" with their own cover logic.

Proposed fix:

1. Pure refactor first: shared `kill_time`/`damage_taken` helpers (identical results).
2. Then one `combat_estimate(ai, own, threats, reach=None)` applying tank neutralization and the bunker path, used by `wins`, `fight_result` and `strike_result`. **Changes behavior** for `wins` and `strike_result`.

Test: step 1 identical orders; step 2 offline tests with a fake `can_win_fight` (tanks in minimum range, bunker cargo), then the tank and bunker scenarios before/after (may differ).

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real; Medium is fair (maintainability, not a defect).

- `bot/army.py` is ~2,600 lines; by AST `fight` is 282 lines, `control_squad` 162, `update_strikes` 136, `widen_plan` 130, `assign_raids` 127.
- `Squad.job` is a plain string with its values only in a comment; `Strike.reach` is typed `object`.

Proposed fix (pure refactors, orders must not change):

1. Typed first: `Squad.job` as an enum/`Literal`, `Strike.reach: ReachMap | None`.
2. Move diagnostics/notes, the route/reach caches, and `evaluate_breach`/`widen_plan` to their own modules, keeping the call order in `_control`.
3. Then split `fight` around a fight-context object.

Test: existing tests plus one scenario set with identical decision recordings and `COUNTS` summaries.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real, narrower than stated: Low.

- The module globals are shared by every `Army`. But `TARGET_NOTES`, `CHOKE_NOTES`, `CHOKE_OPENERS`, `UNIT_NOTES` and `BACK_OFF_CIRCLES` are cleared at the start of every `_control`, so the `CHOKE_NOTES` decision cache is per-step and can't leak across games.
- What can leak: `COUNTS` (see 01) and `REACH_DUMPED` (only with `ZASZ_DUMP_REACH`). Ladder games, scenarios and parallel benchmarks each run in a fresh process; the exception is `benchmark.py --jobs 1`, which plays games in a loop in one process. Two `Army` objects in one process (tests) also share the notes.
- `scenarios/army.py` and `overlay.py` import the notes directly.

Proposed fix:

1. Clear `COUNTS` and `REACH_DUMPED` at game start (no decision change).
2. Optional: a `Diagnostics` object owned by `Army`; keep `CHOKE_NOTES` as an `Army`-owned decision cache. Combine with 01.

Test: two `Army` objects have independent state; orders identical.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real but narrow: 24a (a crash) Low–Medium, 24b (spore retry) Low.

- 24a: `affordable_on_arrival` (`bot/actions/building.py`) calls `ai.workers.closest_to(target)` when minerals are short (spot-checked). Probe: an empty `Units.closest_to` raises `AssertionError`, uncaught in `on_step`. Reachable via `macro_plan` → `Build.EXPAND`: `expansion_wanted` is true with zero drones once `drone_capacity` is 0 (mined-out bases). So: no drones, mined-out bases, a free expansion and under 300 minerals — a late-game corner, in a game that's already being lost.
- 24b: `build_spores` returns after the first hatchery with a spot even when `find_placement` or `select_worker` fails, so later bases are skipped, and `find_placement` (a game API round trip) is retried every step. Cost unmeasured.

Proposed fix:

1. 24a: with no workers, return True (or treat arrival as immediate). **Changes behavior** only in a state that crashes today.
2. 24b: only return after a successful build, otherwise try the next hatchery; remember a failed placement per hatchery for a few seconds. **Changes behavior** (spores at other bases, fewer API calls).

Test: offline fakes for both (empty workers with low minerals must not raise; two hatcheries with the first unplaceable). Normal benchmarks identical (neither path is hit in ordinary games).

### 25. Injector assignment can churn and repeats every step — Medium / Profile first

**Location:** [bot/actions/queens.py](bot/actions/queens.py), `pair_injectors`, `control_queens`.

Every step greedily assigns each ready hatchery its closest remaining queen from scratch.
Small movement during defense can swap assignments and turn previous injectors into
creep queens. It also repeats distance work even when bases and roles have not changed.

**Opportunity:** keep valid queen-to-hatchery assignments until death, a base/role change,
or an explicit reassignment condition. Verify recovery after defense and queen loss;
measure role changes as well as CPU before treating this as a performance win.

**Verification (2026-10-05, commit `810a6e6`; not applied):** real but small: Low.

- `pair_injectors` (`bot/actions/queens.py`) rebuilds the pairing every step, greedily in hatchery order. Cost trivial (~4×4 distances).
- Churn is possible but self-limiting: `defend` runs first, and creep queens roam away, so injectors usually stay the nearest queens. Frequency and larva cost unmeasured.

Proposed fix:

1. A persistent `{hatchery: queen}` map, refilled only for missing pairs. **Changes behavior** (assignments stop following small movements).
2. Before changing anything, count role switches per game in a benchmark to see whether it happens at all.

Test: offline test of `pair_injectors` with a queen dying and a base added; benchmark outcomes may differ (larva timing).

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real; the benchmark and scenario halves differ. High for `benchmark.py` (it can mislabel which code ran), Medium for the scenarios.

- `scripts/benchmark.py` and `scenarios/efficiency.py` have identical `current_commit()` functions: read `.git/HEAD` and the loose ref file, `"none"` on `OSError`, never a `-dirty` suffix (spot-checked). Probe: after `git pack-refs --all` (which `git gc` does) or in a `git worktree`, it returns `none`; `benchmarks/results.csv` already has 6 such rows.
- **For a before/after check:** an uncommitted refactor benchmarked on top of commit A is recorded as `A`, the same as the baseline; only timestamps tell them apart. `efficiency.csv` has the same problem.
- `run_scenarios.current_commit` uses git, but its dirty check counts the tracked `benchmarks/*.csv` it just appended to: the first run on a clean tree is `A`, every later one `A-dirty`. Its `compare()` then picks the last row with any *other* commit string — which can be a third commit or the other machine's rows (silent, not wrong). Rows are keyed by SC2 build, so Windows and Linux don't cross-compare there; `results.csv` has no build column.

Proposed fix (tooling only):

1. One shared helper: commit via `git rev-parse`, dirty via `git status --porcelain -- . ':!benchmarks'`, plus a short hash of `git diff HEAD -- bot scripts scenarios`; used in all three places.
2. Add `sc2_build` and `run_id` columns to `results.csv` and `efficiency.csv` (and `run_id` to `scenarios.csv`).
3. `compare()` skips rows from the current invocation.

Test: the helper on a temp repo (loose refs, packed refs, worktree, only `benchmarks/x.csv` changed, `bot/x.py` changed).

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real: Low–Medium. The missing columns are a real analysis limit; the concurrency race is only a fresh-file edge case.

- `StructureAssault.report` (`scenarios/efficiency.py`) writes from each worker: `exists()` check, append, header if absent. Two workers on a missing file could both write a header; the file has one header today. Single short appended lines make torn rows unlikely.
- Missing keys are real: columns are `timestamp, commit, scenario, kill_time, dps, efficiency, blocked, idle, dealt, taken` — no seed, side, build, status or run id. `efficiency_cramped_main` has ~500 rows mixing sides and `--seeds` games, so per-commit averages mix different games. `scenarios.csv` records the same runs with seed/side/build, joinable only loosely by timestamp.
- `efficiency or 0` conflation: 8 all-zero rows, distinguishable by `dealt == 0`.

Proposed fix (tooling only):

1. Append `side`, `seed`, `sc2_build`, `run_id` columns (old rows still parse); write blank for undefined efficiency.
2. Better: return the metrics in `Report.stats` and let the parent write the row, as for `scenarios.csv` (removes the race).

Test: two fresh-file scenarios with `--jobs 2` give one header; compare one seed across two commits via the new columns.

### 28. Benchmark pairing fails to cover the map/race cross product — High / Reproduced by construction

**Location:** [scripts/benchmark.py](scripts/benchmark.py), `main` (~326), matchup creation.

Zipping a cycle of maps with a cycle of races gives only `lcm(map_count, race_count)`
distinct pairs, rather than their cross product. With three maps and three races, even
nine games repeat the same three pairings three times; six combinations never run.
Shuffling maps changes which pairings are missed but does not solve coverage.

**Opportunity:** cycle a balanced product of map/race pairs, with a deterministic shuffle
or explicit schedule. Test the planned matchups without launching games.

**Verification (2026-10-05, commit `810a6e6`; not applied):** real in principle, but not with the 7 maps installed on the Windows PC: Low (Medium if the Linux map count shares a factor with the race count).

- `main` zips `cycle(maps)` with `cycle(races)`, covering `lcm(M, R)` pairs. Probe: M=3, R=3 gives 3 distinct pairs in 9 games; M=6 gives 6 of 18; M=4 and M=5 cover everything.
- Windows has 7 maps; gcd(7, 3) = 1, so 21 games cover everything (the default 6 games see 6 distinct pairs). It bites when gcd(M, R) > 1, e.g. 3, 6 or 9 maps, or `--maps` with 3 or 6 entries.
- The unseeded shuffle is 29's issue.

Proposed fix (tooling only):

1. Build the full product `[(m, r) for r in races for m in maps]`, order it with a seeded shuffle (see 29), and cycle through it.
2. Print the planned matchups before launching.

Test: the planner with M=3, R=3, games=9 gives 9 distinct pairs (no SC2 needed).

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real — Medium in general, but **High for the Linux before/after use**: the most important finding for that.

- `random.shuffle(map_names)` is unseeded and `run_game(...)` gets no `random_seed` (spot-checked), unlike the scenario runner. With the default 6 games on 7 maps, each run plays a random subset of maps with races assigned by position, so baseline and refactor runs can play different matchups. Hard-AI win rates over ~6 games are noisy enough that a difference can be the draw, not the code.
- `results.csv` records no seed or run id; comparing runs is done by hand.
- Even with seeds, the built-in AI and SC2 aren't bit-for-bit repeatable over a 20-minute game: a seed fixes the schedule, not the game.

Proposed fix (tooling only):

1. `--seed` (default random, printed) for the map shuffle; pass `random_seed=seed + i` to `run_game`; record `seed` and `sc2_build` per row.
2. Until then: pass `--maps` explicitly, use a `--games` count that is a multiple of maps × races (or one `--races X` per invocation), use the same values both times, and compare per matchup.

Test: the same seed plans identical matchups twice; two short games with the same seed have the same opening in the replays.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real; the scan cost is trivial: Low. The header rewrite is the only part worth fixing.

- `load_results()` runs once; `compare()` is a linear scan per result (~950 rows × tens of results: microseconds).
- `open_results()` in `benchmark.py` rewrites `results.csv` in place when its header differs from `FIELDS`, with `extrasaction="ignore"` (drops unknown columns); a crash mid-rewrite would truncate it. Only triggers when `FIELDS` changes.
- With union merges, two machines on different headers could leave a stray header line that `DictReader` reads as data (not reproduced).

Proposed fix (tooling only):

1. When the header differs, write `results.csv.tmp`, keep a `.bak`, `os.replace`, and warn about dropped columns.
2. No history index: not worth the code.

Test: a CSV with an extra column gets a `.bak`; an interrupted rewrite leaves the original intact.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real but negligible: Low.

- `BenchmarkZasz.step_times` keeps one float per step: ~13,400 for a 20-minute game (~100 KB); count/sum/max would do.
- `run_scenarios` keeps every `Report.log` until the end: probably tens of KB each, megabytes for ~50 runs (unmeasured). Results are reported in submission order by design.

Proposed fix (optional, tooling only): aggregate step times; drop `Report.log` after printing. Not needed for the refactor check.

Test: aggregated and list versions give the same `frame_ms`/`avg_step_ms`/`max_step_ms` on synthetic samples.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** a real gap; whether the watchdog works on a truly frozen SC2 is unmeasured. Medium is fair; worth a quick test on Linux.

- `run_scenario` arms a `threading.Timer` that calls `_thread.interrupt_main()`: that sets a flag in the worker's main thread rather than delivering a signal, so a main thread blocked in a socket wait may not wake. Not confirmed either way without a hung SC2.
- The parent waits on `future.result()` with no timeout; `benchmark.run_matchups` and `seeds.probe` have no wall-clock limit (`game_time_limit` is game time).
- History: 9 scenario rows say `worker died: BrokenProcessPool`; when one worker dies the pool fails every pending future, so the rest of that run is recorded as `ERROR` too. No row says `no result after ...`: the watchdog has never fired.
- `except (KeyboardInterrupt, Exception)` in the worker records Ctrl+C as an `ERROR`.

Proposed fix (tooling only):

1. A parent-side deadline (`future.result(timeout=...)`); on timeout kill the worker's process tree and record `ERROR: timeout`. Same wrapper for `benchmark.run_matchups` and `seeds.probe`.
2. One subprocess per game, so a dead worker doesn't fail the whole pool.

Test: a fake worker that sleeps forever (no SC2): the parent returns after the deadline; killing one worker marks only its own row `ERROR`.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real; Medium is fair for a regression check — a crashing bot still exits 0.

- Probe: `print_summary([])` raises `ZeroDivisionError`; 3 `Crash` rows print `Overall: 0/3 (0%)` and `Crashes: 3`. `main` exits 1 only when `over_ladder_limits` finds something (spot-checked), so crashes alone exit 0.
- `--games 0` or no installed maps reaches the `ZeroDivisionError` after the run directory exists. `--races` has no `choices`: a bad name (or a bad `--maps` name) becomes a `Crash` row per game instead of an upfront error.
- `run_scenarios --repeat 0` prints `0/0 passed` and exits 0 (after the seed lookup, which may launch probe games).
- For now, the `Crashes: n` summary line is the only signal; checking the exit code isn't enough.

Proposed fix (tooling only):

1. Validate `--games`/`--repeat`/`--jobs` ≥ 1, `--races` against `Race`, and `--maps` against `installed_maps()` before launching.
2. Exit 1 when any row is `Crash`; guard the win-rate division.

Test: `--games 0`, `--races Foo`, `--repeat 0` give clean usage errors; synthetic `Crash` rows make the exit code 1.

### 34. Run identity and scenario keys can collide — Medium / Confirmed

**Locations:** [scripts/benchmark.py](scripts/benchmark.py), run directory naming;
[scripts/run_scenarios.py](scripts/run_scenarios.py), `runs`, `replays`, `futures`.

Run directories use timestamps with one-second precision. Two invocations in the same
second collide at `mkdir()`. Repeated positional scenario names create identical `run`
tuples, so the futures dictionary overwrites an earlier entry even though its task was
already submitted. Those tasks also share a replay filename.

**Opportunity:** use a unique run ID and per-execution sequence ID; either reject duplicate
names or support them explicitly. Keep display labels separate from execution identity.

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real: Low.

- `run_scenarios.py` keys runs by `(play, repeat index)`: `run_scenarios.py tank_dive tank_dive` gives two equal keys. Both games run, but the futures dict keeps only the last, so the first report is never printed or written, and both write the same `.SC2Replay` path.
- Run directories use a seconds timestamp and `mkdir(parents=True)` without `exist_ok`: a second invocation in the same second raises `FileExistsError` before any game starts (loud, scripted loops only).
- `--repeat`, `--seeds` and `--side both` give distinct keys: the common paths are fine.

Proposed fix (tooling only): dedupe or reject repeated names; add a pid/counter suffix to run directory names.

Test: argument planning for `tank_dive tank_dive` gives an error or one run; two same-second `mkdir`s get different directories.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real and reproduced, but only leaks empty temp directories on the developer machine, never on the ladder: Low.

- Probe: importing `scenarios` left one new temp directory, from `CustomDecisions.__init__` (`tempfile.mkdtemp()`, spot-checked) via the module-level registry in `scenarios/decisions.py`.
- The runner spawns a fresh process per scenario (`spawn`, `max_tasks_per_child=1`), and seed probes and `--list` import it too: one leaked directory per process. It's empty unless `custom_decisions` runs (then it keeps its `.jsonl`); nothing deletes it.
- The ladder zip ships `bot/` only; `tests/` never imports `scenarios`.

Proposed fix (tooling only): create the directory lazily in `recorder()` (or a `TemporaryDirectory`), and delete it after `check` has read the file.

Test: importing `scenarios` leaves the temp dir count unchanged; running `custom_decisions` creates and removes its own directory.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real: Low.

- `StructureAssault`, `CrampedMain`, `OpenArmy` and `WalledMarines` each hand-write the spawn/clear/spawn-attackers/reset/fight phases.
- `ScenarioBot.spawn` sends one `debug_create_unit` per call: ~5–20 calls once at setup, not measurable against a 60–180 s game. Tech-lab retries and next-step barriers must stay.
- `layout()` is recomputed in several phases (cheap, same result). The `except TypeError: pass` around `ReachMap(...)` in `CrampedMain.on_step` can now only hide a real bug. `OpenArmy` sets `army.debug = True`, already done by `ScenarioBot`.

Proposed fix (tooling only): `spawn_all(specs)` for barrier-free batches; cache `layout()`; drop the `try/except TypeError` and the redundant debug flag; optionally a shared phase helper.

Test: `efficiency_cramped_main` and `wall_bunker` before/after with the same seeds give the same numbers.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real: Low.

- Reproduced: after `close()`, another `record()` reopens the file with `"w"` and truncates it (4 lines became 3). No production caller does this (`ZaszBot.on_end` closes once).
- `benchmark.py`'s `play_game` never closes the recorder, so a crashed game has no `end` record; `read_records` represents that as `result is None` (tested), a designed state.
- Buffered tail loss needs a hard kill: `os._exit` left a 0-byte file; a normal exit without `close()` flushed it.
- `CustomDecisions.check` reaches into `bot.recorder._file.flush()`.

Proposed fix (recording/tooling): a `_closed` flag so `_write` after `close()` is a no-op or error; a public `flush()`; `close("Crash")` in `play_game`'s `finally`.

Test: record, close, record again leaves the file unchanged (or raises); `flush()` makes lines readable without `close()`.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real and reproduced, but nothing consumes recordings yet: Low until training code exists.

- Probe: `DecisionRecorder(meta={"version": 99, "type": "x", "features": ["bogus"]})` wrote a header with those values (`header | self.meta` lets metadata override structural keys); `read_records` then didn't recognise the header and `header["version"]` raised `KeyError`.
- `read_records` ignores `version` and `features`; `Observation.from_dict` fills missing features with defaults, so a renamed feature silently becomes zeros, while `Decision.from_dict` requires every key.
- A truncated last line raises `JSONDecodeError` (loud, so not "silent acceptance"), but without file or line context.

Proposed fix (offline code only): structural keys win when building the header; `read_records` checks version and feature names; report file/line on decode errors, with an opt-in to skip an unterminated last line; optional streaming `iter_records()`.

Test: recorder tests for a reserved-key override and a truncated tail.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real, latent: Low.

- `ScriptedDecisions.decide` passes a fresh `dict(ARMY_COMPOSITION)` each step, so nothing is aliased today.
- Reproduced for a future policy that reuses a dict: mutating the `army` dict of a recorded `Decision` and recording it again wrote no second step (the recorder compares against the same mutated object).
- `Decision(army={PROBE: -3.0, ZERGLING: 5})` is accepted; `to_vector()` drops PROBE and keeps 5.0 (no sum check). `spawn_composition` ignores non-positive shares. Priority follows dict order in `spawn_composition` but `ARMY_UNITS` order in the vector (they agree with one unit type).

Proposed fix (offline code; changes behavior only for invalid decisions): `Decision.__post_init__` copies `army` into a read-only mapping and validates keys, non-negative shares and upgrades; explicit priority order in `spawn_composition`.

Test: a bad `Decision` raises; mutating the source dict doesn't change the object; the recorder notices real changes.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real, but mostly a documentation contradiction: Low.

- `ScriptedDecisions.should_attack` keeps `self.attacking` (hysteresis): between `STOP_ATTACK_AT_ZERGLINGS` and `ATTACK_AT_ZERGLINGS` the same observation can give either answer, and `Observation` has no previous-attack field.
- README says decisions are "a pure function of the observation"; the `Decisions` protocol docstring says a policy "may keep its own state". Recordings write `attack` on every change, so replay as a sequence still works; only independent per-sample training loses information.

Proposed fix: fix the README/module wording (docs only); optionally append an `attacking` feature at the end of `Observation` (changes the recorded schema, see 38). Keep the hysteresis.

Test: replaying a recorded sequence through a fresh `ScriptedDecisions` reproduces the recorded `attack` values.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real; Low is fair.

- `sys.path` setup is hand-written in five places (`run.py`, `tests/conftest.py`, `scripts/benchmark.py`, `scripts/run_scenarios.py`, `scenarios/seeds.py`). `bot/decisions/__init__.py` and `scenarios/__init__.py` rely on late imports (`noqa: E402`) because of circular imports.
- First-party code imports `numpy`, `scipy`, `loguru`, `aiohttp`, `yaml` and `map_analyzer` directly, but `pyproject.toml` lists only `ares-sc2`: they arrive transitively, so a change in ares' dependencies could break Zasz.

Proposed fix (tooling only): one bootstrap helper for scripts/tests/seed probe (leave `run.py`'s insert, the zip relies on it); declare direct dependencies and re-lock; move `Build`/`Decision` into `bot/decisions/types.py`.

Test: pytest, `run_scenarios.py --list`, and a ladder archive build with unchanged contents.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** partly real: Low–Medium. The extra binaries are small; the non-atomic write is the part worth fixing.

- The current `Zasz.zip` ships four `sc2_helper` `.so` files (Python 3.10–3.13, ~790 KB each) and three `map_analyzer` extensions; only the 3.12 ones are needed (~2.75 MB of 28 MB uncompressed, most of which is the 22 MB `cython_extensions` bootstrap). `sc2_helper/test.py` is shipped too.
- `main()` opens `ZipFile(out, "w")` (truncating the old zip) and does the network download last, so a network failure leaves a partial archive in place of the previous one.
- `urlopen` has no timeout and no cache; `next(...)` raises a bare `StopIteration` if no wheel matches; the wheel match doesn't check the CPU architecture (network not probed). `assert not config["Debug"]` disappears under `python -O`.

Proposed fix (packaging only): write `Zasz.zip.tmp` and `os.replace` after the download succeeds; match `cp312` + `manylinux*_x86_64` explicitly with a timeout and a clear error; replace the `assert`; skip non-3.12 `.so` files (after checking the ladder's loader).

Test: build into a temp path; exactly one 3.12 `sc2_helper` and `mapanalyzerext`; `run.py`, `ladder.py`, `config.yml` present; the archive's `bot`/`ares` import in a clean interpreter. Don't upload.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real, and more is stale than listed. Low for the code, but a reader following the docs today would be misled.

- `README.md` lists `bot/army_plan.py`, which no longer exists (spot-checked), and omits `bot/squads.py`; its Strategy paragraph still describes the army moving as one; it says tests run in "< 1 s" (79 tests, ~2.7 s); "decisions are a pure function of the observation" (see 40); the benchmark table stops at `1cc08d0`.
- The `bot/army.py` docstring says ares groups units into squads (it's `SquadBook`). `docs/progress.md` ("Current state, 2026-10-01") still calls merged branches unmerged and names `STRUCTURE_FILL` (now `TARGET_FILL`) and `army_plan.py`.
- A comment in `bot/decisions/scripted.py` says `pool_wanted` stays True after the pool is ordered; the code returns `not obs.pool_ordered and ...`.
- `scripts/benchmark.py` docstring calls `results.csv` gitignored (it's tracked and union-merged); `create_ladder_zip.py` mentions a "Zasz-ML folder".
- None of the three `experiments/` patches applies any more (`git apply --check`).

Proposed fix (docs/comments only): correct the items above; mark `docs/progress.md` historical with a short current-state section; an `experiments/README` with one line per patch ("does not apply since squads").

Test: no `army_plan`, `STRUCTURE_FILL`, `Zasz-ML` or "gitignored" outside historical notes; the README layout matches `git ls-files bot/`.

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

**Verification (2026-10-05, commit `810a6e6`; not applied):** real; Medium is fair.

- 79 tests pass in ~2.7 s; of `bot/army.py` only `armed`, `dps_vs`, `reach_of` and `weapon_range` are tested.
- No offline coverage of: `Army.control`/`fight`, the route cache (03), squad-scout cleanup (02), repicks (07), `pick_tiered` (16), `Decision` validation, the recorder lifecycle, anything in `scripts/`, or `create_ladder_zip.py`. Three test files define their own fake units.

Cheap tests to add (offline, no SC2): `benchmark.py` `is_notable`, `over_ladder_limits`, matchup planning (28) and the header upgrade (30); `run_scenarios.py` `compare()` and duplicate names (34); `create_ladder_zip.add_tree` on a temp tree; recorder after `close()`, reserved header keys, truncated tail (37, 38); `Decision` validation (39); `pick_tiered` with the existing `ReachMap` fixtures. More work: `Army.fight` with a fake `ai`; scout-plan cleanup after a squad dies.

Proposed fix: tests only, no behavior change; a shared `tests/fakes.py`; keep tests deterministic and the suite under ~5 s.

Test: break the covered behavior temporarily and confirm each new test fails.

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
