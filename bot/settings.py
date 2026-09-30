"""All of Zasz's tunables in one place."""

from sc2.ids.unit_typeid import UnitTypeId

MAX_DRONES = 70  # overall cap; the real target comes from `actions.drones.drone_target()`
POOL_AT_DRONES = 16  # start the spawning pool once we have (or are making) this many drones
EXPAND_TO_BASES = 2  # take expansions (starting with the natural) right after the pool
ATTACK_AT_ZERGLINGS = 12
DEFEND_RADIUS = 20  # zerglings attack enemies this close to any of our hatcheries

ARMY_COMPOSITION = {
    UnitTypeId.ZERGLING: {"proportion": 1.0, "priority": 0},
}
