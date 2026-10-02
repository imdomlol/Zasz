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
