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
ATTACK_AT_ZERGLINGS = 16  # start attacking with this many zerglings...
STOP_ATTACK_AT_ZERGLINGS = 8  # ...and keep attacking until we drop below this many
DEFEND_RADIUS = 20  # squads this close to a hatchery always fight
ENGAGE_RANGE = 14  # squads react to enemies this close to any of their units
FIGHTER_PRIORITY_RANGE = 10  # enemy units that fight back this close beat buildings/workers as targets
# After a squad is turned back by a fight the sim says we'd lose, attack targets inside that enemy
# group's reach (its spread + weapon range + AVOID_MARGIN) are skipped for AVOID_SECONDS: try other
# bases instead of walking back into it
AVOID_SECONDS = 45
AVOID_MARGIN = 3
CHECK_AGAIN_SECONDS = 60  # an enemy expansion spot seen empty isn't checked again for this long
APPROACH_DISTANCE = 12  # advancing squads path around known enemy fire until this close to the target
# Enemies only count as a threat to a squad within their weapon range + this of one of its units, so
# a squad going *around* an army at a safe distance isn't turned back by it
VISIBLE_THREAT_MARGIN = 6
MEMORY_THREAT_MARGIN = 4  # remembered enemies (last seen in the fog) can't be chasing us as well
LOOKOUT_FLEE_MARGIN = 0.5  # lookouts back off within an enemy's range + this (less than PEEK_MARGIN)
# Army plan (`bot/army_plan.py`): grouping only when necessary
PLAN_SECONDS = 2.0  # re-plan who does what this often
ENEMY_GROUP_RADIUS = 10  # enemy units this close together are one group
RAID_MIN = 6  # smallest fighting detachment; raids grow in steps of this until the sim says they win
MAX_RAIDS = 2  # enemy bases raided at once
MAX_SCOUTS = 3  # single zerglings checking the enemy main and expansions
SCOUT_ZERGLINGS = 2  # while attacking, zerglings that run ahead as lookouts (outside the army plan)
MAX_WATCHERS = 4  # single zerglings watching known enemy groups
PEEK_MARGIN = 1.5  # peeking zerglings stop this far outside the remembered enemy's range
PEEK_AGAIN_SECONDS = 20  # don't peek at the same remembered group more often than this
WATCH_MARGIN = 6  # watchers sit this far outside an enemy group's range
STAGING_MARGIN = 6  # when grouping up, gather this far outside the enemy's range
GATHERED_FRACTION = 0.8  # engage once this share of the army has gathered
FORWARD_FRACTIONS = (0.5, 0.4, 0.3)  # waiting spots, as a share of the way from our rally to their natural
# Melee attackers per structure (`bot.targeting`): they fill this fraction of the ring around it,
# MELEE_SPACING attacker-widths apart; the rest pick the next structure, deeper in
MELEE_SURROUND_FRACTION = 0.5
MELEE_SPACING = 1.25
# How melee attackers spread over structures: "back" = first ones in go deepest, later ones fill
# toward the front; "nearest" = closest structure with room; "off" = no slots (closest structure)
STRUCTURE_FILL = "back"
SQUAD_RADIUS = 9  # zerglings this close together form one squad
REGROUP_DISTANCE = 15  # squads farther than this from the main squad rejoin it before attacking
FIGHT_AT = EngagementResult.TIE  # fight while the sim predicts at least this; retreat on any predicted loss
# Leave a sieged tank out of the combat sim once this many zerglings are inside its minimum range
# (the sim ignores minimum range). Judgement call: revert (set very high) if it causes big mistakes.
TANK_NEUTRALIZED_BY = 3
UNDER_FIRE_SECONDS = 1.5  # a unit damaged this recently counts as under fire

# --- Queens and defense ---
CREEP_QUEENS = 2  # queens beyond one per hatchery, used to spread creep
QUEEN_DEFEND_RANGE = 12  # queens fight enemies this close while near our bases
SPORES_PER_BASE = 1  # built in each mineral line once the enemy shows air
EXPANSION_RECHECK_SECONDS = 10  # how often to re-check whether a blocked expansion is reachable again

# --- Scouting ---
OVERLORD_SCOUTS = 4  # overlords posted near enemy expansions (natural, third, fourth, ...)
SCOUT_POST_OFFSET = 9  # how far from the expansion spot an overlord waits, toward our side
SCOUT_POST_COOLDOWN = 90  # seconds to avoid a post after an overlord got shot there

# Upgrades the scripted decisions want, in priority order (once the spawning pool is down)
SCRIPTED_UPGRADES = [
    UpgradeId.ZERGLINGMOVEMENTSPEED,
    UpgradeId.ZERGMELEEWEAPONSLEVEL1,
    UpgradeId.ZERGGROUNDARMORSLEVEL1,
    UpgradeId.OVERLORDSPEED,  # faster scouting overlords, and they escape danger better
    UpgradeId.ZERGMELEEWEAPONSLEVEL2,
    UpgradeId.ZERGGROUNDARMORSLEVEL2,
]

# Army proportions the scripted decisions ask for (first listed = built first when larva is short)
ARMY_COMPOSITION = {
    UnitTypeId.ZERGLING: 1.0,
}
