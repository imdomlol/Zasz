# Repository review 2: findings and faster testing

Status report written while the tests run; **Latest results** is updated as they finish. The backlog itself is
`todo.md`; this file can go once you've read it.

## Where things stand

| Phase | Branch | State |
|---|---|---|
| 1, measurement tooling | `review2-phase1-measurement` | Done, pushed; bot unchanged |
| 2, refactors with identical orders | `review2-phase2-refactors` | Done, pushed. 39 of 39 repeatable scenario runs matched command for command, debug on and off |
| 3, behavior fixes | `review2-phase3-behavior` | Done: R01, R03, R07, R05, R06 kept; **R02 and R04 reverted and deferred** |
| 4, runners and state owners | `review2-phase4-rebased` | Done. 39 of 39 repeatable scenario runs identical to phase 3 (30,813 commands); 21 games ran with no crash |

(`review2-phase4-runners` on GitHub is the older draft: I rebased after reverting R02, and overwriting a pushed branch
needs your say-so. `review2-phase4-rebased` is the current one; `review2-drafts` can go too.)

Nothing is merged into `squads` or `main`.

## The main finding: R02 made the bot worse, and one-run checks missed it

Phase 2 and all of phase 3 ran side by side, same machine, same seeds, 21 games, two rounds each:

| Code | Resources lost per run | Total | Wins of 42 |
|---|---|---|---|
| Phase 2 | 64.2k, 104.5k | 168.7k | 41 |
| Phase 3 (R02, R01, R03, R07, R05, R06) | 104.6k, 144.7k | 249.3k (+48%) | 36 |

Phase 3 is worse in 15 of 21 matchups, and kills about the same, so it's trading worse. Each fix had looked "within
noise" against the fix before it, and that's how the drift got through: **comparing each step only to the previous
step, with one run, can't see a slow slide.** Compare against a fixed baseline, in the same conditions.

## Decisions so far

- **R04 reverted and deferred.** Worse in its head-to-head, but on thin evidence (3 games a side): "not shown to help,
  possibly harmful", not proven harmful.
- **R05 kept without more testing (your call).** It only touches overlords.
- **R06: no effect seen.** No game logged a blocked or reopened expansion, so it changed no decision in those runs.
- **R02 reverted and deferred** (see below; it had looked fine, even good, in its first run).
- **R01, R03, R07 kept as a group:** without R02 the stack matches phase 2 on average. They weren't separated from
  each other, so this says "no harm together", not that each one helps.

## Latest results

**The split test** (side by side, same seeds, 2 rounds of 21 games each):

| Code | Lost per run | Avg lost | Wins of 42 | Lost/killed | Avg game |
|---|---|---|---|---|---|
| Phase 2 | 64.2k, 104.5k | 84.4k | 41 | 0.38 | 575 s |
| Phase 2 + R02 only | 105.8k, 98.9k | 102.4k | 38 | 0.44 | 654 s |
| All of phase 3 without R02 | 69.1k, 88.0k | 78.5k | 40 | 0.37 | 596 s |

R02 alone costs about 18k more resources per 21 games, and games run longer. Why isn't proven. The likely reason: its "don't send
spare squads to a site the sim rejects" removed pressure that was covering for the sim's pessimism (`todo.md` #21).

**Noise check:** phase 4's own run lost 113.5k, though its orders match the variant that lost 69.1k and 88.0k on the
repeatable scenarios. A single 21-game total can swing that far on essentially the same bot, so never judge by one run.

**Fine-tuning (running when this was written):** base, V1 (spare squads join a gathering raid at its staging point:
R02's one part that shouldn't reduce pressure) and V2 (R04 again, fairly tested), side by side, 2 rounds.

## Faster testing

### What the data says

From the runs above (few runs, so treat these as a strong lead, not a proof). "Signal" is the difference between phase 2
and phase 3, divided by how much phase 2 varies between its own runs. Higher is better.

| What you measure | Signal | Note |
|---|---|---|
| Total resources lost, all games (what we use now) | 2.6 | |
| Average of log(resources lost) per game | 4.1 | One 30k game no longer dominates |
| Average lost / killed per game | 4.7 | Nearly twice the signal, same games |
| Lost in **non-Protoss games only** (14 games) | 5.6 | Fewer, shorter games, and far more signal |
| Lost in Protoss games only (7 games) | 0.8 | The slowest games tell us almost nothing |

Also:
- Three matchups (LeyLines, Ultralove and Magannatha vs Protoss) hold 43% of all losses, and they're the noisiest.
  LeyLines Protoss alone varies by ±15k, more than the whole Terran and Zerg signal.
