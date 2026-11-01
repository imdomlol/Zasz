# Milestones

What changed in Zasz and what it measured at the time, newest first. One line per milestone; the details are in
the commits. Open work is in `todo.md`. Results before 2026-10-05 were measured without fixed seeds, so they
only compare loosely with later ones.

| Date | Milestone | Code | Measured |
|---|---|---|---|
| 2026-11-01 | Avoid areas aren't law: each remembers its enemies and drops once the free squads clearly beat them; static defense keeps its area | `3ef2dec` | 21 vs Hard side by side, 3 rounds: 60 of 63 wins, avg 95.1k lost (base 55, 91.2k); scenarios x3 63/66 |
| 2026-10-31 | Repository review 2 done: measurement tooling, CI, faster reach maps, runner deadlines, typed state owners, behavior fixes R01, R03, R05, R06, R07 (R02, R04 deferred) | `6c460c1` | 21 vs Hard side by side, 5 rounds: 96 of 105 wins, avg 88.6k lost (74.5-97.7k); scenarios x3 57/60 |
| 2026-10-07 | `bot/army.py` split into parts (`jobs`, `strikes`, `breach`, `routes`, `avoid`, `micro`, `combat`, ...); order logs (`--order-log`, `scripts/compare_orders.py`) to prove a refactor changes no command | `679b123` | 21 vs Hard (`--seed 1`): 19 wins + 2 ties, 91.7k lost, 9.6 ms per game loop |
| 2026-10-07 | Code review phase 5: recordings checked and streamed, decisions validated (+ `attacking` feature), efficiency rows with side/seed/status, ladder zip with only the ladder's binaries, one import-path setup | `8157e61` | scenarios unchanged; 6 vs Hard: 5 wins + 1 tie |
| 2026-10-06 | Code review phase 4: route maps per squad strength, no push when nowhere is safe, per-attacker slot accounting, area maps per squad, one `combat_estimate`, stable injector pairs, efficiency metric fix | `d6e38de` | 21 vs Hard: 19 wins + 2 ties, 101k lost (-22%), 10.8 ms per game loop |
| 2026-10-05 | Code review phases 1-3: run labels (commit, build, run id), seeded benchmarks (`--seed`), tests on the risky code, step-time cuts with identical scenarios | `9bf51ae` | 21 vs Hard: 20 wins + 1 tie, 128k lost, 8.8 ms per game loop |
| 2026-10-03 | Squads: lasting squads with jobs (patrol, raid, help, defend) replace the single army; help coalitions, rallies, strikes, breaches and widening, routes around danger, squad scouts; debug overlay | `squads` | every scenario passes but `wall_bunker` (expected) |
| 2026-10-02 | Blocked zerglings: reach maps, stepping out of plugged gaps, costed breaches, packed-base and wall scenarios; fixed scenario seeds per side, resources-lost tracking | `blocked-zerglings`, `scenario-seeds` | `efficiency_cramped_main` 25/25 |
| 2026-10-01 | Damage efficiency metric and scenarios, target slots (fill from the back); avoid areas for armies we can't beat | `damage-efficiency`, `alternate-targets` | Hard 6/6; VeryHard 4/6 + 2 ties |
| 2026-10-01 | Learning interface: observe -> decide -> act, fixed observation/decision shapes, recording | `1cc08d0` | Hard 6/6, same decisions as before on 22,348 steps |
| 2026-10-01 | Expansions we can't walk to are skipped, with the reason (rocks, hostile, walled) | `59432bf` | Hard 6/6 |
| 2026-10-01 | Scenario suite; step time measured like the ladder; Hard becomes the benchmark default | `9bcfdcb` | Hard 6/6; 5.4 ms per game loop in a 12-min game |
| 2026-10-01 | Overlord scouting, sieged-tank dives, fighters before buildings | `75aba21` | Medium 6/6 |
| 2026-09-30 | Squad army with the combat sim, per-unit targeting, upgrades, creep, queens, spores; parallel benchmark | `467ba00` | Medium 6/6; difficulty sweep: 6/6 up to Hard, Harder 3/6, VeryHard 4/6 |
| 2026-09-30 | First version: 16 pool, expand, mass zerglings, one-point attacks | `12faa25` | Medium 4/6 |
