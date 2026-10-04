# Notes: zergling blocking (`blocked-zerglings`), then squads (`squads`)

**Latest (Oct 2-3, branch `squads`, sections 18-26):** the single army is replaced by squads with jobs
(patrol / raid / push / help / strike), plus breach, the wall and bunker scenarios, a debug overlay
(`--overlay`) and seeded comparisons (`--seeds N`). All scenarios pass except `wall_bunker` and parts of
the disengage test; the Hard benchmark of the latest commit (`26301e0`) is still to run, headless.

**Summary (for when you're back):**
- `efficiency_cramped_main` (5x3 gateway grid, 4 zealots, 4 stalkers): **25/25** in a row (73-108 s).
- `efficiency_cramped_more_zealots` (+2 zealots in the middle row): **10/10** (78-112 s). The army
  only takes this fight when the scenario sets `Army.committed`: the combat sim calls it a loss and
  the bot otherwise never attacks (section 14; your call whether the real decision should change).
- The big wins: walking around enemy units to reach them (section 11) and not trading 1-on-1 with a
  zealot plugging a gap (section 12). Everything that was tried and dropped is logged below.
- `fight_winnable` fixed (section 17): it broke on this branch (first at `4ed0545`, every time from
  `748f1c9`), not before it. "Only fight fighters that can hit you" is replaced by "fighters within
  a 10-tile *walk*", plus two chase fixes. Full suite 30/30.

Running log of what was tried against the packed-gateway scenario and what happened. Newest at the
bottom. Goal: `efficiency_cramped_main` 25/25, then with extra zealots in the gateway gaps 10/10.

## The test: `efficiency_cramped_main`

`scripts/run_scenarios.py efficiency_cramped_main --repeat N`

- 5 wide x 3 deep grid of gateways (3x3) with 1-tile gaps, just past the top of the enemy's main ramp
- 4 zealots in the 1-tile gaps of the back row, 4 stalkers behind the back row (held in place)
- 48 zerglings start below the ramp; pass = every unit and gateway dead within 180 s
- start locations are random, so runs alternate between the two mains (the two sides behave
  differently: different ramp angle, different room around the grid)

Reported per run: kill time, dmg/s, efficiency (damage potential that landed) split into blocked
(out of range, not getting closer) and idle (in range, not swinging; ~24% is the melee wind-up
baseline).

## Starting point (before this branch)

From a VeryHard game the user watched: 200+ zerglings in the enemy main, game-end efficiency 19%
(blocked 71%). Screenshots: hundreds of zerglings tunnelling on pylons while gateways next to them
were untouched; zerglings stopping at the first enemy at the top of a ramp and jamming everyone
behind.

## Log

### 1. Tiers with room + fill from the back for units + block detection (commit before WIP)

- Target choice in priority tiers, taking the best-tier target that still has room (melee slots:
  structures part of a ring, units a full ring). A full pylon no longer holds up the gateway next to
  it.
- Fill from the back for units too: the first zerglings in take the deepest target, so they run past
  the front line instead of stopping at the first enemy.
- Only stop for *your own* target (no "attack whatever is in range").
- `BlockTracker`: out of range and not 0.5 closer within 1.5 s = blocked on that target for 6 s.

Old easy layout (stalkers in front, a few buildings behind): old code 42-52 s, blocked 24-33%; new
34-39 s, blocked 13-14%.

5x3 gateway grid (user's harder layout): 1/2. The failure: with fighters close, buildings weren't
targets at all, so zerglings blocked from the zealots in the gaps had nothing to do but try to flank
while the stalkers shot them, until too few were left.

### 2. Break through the structure in the way when every close fighter is full/blocked

2/2 passed (134 s, 144 s; blocked 7-9%). But the user watched a failure in a longer set: zerglings
that fell back to a gateway kept hitting it (stickiness: keep the current target while it has room)
and never switched to the stalkers they could have reached by going around.

### 3. Detours around our own crowd + stickiness only within the best reachable tier (WIP)

- `Detours`: a zergling blocked on an enemy unit plans a path to it on a copy of the ground grid where
  friendly zerglings near it are expensive and its jammed spot very expensive, so the path goes around
  (e.g. outside the gateway grid); follow it for up to 6 s. Break through only with no reasonable
  detour.
- Keep the current target only if it's in the best tier that has a reachable target with room.

Result: 0/4, much worse (blocked 52-71%, 3-16 dmg/s in three runs). Investigating.

### 4. Instrumentation (Army.debug counters, logged every 3 s by the gateway scenario)

The 0/4 with detours: zerglings almost only targeted units ("unit" ~1100 per 3 s) but only 2-15% were
in range; 48 fell to 21 in 24 s and no target died. The zealots in the gaps plug the 1-tile lanes, but
pathing ignores units, so every stalker behind the grid *looked* reachable.

### 5. Reachability map + surround-only unit targets + safe-first clearing

- `ReachMap` (bot/targeting.py): BFS over the ground grid around the fight with enemy units as
  obstacles, from every zergling. An enemy unit is a target only with >= 3 reachable open cells
  around it (or touching us); its melee slots are capped by those cells.
- Fighters around but none surroundable: clear structures, those hittable from outside enemy range
  first (front gateways before the back row next to the stalkers).
- Structures hittable from outside enemy range stay targets even inside an avoided area.

1/4 then 1/2. A clear pattern: losses barely differ between passes and fails (~900-1100 damage
taken); fails are what happens *after* the sim calls a retreat at ~0:30: the split-up plan took over
and the leftovers stalled next to the grid.

### 6. Scenario fixes (the test itself was wrong on one side)

- On the side where the ramp is diagonal, the rotated grid snapped its 3x3 footprints onto whole tiles
  and the 1-tile gaps closed into a solid wall. The grid is now aligned to the map axes.
- A zealot planned for a gap against a cliff edge got pushed to (30, 104), far outside the main:
  every run on that side ended "1 of 23 alive". Unit spots are now checked for walkable ground and the
  setup fails loudly if a unit isn't where planned.
- Bot bug found through it: the flying keep-alive barracks counted as an attack target, pulling idle
  zerglings to the map corner. Targets are now only things our army can hit; with no hittable
  structure the army hunts known enemy ground units.

With a valid scenario on both sides: 2/4 (passes in 133-134 s).

### 7. What actually happens on each side (reach logged at the start of the fight)

| Side | Stalkers | Zealots |
|---|---|---|
| top (enemy main y~130) | unreachable: 0 open cells (grid + cliff seal them in) | 1 open cell each |
| bottom (y~45) | reachable around the grid: 6-9 open cells | 2-5 open cells |

So the two sides need different play: on top the only way to the units is through gateways; on the
bottom the stalkers can be surrounded from behind the grid. A single fixed priority order helps one
side and hurts the other.

Baseline for reference: the bot before this branch (`alternate-targets`) fails the grid 0/4, killing
2 of 22-23 targets per run. The versions below kill 7-21.

### 8. Things tried in this round (each 4 runs unless noted)

- "No free damage" for structures (only hit structures from outside enemy range while fighters are
  around; step out of range otherwise): zerglings cleared every safe gateway with no losses, then
  waited outside forever (15/22 alive every time on the top side).
- Safe first, then commit to exposed structures: near misses (2-3 gateways left) but zerglings walked
  home: the attack stopped below 8 zerglings even with no enemy army left. Fixed in the decisions
  (keep attacking with a few zerglings when no enemy army is known); real-game relevant too.
- Terrain level: zerglings below the main's cliff were "in range" of gateways on top of it (straight
  line) and stood there for minutes. Reach map and block detection now compare terrain levels.
- Work order with fighters around: touching fighters, safe structures, surroundable fighters,
  workers, exposed structures. Survival much better (48 -> 30 at 0:57 instead of ~14) but slow kills.
- ares' danger grid pads every weapon range by 4 tiles (RangeBuffer), so in a cramped base almost
  everything looks unsafe. Exposure for target choice is now computed from actual weapon ranges.
- The combat sim judged each small ares squad against the whole enemy group, so squads retreated
  from fights the army as a whole wins. Fights are now judged with all our army units near the threat.
- Focus fire when breaking through under fire: everyone on the exposed structure closest to the enemy
  fighters (most damaged first) to open a hole fast.

### 9. More experiments, all worse together, then a reset (lesson: change one thing at a time)

Measured on the corrected scenario, 6 runs each unless noted:
- fighters-first vs safe-structures-first order: 0/6 both (~15 left on average)
- reference: targeting from `856dc28`: 0/6, but 4-5 left in half the runs (wide spread: early losses
  snowball, so small samples can't separate versions)
- coordinated arrival ("Assault": gather at the last safe cell, go in together): 0/6, worse on the
  bottom side (zerglings waited instead of fighting)
- damage attribution (debug: damage taken per decision): the losses were stalker shots (13 each) on
  zerglings working gateways I'd classed as safe (only the side facing the zergling was checked)
- whole-ring safety for structures: 0/6, ~19 left (too conservative: nothing left to do safely)
- never retreat at all: 0/6, ~15 left, so retreats weren't the bottleneck
- real utilization (debug: % of zerglings actually swinging): peaks at ~45% while the front row
  falls, then 15-20% while zerglings die: the rest had no target with room and crowded full targets
  inside stalker range
- no crowding (wait out of range when everything in reach is full) + 3/4 of a ring per building:
  0/6, ~17 left

Everything above is in commit `5b18f41` for reference. Reset: targeting back to `856dc28`, plus only
the clear bug fixes (terrain levels for reach/blocking, fights judged with all our nearby units, the
attack continuing with a few zerglings when no enemy army is known):

**3/8 passed (92 s, 121 s, 129 s), three more with only 3-5 targets left.** New baseline. From here,
one change at a time, 8 runs each, kept only if it beats this.

### 10. One change at a time from the reset (10 runs each, split by side)

- **C: 3/4 of a ring per building** (`MELEE_SURROUND_FRACTION` 0.75): 4/8, but catastrophic failures
  on the bottom side.
- **C + D: only fight the fighters that can hit you** (within their weapon range + 2), not every
  fighter within 10: top 2/5 (3.6 targets left on average), bottom 2/5 (5.0). Committed (`748f1c9`).
- **Clear-the-way range 10 -> 20** (idle zerglings near the end should find the last gateways): 0/10,
  12-14 left. Reverted.
- **C + D re-run** to check the baseline: top 3/4 (passes in 100-108 s), **bottom 0/6** (16 left,
  twice 22 of 23). The two sides really are different problems, and samples of 4-5 per side are
  noisy.

Why the bottom side collapses (logged run): 22 of 48 zerglings die in the first 20 s while only one
gateway falls. About 13 zerglings at a time are going for units and about 11 "breaking through"
gateways inside stalker range. Then the sim calls a retreat (LOSS_MARGINAL) and the remaining 22-26
flip between retreat and fight for 2.5 minutes, doing nothing.

The stalkers on that side can be reached around the outside of the grid (6-9 open cells each), but
SC2 pathing ignores units: a zergling ordered to attack a stalker walks the shortest way, down a
1-tile lane between gateways, and gets stopped by the zealot plugging the end of it, inside stalker
range. Detours only kick in after it has been stuck for 1.5 s, and most die before that.

### 11. Walk around enemy units to reach them (the big one)

`ReachMap.approach_step`: per target, a distance field (BFS outward from the open cells around the
target, with enemy units as walls, cached with the reach map for 1 s). A zergling going for an enemy
unit that isn't in range yet steps down that field (a move 4 cells ahead) instead of getting an
attack order, so it goes around the outside of the grid instead of down the lane a zealot plugs.
Once it's on a cell next to the target it gets the normal attack order.

**9/10: top 6/6 (81-125 s), bottom 3/4 (110-125 s; the failure had 6 left).** Up from 3/10.

Repeat, 16 more runs: **15/16** (top 10/10 in 81-96 s, bottom 5/6 in 114-156 s). Together **24/26**:
top 16/16, bottom 8/10. The approach fields first used a pure-Python BFS: 125 ms for 8 targets in
one step, far over AI Arena's 40 ms per game loop. Now one multi-source Dijkstra per target on a
cell graph built once per reach map (scipy.sparse.csgraph): 6.5 ms for 8, graph build included.

Both remaining failures are on the bottom side and look alike: ~23 of 48 zerglings die in the first
30 s (about 10 at a time "breaking through" the gateway next to them, inside stalker range, because
every stalker slot was full), then slow attrition next to the back-row zealots, ending with 4-6
targets (zealots plus the gateways beside them) alive.

### 12. After the approach fix: the bottom side's leftovers

- **F: fighters all full -> clear safe structures within 10 instead of the one within 3**: top 9/9,
  bottom 2/7. Worse. Reverted.
- Debug log now also lists the enemies we see (by type) every 3 s. That showed the real loss on the
  bottom side: the stalkers are all dead by 0:24 (48 -> 31 zerglings), and then **15 zerglings die to
  the four zealots on hold in the back-row gaps** in the next 30 s. Those zealots survive to the end
  (the "2-7 left" failures are zealots and the gateways next to them). A zealot in a 1-tile gap has 2
  open cells; zerglings touching it fought it there ("hit back whatever touches you"), 1-2 at a
  time, and lost every trade.
- **G: step out of reach of a melee unit plugging a gap** (touching, < 3 open cells) instead of
  hitting it back; ranged units right next to us still get hit back. **15/16: top 6/6, bottom 9/10**,
  and bottom-side passes went from 110-156 s to 88-102 s (zealots now get swarmed once the
  gateways beside them fall). Kept.
- G's one failure: everything dead except one zealot, and 19 zerglings stood idle for over a minute.
  A lone zealot isn't a "threat" until something is next to it, and with no structures left the
  squad attack-moves to the target with ares' `AMove`, which does nothing within 7 of its target by
  default. The same trap was fixed earlier for remembered units; the close-in attack-move now goes
  all the way in. Real-game relevant: a last unit hiding at the edge of a base would have been
  left alone.

### 13. 25/25

G + the attack-move fix, 25 runs in a row: **25/25** (top 13/13 in 73-88 s, bottom 12/12 in
89-108 s; efficiency 65-69%, blocked 6-11%). For comparison, the bot before this branch killed about
2 of the 23 targets.

What made the difference, in order of impact:
1. Walking around enemy units to reach them (distance field with units as walls). SC2 pathing
   ignores units and sends zerglings down lanes that zealots plug.
2. Not trading 1-on-1 with a melee unit plugging a gap: step out of its reach, clear the buildings
   beside it, swarm it once it has room around it.
3. Only fighting fighters that can actually hit you; 3/4 of a ring per building.
4. Bug fixes: terrain levels in reach/blocking, fights judged with all our nearby units, the attack
   continuing with a few zerglings when no enemy army is left, AMove doing nothing within 7.

What didn't help: work orders, gathering before going in, focus fire, wider clear-the-way ranges,
safe-only targeting, waiting for free slots (all in `5b18f41` or reverted).

## Phase 2: two more zealots (`efficiency_cramped_more_zealots`)

Same grid; 6 zealots: the 4 back-row gaps plus the 2 center gaps of the middle row.

### 14. First try: 0/10, and the army never really attacked

Bottom side: 25 of 25 targets alive. The whole-army combat sim calls 48 zerglings vs 4 stalkers + 6
zealots a loss, so `avoid_armies_we_cannot_beat` marks it as an area to avoid and the army plan
holds at home all game. Top side: the plan flips between fighting and staging, then squads retreat
on the sim's call (LOSS_MARGINAL/LOSS_CLOSE).

That judgment isn't crazy: in an open field 6 zealots + 4 stalkers do beat 48 slow zerglings. What
the sim can't see is that the gateways let zerglings clear buildings for free and take on the units
a couple at a time. This test is about executing the fight, not deciding whether to take it, so I
added `Army.committed` (attack, ignore avoid areas and sim retreats), set **only by this scenario**.
The 4-zealot 25/25 runs don't use it. **Your call:** whether the real decision should get smarter
here (e.g. discount enemy units that are walled in or can't reach us) is a separate question.

### 15. Committed: 10/10

**10/10** (top 6/6 in 78-89 s, bottom 4/4 in 88-112 s), with no targeting changes beyond phase 1.
Once the army actually takes the fight, the two extra zealots in the middle row are handled like the
others: step out of their reach, clear the gateways beside them, swarm them once they have room
around them.

### 16. Full scenario suite on the final code

13/15. Of the two failures:
- `fight_winnable` (6 zerglings vs 2 marines; 0 damage dealt, they never engage) fails the same way
  on `856dc28`, the code this branch started from, so it isn't caused by this branch. Cause not
  investigated yet.
- `tank_dive` failed once ("never engaged") and passed when re-run: flaky, worth a look.
The other efficiency scenarios (open field, deep base, ramp) all pass with no damage taken.

### 17. `fight_winnable` regression: found and fixed

Bisect (3 runs per commit): fine through `alternate-targets` (`3352be6`: 9/9), flaky from `4ed0545`
(5/9; still 4-6 of 6 through `3aa0451`), failing every time from `748f1c9` (0/6) to the tip (0/8).

Why (logged per zergling): 6 zerglings vs 2 marines in the open, sim says VICTORY_DECISIVE, but
1. `748f1c9`'s "only fight fighters that can hit you" (within their range + 2): half the
   zerglings were 8 tiles from the marines, just outside, so the marines weren't targets; with no
   structures to clear they got **no order at all** and stood idle while the marines walked off.
   The other three alone were a predicted loss and retreated. Bad design (user): replaced.
2. `BlockTracker` called chasing a moving target "blocked" (not getting closer), so chasers got
   flank/detour moves instead of attacking (the same mistake `6a51dd5` fixed in the metric).
3. "Approach around" sent plain move steps even in the open, where an attack order does the same
   and keeps following a target that moves.

Fixes:
- **Near fighters = touching, or within a 10-tile walk** (`ReachMap.walk_distance`: the walking
  distance to a spot next to the target with enemy units as walls, a Dijkstra capped at the
  limit). Keeps the original point: a stalker behind the gateway grid is a long way round.
- `BlockTracker`: while the target itself moves (`TARGET_MOVED` 1.0), it's a chase, not blocked.
- `approach_step` only walks around when that's more than `APPROACH_DETOUR_SLACK` (2) longer than
  straight in; otherwise a normal attack order.

Results: `fight_winnable` 12/12; `efficiency_cramped_main` 32/32 (top 14, bottom 18);
`efficiency_cramped_more_zealots` 12/12; full suite (`--repeat 2`) 30/30, `tank_dive` included.

Cost: `efficiency_cramped_more_zealots` bottom side is slower than with the old rule (about
105-120 s and ~900-1000 damage taken, vs 87-93 s and 725-835): zerglings now go for the stalkers
around the outside of the grid (within a 10-tile walk there) instead of clearing gateways first.

Tried and dropped: also requiring the walk to be "no detour" (walk <= straight + 2) plus a last
resort tier with any other surroundable fighter. Bottom side back to 85-97 s, but the top side of
`efficiency_cramped_main` fell to 5/8: a slower clear beats a failed one.

## Phase 3: squads (branch `squads`, off `scenario-seeds`)

Squads replaced the single army and `ArmyPlan`. Everything is measured on zerglings lost first, speed
second.

### 18. Groundwork: open-field comparison, shared slots

- `scenario-seeds` (your branch) adds fixed seeds per side, `benchmarks/scenarios.csv` and `--realtime`.
  At normal speed the gateway grid passes from both sides; in real time the top side lost 38 and timed
  out. Ladder games aren't real time, so normal speed is what counts.
- New `efficiency_open_army`: the grid's army in the open, held in place. The first runs looked worse
  than the grid, but the enemy wasn't actually held: the AI pulled it back up its ramp (my hold only
  re-issued when units had other orders). With the enemy held every step: **open field 9 / 14 lost (top
  / bottom), against 17 / 21 in the grid**, so the gateways cost about 7-8 zerglings. The "pull back to
  the ramp" behavior is kept as `efficiency_open_army_free`, a disengage test.
- You saw zerglings dive one stalker at a time, about 20 on each. The cause: target slots were counted
  per squad, each squad starting from zero. **Shared slots across squads:** open army 17 -> 12 lost,
  blocked 38% -> 14%.

### 19. Squads: the design

- `bot/squads.py`: lasting squads of 8 (they join, merge and split), each with a job: patrol, raid,
  push, help, gather, engage or defend. `ArmyPlan` is folded in: patrol replaces scout/watch/hold, and
  help replaces group-up.
- **Patrol:** goes where *our own units* have had no vision longest (SC2's visibility is useless with a
  revealed map), moving on once there.
- **Raids:** enemy sites (structure clusters, or an army in the field) get as many zerglings as the sim
  says they need for a comfortable win, gathering out of range first.
- **Help:** every fight is re-checked every 0.5 s. A losing one gets the fewest free squads that win
  together (two squads that only matter together both go), capped by the room around the targets
  (surrounded enemies need nobody piling on). Nobody goes if no combination turns it.
- The first runs exposed several bugs: patrols sitting still, help counting idle patrols as fighting,
  raids dropped when a site's key changed, helpers losing their job mid-fight. All fixed.

### 20. Backing off, rallies, breach

- **Back off just past the enemy's real weapon range**, not all the way home. ares' danger map pads
  every range by 4.
- **Rally:** a losing squad with help in reach waits outside the enemy's reach, the helpers come to it,
  and they all go in once most have gathered. `alternate_target` went from 13 lost to 2 (top) and 0
  (bottom).
- **No blind raids on their main:** one squad walking in blind ran into a sieged tank.
- **Breach (your idea):** when enemy fighters are sealed off, find the building that borders both our
  ground and their pocket (`ReachMap.breaches`, scipy region labels). Cost it as the time to break it at
  full damage, times the damage covering those cells, and staff it with exactly as many zerglings as
  fit. This fixed the grid's top side.
- Tried and dropped:
  - a **choke-aware sim** ("count only the zerglings that fit"): it called the grid's top side
    hopeless from 0:06, and they hovered at stalker range (your screenshot);
  - a **persistent breach plan with a flood** (one target per fight, flood the hole when it breaks):
    no gain on seeds, and `wall_bays` got worse (`experiments/breach-plan.patch`).

### 21. Measuring properly

- One fixed seed per side is repeatable but fragile. Just reordering which zergling picks its target
  first swung the grid's top side from 23 to 34 lost. Even the same seed differs by about 8 zerglings
  between running alone and running 3 at once.
- Added `run_scenarios.py --seeds N` (the same N games every time), and compared versions on 4-6 seeds.
- Combat scenarios now get **no reinforcements** (larva and eggs removed every step): a `wall_techlab`
  "win" came from zerglings hatching in the main and walking in.
- A bug found by measuring: a raid's site was keyed by position. When the scenario killed the enemy CC,
  the building cluster's center moved, and 40 zerglings went patrolling for 30-45 s. Raids now follow
  their site by its units and structures, and raid sizing never marks avoid areas; only the whole-army
  check does.

### 22. Walls and bunkers

- New `wall_techlab`, `wall_bunker` and `wall_bays`: 4 marines inside a ring of engineering bays, 24
  zerglings. Debug commands won't spawn an enemy supply depot at all, so the 2x2 weak point is a tech
  lab (the same 400 health and 1 armor). The bunker test keeps the marines loaded, since the AI unloads
  it.
- Bunkers weren't threats, because SC2 gives them no weapon. They now count as 4 marines at range 6,
  judged in two steps, since the sim doesn't know bunkers shoot. Structures count as "near" by their
  edge, not their center.
- `wall_bunker` still fails: the zerglings that don't fit around a full bunker hit the bays beside it,
  under its fire.

### 23. Strikes

- **Strike (your rule):** an enemy group that's shooting us and can be walked to gets the fewest of the
  nearest zerglings that win, from up to 40 tiles' walk. They gather at the last cell outside the
  group's reach and go in together. This replaces the old 10-tile "fight what's close" for that case.
- Seeds 1-4: walls much better (`wall_techlab` 0/4 -> 3/4, `wall_bays` 2/4 -> 4/4), open army even, grid
  +3.5 lost, `efficiency_open_army_free` 3/4 -> 1/4.
- Dropped: "the strike decides for everyone near its group". Squads that weren't picked stepped away
  instead of fighting, and the open army went from 10 to 16 lost (`experiments/strikes-v2-full.patch`).
- Later fixes from watching the overlay: supporting enemies (tanks behind marines) count in a strike's
  sim, and "already fighting / being hit by that group" skips gathering.
- Found by logging what strikes saw: the tanks revealed by firing were seen, the marines weren't. The
  sim called **2 zerglings against 2 sieged tanks VICTORY_EMPHATIC**, and "fewest that win" sent 2,
  then 6, then 12.

### 24. Hard benchmarks (12 games, 3 at a time, Windows)

| | `main` | squads + strikes | + the list changes |
|---|---|---|---|
| Result | 12/12 | 12/12 | 10 wins, 2 ties |
| Avg game | 525 s | 682 s | 721 s |
| Avg value lost | 1,960 | 6,802 | 12,092 |
| Time per game loop | - | 31.5 ms | 39.3 ms (98% of 40) |

Your question about parallel games: alone, one game used 9.5 ms per loop (24%), so most of the 98% was
3 games sharing the CPU. But a 20-minute stalemate alone still used 29 ms (73%): big armies are costly.

### 25. The list (your requests during the benchmark) and what followed

- **No idle squads:** while attacking, spare squads join a raid or push (natural, then main, then other
  bases) as one group. At most one patrol, and only with 4+ squads (with 24 zerglings, 2 of the 3
  squads had been patrolling).
- **Next-step threats:** threats are what can hit us within about one step (the margin went from 6 to 1).
- **Workers:** raid squads in the middle of a fight aren't released by raid planning.
- **Cornered:** with no reachable ground out of the enemy's reach, fight instead of running into a wall.
- **Reachable destinations only:** patrol spots, sites and push targets must be walkable (mineral
  walls). Bug: a walled site's center is inside the pocket; a site now counts if we can stand next to
  one of its buildings.
- **Overlay** (`--overlay`): squads (colored by job, raids lettered), raids (assigned / needed),
  strikes, breach, avoid areas (reason, time left), back-off lines, scout requests. It found several
  of the issues below.
- **Avoid areas clear when seen empty;** they used to linger 40 s after the army left.
- **Spread out** when moving where we can't see (rows of 6, 2 tiles apart), against splash.
- **Unseen fire:** hit with nothing visible in range, the squad steps back and avoids that spot.
- **Partly seen positions:** a long-range enemy far past our sight (a tank firing from the fog) is
  avoided until scouted, and an overlord is sent (20 s at most). The first version fired on any unit at
  the edge of our vision (units on a ramp top) and kept the army passive; it's now limited to range 7+
  and well past our sight.
- **Hold outside the back-off line** for 4 s after backing off. They used to stutter across the enemy's
  range: out, no longer a threat, back in.
- **Engage / disengage hysteresis:** take a fight at VICTORY_MARGINAL or better, leave at LOSS_DECISIVE
  or worse.
- Last single game, Terran on Ultralove vs Hard: a win in 485 s, 2,450 lost, 8.6 ms per loop. The game
  before it, on older code, was a 20-minute tie with 37,925 lost. Committed as `26301e0`; a full
  benchmark is still to run (headless on Linux).

### 26. Open questions for next time

- Is the combat sim's take on sieged tanks, and on crossing a ranged enemy's range under fire, behind
  many of the bad commits?
- `wall_bunker`: should 24 zerglings take on 4 marines in a bunker at all? Today they mostly lose.
- `efficiency_open_army_free`: chasing an army up a ramp held by zealots still costs a lot of zerglings.

### 27. Status at stop (Oct 3): tank_dive and targeting, work in progress

Committed (`c1a4cbd`): fight_winnable fixed (squads go for winnable fights in sight; strikes already
together skip gathering; patrol spots never seen no longer count as seen at 0 s; patrols use the normal
engage bar). `tank_dive` judged by targeting; overlay priority labels (`1: 6/7`); `--slow X`.

**Uncommitted** (in the working copy, not fully working):
- `tank_dive` stricter: pass only when every enemy unit is dead; fail if the tank holds more zerglings
  than fit while a marine still has room (>0.5 s), or a marine is targeted while the tank has room (>1 s).
- Target capacities settled once per step (`TargetSlots.settled`), so squads with different reach maps
  agree on a target's slots. Fixed the top side.
- "Plugging a gap" redefined (your rule): a unit is a plug only if it blocks our way to other enemy units
  (`ReachMap.plugs`, via `breaches`) and isn't hitting us (range < `PLUG_HARM_RANGE`). A clump of marines
  in the open is a normal target now.

**Still failing:** `tank_dive` bottom: 10 zerglings assigned to the 7-slot tank while marines had room
(0/1, 1/2). The bot itself assigned 10 (not stale orders), so the overflow decision is the cause: likely
the "everything full -> closest in the best tier" fallback in `pick_tiered`, or marines not being
candidates for those zerglings (walk distance > 10 / blocked). Next step: log per zergling why the open
marine wasn't picked.

Also still open: the choke case (3 zealots on a ramp with stalkers behind: don't take it even if the sim
barely says win; see todo #21).

### 28. Oct 3, afternoon: group decisions, unseen fire, chokes, breach (status at stop)

Committed earlier today: `223817d` removed the 10-tile fighter rule (`FIGHTER_PRIORITY_RANGE`). Every
fighter in a fight is a target candidate: whether to engage, and with whom, is decided for the group
(squad verdict, strikes, raids), not by each zergling. `efficiency_open_army` had stood idle 120 s with
"go for a winnable fight" logged and no orders; now 15 s, 10 lost.

This commit:

- **Unseen fire, rebuilt** (your design). A hit counts as unseen only if no enemy seen in the last second
  could have dealt it (weapon reach to the damaged unit, no distance limit, enemies that just died
  included). Visible shooters are refreshed when the damage event arrives, so a tank revealed by the shot
  counts as seen. Unseen hits are kept 8 s, grouped into circles (pink on the overlay, damage and hit
  count). A force retreats only on 2+ unseen hits around it adding up to 25% of the health of our
  zerglings there. Meant for cloaked/burrowed attackers. The old one-hit rule is gone: a zealot's last
  hit as it died had marked a 14-tile avoid area and idled the gateway-grid cleanup for 45 s.
- **Threat lookup** (`THREAT_LOOKUP = 18`, was `ENGAGE_RANGE = 14`): a sieged tank hits from ~14.5, so
  squads never saw one firing near max range. `can_reach_soon` still decides what can hit us. Fights
  worth going for are noticed among visible armed units within `NOTICE_RANGE = 14`.
- **Fight membership** (`Army.members`): a squad's fight is judged with its actual members (the squad it
  helps and its helpers, squads gathering with it, squads raiding the same site, squads whose last fight
  shared enemies, strike zerglings on these enemies, queens there), not everyone within 26 tiles.
- **Choke fights** (your option b, `choke_worth`): a melee unit with at most 2 open sides is attacked
  only if the zerglings we'd lose killing it (that few at a time, under the fire of everything covering
  it) are worth no more than its cost in zerglings. Otherwise step out of its reach and work the
  buildings around it. A zealot in a 1-tile lane is never taken, and is swarmed once it's opened up.
  Shooters are never refused (refusing a stalker doesn't stop it shooting). Overlay:
  `choke: lose ~N, worth M (take/skip)`.
- **Nothing inside enemy range except the breach**: the "clear the way" and "break through" fallbacks
  only take buildings we can hit from outside enemy range (`safe_structures`, was `by_safety`, which
  only sorted safe ones first). Overflow with nothing safe to do holds just outside their reach
  (`hold for the breach`). A squad whose zerglings all come up empty (bad chokes only, or no breach worth
  it and nothing safe) leaves: avoid area, freed for other jobs (your choice: refuse the bad trade). Not
  for committed fights or near home.
- **Breach, committed and priced once**:
  - The army keeps its breach while it's acceptable. Another must be >20% cheaper to replace it
    (`BREACH_SWITCH_SAVING`). It lapses after 10 s unconfirmed.
  - Priced once per fight for the whole army (`evaluate_breach`): a reach map from all our army units
    within 30 of the enemy fighters, cached per REACH_SECONDS. Before, each squad priced breaches from
    its own side, so squads on opposite sides flipped the shared breach right/left (what you saw).
  - Enemy range is measured to the attack cells (`ReachMap.attack_cells`: the reached cells our
    attackers stand on), not the gateway edge + 1.5. A stalker reaching only the far corner no longer
    counts.
  - Debug log: one `breach eval:` line per candidate (cells, who covers them, time, cost, acceptable).

**Results** (watched, overlay, Windows, one at a time):

| Scenario | Result |
|---|---|
| `efficiency_cramped_main` top | **PASS**, 152 s, 28 lost. Breach held on the right gateway 1000 -> 0, no switches; strikes then killed the stalkers |
| `efficiency_cramped_main` bottom | PASS, 134 s, 29 lost |
| `efficiency_open_army` top | PASS, 14 s, 10 lost |
| `efficiency_open_army_free` bottom | PASS, 70 s, 33 lost (28 in the earlier run today) |
| `tank_dive` top | PASS, 9 s, 4 lost |
| `efficiency_cramped_more_zealots` top | **FAIL**, 36 lost, stalkers alive, 7 of the last 12 zerglings idle |

Earlier runs today of the top-side grid, before the breach fixes: FAIL with 33-43 lost. The losses came
from zerglings working gateways inside stalker range while no strike formed.

**Open, next steps:**
1. `efficiency_cramped_more_zealots` top regressed. It passed 10/10 before today; not yet run with
   the debug log, so which of today's changes did it is unknown.
2. Breach acceptability flickers: in the passing run, 115 of 143 evaluations said "not acceptable"
   for the same gateways at the same cost. Probably the survivor/win check, as zerglings move in and out
   of the 30-tile view. The breach still held, because the commitment only lapses after 10 s, but a
   step with no acceptable breach gives the zerglings no breach.
3. A patrol inside a raid: a squad freed by "leave" becomes a patrol in the middle of the site, is
   judged alone (not a raid member), and marks "no help possible" avoid areas. Proposed: (a) a raid
   squad that refuses a choke stays on its raid and works the rest of the site; (b) a patrol fighting
   at a raid's site joins the raid.
4. `tank_dive` bottom still overfills the tank while a marine has room (section 27).
5. Cloaked units: we have no detection (no lair/overseers). Unseen fire now only retreats on sustained
   damage, but detection is the real answer (todo).

### 29. Oct 4: safe cells, active enemies, open chokes up first

The 6-zealot gateway grid (top) went from FAIL (36-48 lost, no breach ever accepted) to **PASS: 105 s,
23 of 48 lost**. Watched runs with the new per-zergling overlay labels drove each change:

- **Per-zergling labels** (overlay): squad, decision this step and why. Examples:
  `S3 unit: zealot@42,132 [fighter 2/2]`, `safe work: gateway@.. [2/4] walking to its cell`,
  `step away from zealot@.. (open it up first: gateway@..)`, `hold outside their reach`, `strike | ...`.
- **Safe-cell model** (`ReachMap.set_danger`, `safe_attack_cells`, `safe_step`; `safe_work`,
  `work_from_cell`):
  - The old check tested one point on the building's edge facing the zergling, on ares' danger map. A
    gateway looked safe from off to the side; the zergling then stood wherever its path ended, in
    stalker range. As it walked closer the verdict flipped to "nothing to do", which gave no order, so
    the old attack order carried it on to die.
  - Now: danger cells = within any armed enemy's reach + `SAFE_WORK_MARGIN` (melee included, so a
    zealot makes the faces next to it unsafe). A building is safe work only through its safe attack
    cells on the zergling's patch of safe ground, with capacity = that many cells. Each zergling claims a
    cell, walks there around the danger (dijkstra over safe cells) and attacks from it.
  - No cell, no safe way, or nothing to do: an explicit hold outside their reach, replacing old orders.
- **Breach acceptance vs active enemies** (your rule): a breach is judged against what it opens plus
  enemies that are active. Active means moved `ACTIVE_MOVE`, turned `ACTIVE_TURN` (units on hold turn
  to shoot), or dealt a hit we can match by reach and damage amount; idle again after
  `ENEMY_IDLE_SECONDS` = 20. Before, the survivors had to beat all 9 fighters at once, so no breach was
  ever accepted with 6 zealots, and acceptance flickered with 4.
- **Choke cost**: covering enemies measured to the attack cells (`ReachMap.attack_points`).
- **Open chokes up first** (your idea): a partly enclosed melee unit (fewer open sides than in the
  open) isn't attacked while a building touching it has safe cells. Breaking that costs nothing and
  opens it up. Only then does the worth check apply. This was the change that made the 6-zealot grid
  pass: about 2,200 "open it up first" decisions, 14 zealot fights taken.
- **Distance limits that left units stranded** (the pattern: a radius that starts fight mode, 18, bigger
  than the one that gives work, 10):
  - Dropped `CLEAR_THE_WAY_RANGE` for safe work: 8 zerglings had parked down the ramp with "nothing to
    do" and every gateway 10+ away.
  - Dropped the strikes' 12-tile building limit (any building in the strike's map).
  - Dropped `APPROACH_DISTANCE`: no attack-move for the last 12 tiles, ares' path around fire all the
    way.
  - Raid staging times out after `RALLY_SECONDS`.
