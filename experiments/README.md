# Experiments

Army changes that were tried, measured and dropped, kept as patches for reference. None of them
applies to the current code (the army has changed a lot since); to look at one, apply it to a
checkout of the commit it was made on (`git apply --check` passes there).

| Patch | Applies on | What it tried | Why it was dropped |
|---|---|---|---|
| `breach-plan.patch` | `7e43143` (2026-10-02) | A persistent breach plan with a flood: one target per fight, flood the hole when it breaks (`FLOOD_RANGE`, `FLOOD_SECONDS`) | No gain on seeds, and `wall_bays` got worse (notes.md, "Tried and dropped") |
| `free-hits-two-pass-unit-breach.patch` | `7e43143` (2026-10-02) | For `wall_bunker`: overflow zerglings only take free hits (outside every enemy's reach, `FREE_HITS`), two-pass target assignment (idle zerglings first), breaching through a unit as well as a building | Saved ideas, not adopted; `wall_bunker` is still expected to fail (todo.md) |
| `strikes-v2-full.patch` | `d95125b` (2026-10-02) | "The strike decides for everyone near its group" | Squads that weren't picked stepped away instead of fighting; the open army went from 10 to 16 lost (notes.md) |
