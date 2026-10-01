# Progress notes

Where Zasz stands, what's been verified, and what's open. Update this when picking work back up
or finishing a session, so it travels with the repo.

## Current state (branch `tanks-and-scouting`, 2026-10-01)

Pure scripted Zerg bot on ares-sc2. Units: drones, overlords, queens, zerglings only (by choice).

- **Economy:** 16 pool, expand whenever drones are within 4 of filling every base (also when patches
  mine out), up to 70 drones. Just-in-time builder drones.
- **Upgrades:** wish list in `bot/settings.py` (`SCRIPTED_UPGRADES`): ling speed, melee/armor +1,
  overlord speed, melee/armor +2. Gas only while the list needs it.
- **Army** (`bot/army.py`): ares squads; fight while the combat sim predicts at least a tie, retreat
  on any predicted loss but only until out of known enemy range (constant pressure, no free
  damage). Enemy units that fight back within 10 are targeted before buildings/workers; siege
  tanks/lurkers/colossi are dived first. Attack target = least defended known enemy base.
- **Queens:** one injector per hatchery, 2 creep queens, all queens defend (only anti-air unit).
- **Scouting:** up to 4 overlords posted next to enemy expansions, kept out of known anti-air.
- **Defense:** a spore per mineral line once enemy air shows up.

## Verified

| What | Result |
|---|---|
| Benchmark vs Medium (`467ba00`, before this branch) | 6/6 |
| Benchmark vs Medium (`75aba21`, this branch, `--jobs 3` on Windows) | 6/6, no crashes, wins in 6.6-8.8 min game time, ~8 min real time total |
| Benchmark vs Hard (`9bcfdcb`, `--jobs 6` on Linux) | 6/6, no crashes, wins in 7.1-9.4 min game time, ~4 min real time total. Hard is now the benchmark default |
| Benchmark vs Hard (`59432bf`, reachability checks) | 6/6, no crashes, wins in 7.5-10.4 min game time |
| Expansion blockage classifier (debug-spawned walls) | rocks / hostile / walled all identified correctly, blockers listed |
| Scenario suite (`scripts/run_scenarios.py`, 8 scenarios) | 24/24 over `--repeat 3`. Each deliberate break was caught: skipping the reachability check, empty `DIVE_FIRST`, `FIGHT_AT = 0`, no spores |
| VeryHard Terran games on this branch | 3 wins (`26ce39f` PersephoneAIE_v4, `bed03c7` LeyLinesAIE_v3, `75aba21` MagannathaAIE_v2). Several looked like the AI surrendering (few structures killed) |
| Sandbox: sieged tank firing from fog | Revealed to the bot as a visible unit at its exact position for ~2 s after each shot; ares memory keeps it afterwards. So squads do know where tanks are |

## Open items / watch list

- **`TANK_NEUTRALIZED_BY = 3`** (`bot/settings.py`, `tank_neutralized` in `bot/army.py`): sieged tanks
  with 3+ of our zerglings inside their minimum range are left out of the combat sim (the sim
  ignores minimum range). Judgement call — revert (set very high) if it causes big mistakes.
- **ares combat sim limits:** no minimum range, positions barely matter, assumes clumped units. Keep
  that in mind when tuning `FIGHT_AT`.
- **Untested in real games:** spore crawlers (games end before enemy air), overlord scouts
  surviving under pressure.
- **Built-in AI surrenders** when it's clearly losing, so a "Victory" can come with few kills. Check
  killed structure value / game length before reading much into a single win.
- **Rock breaking** (next step for blocked expansions): `ExpansionAccess.blocked` already lists the
  rocks per base. Torches' edge bases need a gate broken; also useful for backdoor rocks on LeyLines
  and Magannatha.
- **Ideas not done yet:** split attacks (pressure a far base while hitting the main), spine crawlers,
  more upgrades (+3 needs hive), imitation-learning model in `bot/decisions/`.

- **Torches AIE expedition gates:** 6 destructible gates (2000 HP, 3 armor) wall off the 4 bases
  along each player's map edge; the game engine agrees they're unreachable until a gate is broken.
  ares leaves those bases out of `get_own_expansions`, so we never try to take them. Candidate for
  rock-breaking (one zergling ~8 min per gate after armor, 16 zerglings ~30 s).
- **CheatMoney crash (2026-09-30):** `find_raw_path` returned `None` in a game that wasn't on Torches.
  Cause unknown; benchmark crashes now keep a `.log` and replays, so the next one can be traced.

## Gotchas found along the way

- `mediator.find_raw_path` returns `None` when the target is unreachable; handle it.

- Never use `unit.distance_to(unit)` with enemy units: ares memory units (last seen in fog) aren't in
  python-sc2's per-frame distance cache and it crashes. Use `a.position.distance_to(b.position)`.
- ares `AMove` does nothing within 7 of its target by default (`success_at_distance`).
- ares `ShootTargetInRange` only considers targets already in weapon range.
- Debug-spawning `SIEGETANKSIEGED` directly fails silently; spawn `SIEGETANK` and siege it.