- **Patrols / help**:
  - A patrol is kept only while raids are staffed, gathered and their fights at the bar (`raids_ok`).
  - "Room" for helpers counts breach slots and safe building cells, not just open sides around units.
  - You decided against "no patrols during raids / helpers stay until the end": we keep splitting forces.
- Note: a squad that's fighting keeps its job label (a "patrol" fought all game next to the raid). It's
  skipped by raid assignment and help while fighting. Harmless; proposal (b) would relabel it.

**Open:**
- Regression set not yet run on this commit: both 4-zealot grids, 6-zealot bottom, open_army,
  open_army_free bottom, tank_dive. You're running it on Linux.
- The "UNSAFE" counts only mean the zergling stood in enemy range when ordered (breach work and
  strikes count). A count of real "walked into fire for nothing" would be more useful.
- Help sends and frees a squad repeatedly while a fight's verdict hovers around the help bar.
- The sim still drives raid sizing (36 for the 6-zealot grid) and the squad verdicts, and doesn't know
  chokes (todo #21).

### 30. Oct 4, later: stable targets, routes around danger, scouting while waiting

**`tank_dive`: both sides pass** (top 8 s / 5 lost, bottom 9 s / 10 lost; bottom never passed before).
- The bottom-side "overfill" was zerglings swapping targets every step. A zergling only kept its
  target if its game order still pointed at it, but zerglings walking around others have *move* orders,
  so they re-picked from scratch. Slots are handed out fresh each step, so they found their slot taken
  and swapped tank <-> marine. `ZASZ_ORDER_LOG=1` logs decision vs game order per zergling (it showed
  orders do get through, one step late).
- `Assignments` (targeting.py): each zergling's target is kept across steps. Its slot is reserved until
  it's handled that step, so newcomers only get really free slots. Detour-following claims its slot too.
- The `tank_dive` checker counts the bot's assignments, not game orders.

**`alternate_target`: both sides pass** (top 85 s / 3 lost, bottom 91 s / 9 lost; it lost 13-16 every
run before).
- What was wrong, in order of discovery:
  1. Pushes kept walking back into the tanks: a push's destination never re-checked for danger.
  2. Even re-planned, the only way to the next target (their main) went past the tanks.
  3. The "scout needed" area cleared as soon as its center (the tank) was visible.
  4. The real trigger: when a tank's reveal from firing wears off it becomes a *remembered* unit, and
     partly-seen only looked at visible ones. So the remembered tank became a plain target (the sim saw
     2 tanks, not the 16 marines) and zerglings dove its last known spot. (Your correction: they never
     saw the tanks.)
  5. Waiting for the overlord wasted time; the area then expired after 20 s and they walked back in.
  6. No overlord came at all: the only one sat on a post, and requests only used free overlords.
- Fixes:
  - Pushes re-plan right away when their destination is in an avoid area or in reach of a group they
    can't beat (`dangerous_for`). Raids/strikes/helps keep their own rules.
  - `RouteMap` (targeting.py): map-wide routes with danger as walls (avoid areas, reach of groups we
    can't beat). Push/patrol/raid/scout squads follow it, and destinations without a route are skipped.
    Cost: a rebuild every 2 s takes 3-5 ms; lookups ~0.1 ms; the army went from ~1.3 to ~1.7 ms/step.
  - "Scout needed" areas: everything armed in them stays partly seen, remembered units included. They
    clear only when actually scouted (center + 8 points at 60% out visible), and stay while an overlord
    is on its way (`Scouting.eta`).
  - Requests take an overlord off its post if no free one exists.
  - Scouting while waiting (your design): a squad held back by a partly seen position becomes a
    `scout` for the closest expansion it can reach and return from before the overlord's vision
    arrives (along the route; up to `SCOUTED_SECONDS` = 60 s if no overlord is coming). One spot per
    squad. It's done when the spot is seen, half its time is used, or it's hit after setting out. Then
    it goes back to the job it had before. Seen spots are skipped for 60 s. Scouting squads can be taken
    for a raid at once, which is how the third got killed.
- Debug log: army ms/step (avg, max) and route timings each 3 s; `squad scout:` rejection reasons.

**Open:**
- Regression set on this commit (everything else touches targeting/movement now): grids (4 and 6
  zealots, both sides), open_army, open_army_free, wall_*, fight_winnable.
- Squads that hit "partly seen" with no overlord at all get no time limit beyond 60 s; check real games.
