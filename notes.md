# Notes: zergling blocking (branch `blocked-zerglings`)

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
