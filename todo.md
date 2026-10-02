# Zasz todo

Each section is one **potential branch**: one idea, scoped so it can be reviewed and merged on its
own. Branch off `main` with the name given; nothing is committed to `main` directly. Merge a branch
into `main` when it's done (benchmark + `scripts/run_scenarios.py` passing), then remove its section
here on that branch.

Numbers are fixed IDs (sections refer to each other by them), so finished items leave gaps.

Impact is rated against where Zasz is now: 6/6 up to Hard, about 50% from Harder upward (difficulty
sweep at `931c39e`), zergling-only. **High** = likely to move win rates at Harder+ or unblock the
learning work; **Medium** = clear benefit, but narrow or indirect; **Low** = nice to have / tooling.

## Branches, in suggested order

| # | Branch | Idea | Impact | Depends on |
|---|---|---|---|---|
| 13 | `damage-efficiency` | Damage efficiency (metric, scenarios, target slots done; ramp open) | High | - |
| 15 | `external-opponents` | Play against other people's bots | High | - |
| 8 | `trade-tracking` | Ally vs enemy losses (resource trades) | Medium | - |
| 9 | `mining-rate` | Mining rate per drone | Low | - |
| 10 | `army-value` | Army value: ours vs the enemy's | Medium | - |
| 14 | `supply-targeting` | Target supply buildings when the enemy is near its supply cap | Medium | `army-value`, `damage-efficiency` |
| 2 | `army-variety` | More unit types + army composition logic | High | - (`damage-efficiency` helps tune ranged/air control) |
| 3 | `army-upgrading` | Upgrades tied to army composition | Medium | `army-variety` |
| 11 | `decision-explanations` | Explain decisions in chat | Medium | `trade-tracking` (for outcomes) |
| 12 | `mutalisk-harass` | Mutalisk worker harass, decided by expected value | Medium-High | `army-variety` (spire tech), `army-value` |
| 1 | `replay-summary` | Watch replays on Linux | Low | - |
| 6 | `static-defense-placement` | Better spore / spine placement | Low-Medium | - |
| 7 | `drone-escape-building` | Drone saves itself by starting a building | Low-Medium | - |

Why this order: `damage-efficiency` builds directly on the scenario suite and the bot is all melee
today, so it's likely the biggest win-rate lever before new units. `external-opponents` next,
because everything after it should be judged against real bots, not just the built-in AI (which
Zasz now beats 6/6 up to Hard). `trade-tracking`, `mining-rate` and `army-value` are small and
land as features in `bot/observation.py` (the learning interface is done: observe -> decide -> act,
see the README), rather than as scattered helpers. `static-defense-placement` and `drone-escape-building` wait until harass
shows up in losses (check kept replays, summarised by `replay-summary`).

## `damage-efficiency` (#13): Damage efficiency: how much of the army's damage actually lands

**Status (branch `damage-efficiency`):** metric, scenarios and a first fix are in; the ramp is still
open.

**Done:**
- `bot/efficiency.py`: per committed unit (same target >= 1 s, not travelling, not chasing a fleeing
  unit), potential DPS (`calculate_dps_vs_target`) split into delivered / blocked / idle, plus damage
  dealt/taken and dmg/s. Not melee-specific: ranges come from each unit's own ground/air weapon, so
  it carries over to mutalisks (their bounce isn't counted as potential). Logged at game end ("Army
  damage: ..."); the 10 s rolling efficiency is the `damage_efficiency` observation feature.
- `scenarios/efficiency.py`: `efficiency_open_field`, `efficiency_deep_base`, `efficiency_ramp`
  (30 zerglings vs engineering bays; results appended to `benchmarks/efficiency.csv`).
- `bot/targeting.py`: target slots. A structure takes only as many melee attackers as fit around
  it; with `STRUCTURE_FILL = "back"` the first attackers in go deepest and later ones fill toward the
  front. Ranged/air attackers and unit targets aren't capped (`attack_capacity` is where a mutalisk
  overkill cap would go).

**Results** (kill time, 2-3 runs each): open field off 31-32s / back 30s; deep base 64-65s / 59-62s;
ramp 35-36s / 34-38s. Slots cut blocking where there's room to spread out; target choice makes no
difference on the ramp.

