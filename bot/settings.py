"""All of Zasz's tunables in one place."""

from ares.consts import EngagementResult
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

# --- Economy ---
MAX_DRONES = 70  # overall cap; the real target comes from `actions.drones.drone_target()`
POOL_AT_DRONES = 16  # start the spawning pool once we have (or are making) this many drones
# Expand when our drones are within this many of filling every base we have (and more patches
# would let us use more drones). Also covers mined-out patches, which lower the drone target.
EXPAND_WHEN_DRONES_WITHIN = 4

# --- Army ---
ATTACK_AT_ZERGLINGS = 16
DEFEND_RADIUS = 20  # squads this close to a hatchery always fight
ENGAGE_RANGE = 14  # squads react to enemies this close
SQUAD_RADIUS = 9  # zerglings this close together form one squad
REGROUP_DISTANCE = 15  # squads farther than this from the main squad rejoin it before attacking
RETREAT_AT = EngagementResult.LOSS_CLOSE  # retreat if the combat sim predicts this or worse

# --- Queens and defense ---
CREEP_QUEENS = 2  # queens beyond one per hatchery, used to spread creep
QUEEN_DEFEND_RANGE = 12  # queens fight enemies this close while near our bases
SPORES_PER_BASE = 1  # built in each mineral line once the enemy shows air

# Upgrades the scripted decisions want, in priority order (once the spawning pool is down)
SCRIPTED_UPGRADES = [
    UpgradeId.ZERGLINGMOVEMENTSPEED,
    UpgradeId.ZERGMELEEWEAPONSLEVEL1,
    UpgradeId.ZERGGROUNDARMORSLEVEL1,
    UpgradeId.ZERGMELEEWEAPONSLEVEL2,
    UpgradeId.ZERGGROUNDARMORSLEVEL2,
]

ARMY_COMPOSITION = {
    UnitTypeId.ZERGLING: {"proportion": 1.0, "priority": 0},
}
