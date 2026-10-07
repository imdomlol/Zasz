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

- **Help flip-flop:** help sends and frees a squad repeatedly while the verdict hovers around the help bar.
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
  Related: a travelling squad with no route around danger falls back to ares' path (danger only a cost);
  it could hold or back off instead.
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