**Still open:**
- **Ramp throughput** (~15% blocked): it's traffic on the ramp itself, not target choice. Ideas: hold
  the back of the army out of range (or keep it moving to a second ramp/flank) instead of queueing;
  only send as many up as the top can use. Measure with `efficiency_ramp`.
- **Idle ~24% everywhere** is mostly the melee wind-up (cooldown hits 0 before the next hit lands);
  a better "swinging" signal would make this number meaningful.
- **Stutter step during cooldown**: not tried; the metric can decide.
- **Mutalisks** (#12): add an overkill cap for ranged attackers in `attack_capacity`, and a
  scenario (mutalisks vs workers/structures) to measure bounce and spread.
- **Real games are noisy** (queens, regeneration, repair, vision): compare in scenarios.
- **Real games read lower than scenarios:** a VeryHard win at `4ec2259` logged efficiency 32%
  (blocked 51%). Zerglings chasing a unit moving *sideways* counted as blocked; fixed in `6a51dd5`
  (chasing any moving target isn't blocked), after which a VeryHard win read 45% (blocked 35%). The
  rest of the gap to 60-70% may be real congestion in messy fights: worth a scenario with enemy
  *units* (e.g. zerglings vs a marine ball) next. The game-end dmg/s is averaged over the whole
  game, so it only means something in scenarios.

## `external-opponents` (#15): Play against other people's bots

**Idea:** Figure out how to enable battling other models that other people have put forth.

**Depends on:** -

**Revision:** Two levels, cheapest first:
1. **Ladder:** upload Zasz to AI Arena (aiarena.net) and let it play the ranked ladder against
   everyone's bots. The pieces exist (`scripts/create_ladder_zip.py`, `ladder.py`,
   `ladderbots.json`); this is mostly checking the zip runs and setting up an account and bot
   entry. Results come back as match history and replays (4.10, so they play here headless and
   feed `replay-summary` #1 and training data in the same format as `benchmark.py --record`).
2. **Local bot-vs-bot:** add `--opponent <bot folder>` to `scripts/benchmark.py`. python-sc2's
   `BotProcess` already launches any bot that speaks the standard ladder protocol (`--LadderServer`,
   `--GamePort`, `--StartPort`), in any language, which every AI Arena bot does. Opponents come from
   AI Arena bots whose authors made them downloadable, or open-source bots on GitHub, kept in an
   `opponents/` folder (gitignored) with their `ladderbots.json` read for the launch command and
   race. The rest of the benchmark (parallel jobs, crash handling, kept replays, results.csv) works
   as is, with the opponent's name in place of the difficulty. For bots with heavy dependencies,
   AI Arena's local-play setup (Docker, `aiarena/arenaclient-sc2-base` / sc2-ai-match-controller)
   runs them the same way the ladder does.

**Impact:** **High.** The built-in AI is a narrow and predictable opponent (and the harder levels
cheat); tuning only against it risks overfitting. Real bots punish different weaknesses (early
rushes, cannon/bunker rushes, mass air, proxy builds), and the ladder gives a rating to track over
time. It also gives the most realistic training data for a learned `Decisions` model.
**Blockers:**
- **Getting opponents:** only bots their authors mark downloadable on AI Arena can be fetched, and
  some need an AI Arena account. Respect each bot's license; don't redistribute them (keep
  `opponents/` out of git).
- **Running them:** each bot brings its own runtime (Python version and packages, .NET, Java, C++
  binaries, sometimes PyTorch/GPU for ML bots). Linux binaries are fine here (AI Arena runs
  Linux); Python bots can conflict with our venv, so give each its own (or use the Docker setup).
- **Two bots = two SC2 instances per game:** roughly double the CPU/RAM of a vs-AI game, so fewer
  parallel `--jobs` on this machine (8 cores, 30 GB).
- **Ladder rules:** ares `Debug: False` for uploads (already noted in `config.yml`); the ladder's
  map pool and Python version (3.12) have to match; chat that other bots can read (#11).
- **Timing:** AI Arena enforces per-step time limits; a slow `on_step` (e.g. lots of pathfinding,
  #13 metrics) can lose games by timeout on the ladder even when it wins locally. The benchmark
  measures step time against the ladder limits (done), so check it for bot-vs-bot games too.

## `trade-tracking` (#8): Ally vs enemy losses (resource trades)

**Idea:** Implement logic to determine total ally losses vs enemy losses, which helps us understand wasted resources better. When we train a model, this will be useful.

**Depends on:** - (add it as `Observation` fields in `bot/observation.py`)

**Revision:** Mostly already available: `state.score` has killed and lost values by category (the
benchmark already records `killed_value` and `lost_value`). What's missing is *per-engagement*
trades: snapshot the values when a squad engages and when it disengages. That gives "this fight
cost X and killed Y", which #11 and the model can use.
**Impact:** Medium (a good model feature and a good benchmark metric; no win rate on its own).
**Blockers:** Defining where an engagement starts and ends. Enemy losses in the fog aren't
reported.

## `mining-rate` (#9): Mining rate per drone

**Idea:** Implement logic to determine the number of resources mined per drone, like the rate we are gathering, so the model will be able to better understand and plan around incoming minerals (unless we already do this somehow?)

**Depends on:** - (add it as `Observation` fields in `bot/observation.py`)

**Revision:** Partly there: `state.score.collection_rate_minerals/vespene` is the game's own income
rate (`affordable_on_arrival` already uses it). Per drone = rate / mining drones. Add the
"expected" rate from patch saturation (ares' `Mining` knows assignments), so the gap shows
oversaturation, long-distance mining or harass.
**Impact:** Low (cheap model feature; small decision value for scripted rules).
**Blockers:** None significant. The game's collection rate lags (it's averaged), so it reacts slowly.

## `army-value` (#10): Army value: ours vs the enemy's

**Idea:** Implement logic to understand enemy army value vs ally army value to give model more information when deciding to attack or not (we will also adjust the ares packages that do this to advice the model similarly)

**Depends on:** - (add it as `Observation` fields in `bot/observation.py`)

**Revision:** ares already does this in the combat simulator (`FIGHT_AT` uses it per fight). A
whole-map value is easy to add (cost of our army vs cost of known enemy units, from ares' unit
memory) and is a natural `Observation` feature for the attack decision.
**Impact:** Medium. It improves the attack/defend decision, especially once there's more than one
unit type (#2).
**Blockers:** **Fog.** The enemy value is only what we've seen (plus memory), so it undercounts
whatever we haven't scouted; it needs a "last seen" age or a scouting confidence. Changing ares'
own advice means forking or wrapping ares code (it's a pinned submodule).

## `supply-targeting` (#14): Target supply buildings when the enemy is near its supply cap

**Idea:** Estimate the enemy's current supply, so when attacking an enemy base we can decide whether supply buildings are better targets than production. If the enemy uses 50 supply and we see 51 supply worth of supply buildings, killing one of them cuts off all future production and effectively disables many more buildings quickly.

**Depends on:** `army-value`, `damage-efficiency`

**Revision:** Estimate the enemy's supply **used** and supply **cap**, and when the margin is
smaller than what one supply building provides, put that building type ahead of production in the
target priority. Today the priority is fixed (`PRIORITY_STRUCTURES` in `bot/army.py`: townhalls,
pylons, spawning pool), so this makes it dynamic.
- **Cap:** sum the supply of known enemy providers (max 200), townhalls included:
  - supply depot / pylon / overlord: 8
  - townhalls: command center / orbital command / planetary fortress 15, nexus 15,
    hatchery / lair / hive 6

  Townhalls count toward the cap like any provider, so killing one also takes away its supply
  (on top of its economy). It can be the thing that supply-blocks them, which makes townhalls
  (already in `PRIORITY_STRUCTURES`) even better targets near the cap. ares' unit memory keeps
  structures we've seen in the fog.
- **Used:** supply cost of every known enemy unit (memory included), plus an estimate for workers
  we haven't seen (e.g. from the number of bases and the game time).
- **Rule:** if `cap - used < 8`, killing one provider supply-blocks them, so add the provider type
  to the front of the priority (a capped number of zerglings, see #13, so the rest still hit other
  targets). Re-check each step: once they're blocked, killing a second one adds little.
- **Protoss is the strong case:** a pylon also powers nearby buildings, so killing one can switch
  off production *and* supply at once. Pylons are already a priority; this would rank the pylons
  powering the most production first.

**Impact:** Medium. Strongest against Protoss (pylon power) and when we're already in the enemy
base; small otherwise, because a supply block only stops *new* units (units already in production
finish), and the built-in AI rebuilds supply quickly.
**Blockers:**
- **Fog makes both numbers uncertain:** we rarely see all their units or all their supply
  buildings. Missing units make the margin look bigger than it is (we skip a good target);
  missing supply buildings make it look smaller (we kill one that doesn't block anything). Needs
  a confidence measure (how recently and how much of the base we've scouted), and should only act
  when the estimate is clearly close to the cap.
- **Zerg supply is overlords:** zerglings can't hit air, so against Zerg this only works with
  anti-air units (#2) or by sniping hatcheries.
- **Terran depots are often in a wall** (lowered/raised at the ramp): killing one also opens the
  way in, which is a separate value worth weighing.
- Fits naturally with #10 (enemy army value: the same "known enemy units" estimate) and the learning interface (supply
  margin is a good model feature).

## `army-variety` (#2): More unit types + army composition logic

**Idea:** Allow more units to be utilized and create army composition logic

**Depends on:** - (`damage-efficiency` helps tune ranged/air control)

**Revision:** Move from zergling-only to a composition that answers what we've scouted (e.g.
roach/ravager vs ground, hydra/queen vs air, banelings vs light units). ares' `SpawnController`
already takes a composition dict, and the learning interface already carries it: add the units to
`bot/spaces.py` (`ARMY_UNITS`, `OWN_UNIT_TYPES`) and have `decide` set `Decision.army` from what
`Observation` says we've scouted (add enemy unit counts there if needed). The real work is around it: tech path (lair, roach warren, hydra den), gas
timings, and army control that copes with ranged units (squads assume melee today).
**Impact:** **High.** It's the clearest gap: zerglings alone stall against Harder+ (ties at the
20-minute limit, losses to siege tanks and air), and queens + spores are the only anti-air.
**Blockers:** `bot/army.py` targeting/retreat logic is tuned for melee and needs ranged micro;
`upgrades.py` currently only takes gas for upgrades; tech timing has to fit the expansion logic.
Big enough to split into stages (mutalisks first since zergling/mutalisk is a strong combo).
Scope: this branch builds the tech and adds the units to the army; the harass *decision* for
mutalisks is `mutalisk-harass` (#12), and their upgrades are `army-upgrading` (#3).

## `army-upgrading` (#3): Upgrades tied to army composition

**Idea:** Ensure upgrade logic is tied to army composition

**Depends on:** `army-variety`

**Revision:** Derive the upgrade list from the composition (melee vs missile vs flyer, plus unit
upgrades like roach speed or hydra range). `Decision.upgrades` is already a wish list that
`actions.upgrades` turns into gas, tech and research, so this is mostly a mapping table once #2
exists (new upgrades go in `bot/spaces.py` `UPGRADES`).
**Impact:** Medium (it multiplies #2's impact; little on its own while we're zergling-only).
**Blockers:** Depends on #2. Some upgrades need hive tech (+3 needs hive), which nothing builds yet.

## `decision-explanations` (#11): Explain decisions in chat

**Idea:** Implement vocalization of decisions in chat as well as reasoning (eg deciding to attack because army value is higher and success is high, or expanding because drones = 16, or skipping expansion because rocks are blocking, or skipping expansion because hostile controlled, or focusing attack onto enemy expansion because enemy army value is low nearby area, or deciding to attack with mutalisk for xyz as well as the outcome of that decision like lost 100 minerals but cost enemy 145 minerals and 55 potential minerals)

**Depends on:** `trade-tracking` (for outcomes)

**Revision:** A small `explain(event, reason, outcome)` logger: always write to the log file
(benchmarks keep logs for crashes), and send a short in-game chat line for key decisions only
(attack, expand / skip expansion with the blockage reason, harass). Chat lines are saved in the
replay, so this pairs well with #1. Close the loop for decisions that have an outcome by using
the trade numbers from #8.
**Impact:** Medium as a debugging tool, especially with replays and when the model starts making
decisions; none on win rate.
**Blockers:** Chat spam (rate-limit it; keep it opt-in, like ares' `Debug`/`ChatDebug`). Opponent
bots can read chat on the ladder. "Outcome" attribution (which losses came from which decision)
is the hard part, and needs #8's engagement tracking.

## `mutalisk-harass` (#12): Mutalisk worker harass, decided by expected value

**Idea:** Implement a mutalisk worker-harass decision that takes into account the value of an individual worker (including training cost AND potential minerals lost while retraining workers), the value of a mutalisk, expected losses (from base defence like spore crawlers or a reacting army), the time it would take known army locations to move to respond, etc. Essentially harass the enemy economy when it would cause more damage to the enemy than ally.

**Depends on:** `army-variety` (spire tech), `army-value`

**Revision:** Harass when expected enemy loss > expected own loss. Enemy loss = workers killed ×
(50 + mining lost while rebuilding) + supply/production lost; own loss = mutas expected to die vs
known anti-air (spores, turrets, cannons, army) and the time until the enemy army can respond
(path distance from known army positions). ares' air grid and `KeepUnitSafe` (already used for
overlords) handle the avoidance part.
**Impact:** Medium-High against the built-in AI at Harder+, which tends to defend harass poorly;
it also forces the enemy to spread out. But it's a big tech switch.
**Blockers:** **Needs a Spire** (lair, then spire, then heavy gas), which is #2/#3 tech work first.
Muta micro (clumping, magic box vs thor/archon splash) is its own project. The value estimate
inherits #10's fog problem.

## `replay-summary` (#1): Watch replays on Linux

**Idea:** Figure out how to watch replays on linux

**Depends on:** -

**Revision:** Watching isn't possible with the headless build (no renderer). Options: watch on
Windows (benchmarks now keep notable replays in `replays/benchmarks/<run>/`), or run the Windows
client through Wine/Proton. More useful day to day: a `scripts/replay_summary.py` (sc2reader) that
prints build timings, army supply over time and engagements, so most replays never need watching.
**Impact:** Low directly, Medium as an enabler (faster diagnosis of losses).
**Blockers:** Wine needs a Battle.net account and the 4.10 version download, which can be finicky.
sc2reader gives events, not full game state, so it can't answer everything.

## `static-defense-placement` (#6): Better spore / spine placement

**Idea:** Implement logic to determine optimal locations for spore / spine crawlers (near minerals, etc)

**Depends on:** -

**Revision:** Spores already go between the hatchery and the minerals (`defense.py`). Improve: put
each spore where it covers the most mineral patches and the queen/injection area; place spines on
creep facing the likeliest attack path (ares can give the path from the enemy base), and only
build them when scouting shows pressure.
**Impact:** Low-Medium (matters against air/drops and early aggression; games vs the built-in AI
rarely last long enough to need spores today).
**Blockers:** Spines and spores need creep, so placement depends on creep spread. Spine
crawlers aren't built at all yet, so the "when" logic is new.

## `drone-escape-building` (#7): Drone saves itself by starting a building

**Idea:** Implement logic to use a drone to build something when it is being attacked and may die, with the intention of salvaging it later to save the drone. This must take into account building cost to make sure it financially makes sense to build the item when compared to just making a drone. Furthermore, in the case that the drone would still not be safe when the building would complete, we should ensure that we choose a building like spine or spore crawlers that would help the situation after it was done. For example, reapers in the mineral line might lead to a drone saving itself by making a spore crawler, but if the reapers are still in the base when the spore crawler finishes, we lose a drone, get no refund on the building, and the building doesn't help our situation. Ideally the drone builds a spine crawler (unless its too expensive to be worth, im unsure).

**Depends on:** -

**Revision:** When a drone is about to die, morph it into a building and cancel later. Cancelling a
Zerg building in progress returns the drone and 75% of the cost, so the price of a save is 25% of
the building (~19-25 minerals for a spore/spine) versus 50 minerals plus lost mining for a new
drone. Pick the structure by what helps if the threat stays: a spine vs ground (reapers, lings), a
spore vs air; an extractor on a free geyser is the cheapest option when one is in reach. If the
building would die anyway, don't do it.
**Impact:** Low-Medium. Real but small: usually 1-3 drones per harass, and only in games with
harass.
**Blockers:** **A morphing building starts with low HP and grows as it builds**, so a building
started under fire often dies too: we lose the drone *and* the full cost. Needs a reliable "will
this building survive" estimate (incoming DPS vs HP over time). Spine/spore also need creep and a
Spawning Pool; the extractor needs a free geyser close by. The timing of the cancel needs
tracking.