- The median game is 547 s of game time, 10% run past 828 s, and ties take 1200 s.
- A full side-by-side costs 80 minutes because it plays 84 games, 28 of them long Protoss games.

### Ideas, most promising first

**Change what we measure (no new machinery)**
1. **Judge by lost/killed or log(lost), not total lost.** Same games, about twice the signal: often half the runs.
2. **Automatic verdict tool:** given two sets of runs, print the per-matchup difference with a confidence range, "worse",
   "same" or "not enough data". This replaces reading tables by eye, which is how a +48% slide slipped by.
3. **Keep a fixed baseline** (5+ runs in `benchmarks/`), so a test only plays the new code. Halves every comparison,
   if machine conditions are matched or corrected for: a few canary games would detect a slower machine.

**Play fewer, better games (5-15 minutes)**
4. **A fast benchmark of non-Protoss games only:** 14 games at 6 at a time is about 8-10 minutes. Protoss runs join in the
   nightly run, plus a dedicated check when working on ties.
5. **Stop early when it's decided (SPRT, how chess engines test):** play game pairs and stop as soon as the result is
   statistically clear. A big regression stops early, a neutral change takes longest. Not measured here yet.
6. **A game-time cap for the fast benchmark:** end games at 8-10 minutes and score the fight so far. Saves 12-23% of game
   time. This needs loss and kill snapshots over time, and it can't catch late-game ties.
7. **Record scores over time** (every minute: lost, killed, supply, bases). This lets us compare at the same game minute,
   see when a change starts hurting, and supports (6).

**Test the part that changed (seconds to 5 minutes)**
8. **Check if the change even fires.** Count how often the new code path changes an outcome. R06 fired zero times in 21
   games, and R05 touches only overlords: neither needed an 80-minute run. A 2-minute order-log run plus counters says
   whether any test is needed at all.
9. **Tiered gate:** unit tests (1 s), order-log diff of repeatable scenarios (2 min, says if behavior changed at all),
   relevant scenarios (under 5 min), fast benchmark (15 min), full run (nightly or before merging only).
10. **Mid-game scenarios, the biggest gap today.** All 20 scenarios were unchanged while full games got 48% worse, because they
    stage a single fight. Nothing covers raid allocation or spare squads. New staged starts: a 60-zergling army at 8:00
    against 2-3 enemy bases with defenders, a sim-rejected site next to a soft one, a defended site being reinforced. Each
    takes 1-2 minutes. Directly tests R02-type changes.
11. **Fights from real games:** when a benchmark game loses a lot at once, save the units and positions, and re-stage that
    as a scenario. The suite then grows from real failures, focused on what actually costs resources.
12. **Offline decision replay:** save game state snapshots to disk, then re-run only the planning code on them in seconds:
    how many raid, help and patrol decisions change, and where? No StarCraft II needed, thousands of situations per minute.
    It shows what changed, not whether it's better, but that's often the question.

**Less noise in the games themselves**
13. **Find out why fixed-seed games diverge.** 13 scenarios repeat exactly, but full games don't, though seeds are fixed.
    If we find and remove the cause on our side, one paired game shows a change's exact effect from the first differing
    command. This could be the highest payoff of all, and it may be impossible if the built-in AI itself varies.
14. **Paired games with the same seed, judged per pair:** already partly done. Formalizing it (see 2 and 5) removes much of
    the between-map variance.

**More raw speed**
15. **More cores when needed:** games need about 1 core and 1.5 GB each. A rented 32-core machine could play 84 games in
    around 15 minutes for little money. Your Windows PC could also help, but its build isn't comparable, so only as its
    own baseline.
16. **Nightly and on-merge full runs only:** the 80-minute version runs unattended, and daytime uses the tiers above.
17. **Our own light battle simulator** (ties into `todo.md` #21): thousands of fights per second without StarCraft II, for
    tuning fight logic. A big project, and only as good as its match with the real game.

### What I'd do first

1. The verdict tool with lost/killed and log(lost) (idea 1 and 2): half a day, immediate gain.
2. The non-Protoss fast benchmark with early stopping (4 and 5): 8-10 minutes for most changes.
3. Change-fires counters and the tiered gate (8 and 9): skips the benchmark entirely for changes like R05 and R06.
4. Mid-game scenarios (10): covers the blind spot that let this regression through.

The result: most checks take under 15 minutes, many under 5, and the full run moves to nightly.

**Validate first:** the "signal" numbers come from 4 phase 2 runs and 2 phase 3 runs. Before relying on the non-Protoss
subset, check that it also detects a regression other than this one.
