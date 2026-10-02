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
MELEE_SURROUND_FRACTION = 0.75
MELEE_SPACING = 1.25
MELEE_UNIT_SURROUND_FRACTION = 1.0  # melee attackers per enemy *unit*: about a full ring around it
# A melee attacker that doesn't get PROGRESS_STEP closer to its target within BLOCKED_SECONDS while out
# of range is blocked: it skips that target for BLOCKED_MEMORY_SECONDS (another target, or around)
BLOCKED_SECONDS = 1.5
PROGRESS_STEP = 0.5
BLOCKED_MEMORY_SECONDS = 6.0
FLANK_DISTANCE = 4.0  # blocked with nothing else reachable: go this far around to the target's side
BREAKTHROUGH_RANGE = 3.0  # fighters full/blocked: attack a structure this close (edge) to break through
# A zergling blocked on an enemy unit first tries a detour to it around our own crowd (planned on a grid
# where nearby friendly units cost DETOUR_CROWD_COST and its jammed spot DETOUR_JAM_COST); only if the
# detour is longer than DETOUR_MAX_FACTOR x direct + DETOUR_MAX_EXTRA does it break through instead
DETOUR_CROWD_COST = 20
DETOUR_JAM_COST = 100
DETOUR_MAX_FACTOR = 3.0
DETOUR_MAX_EXTRA = 20.0
DETOUR_SECONDS = 6.0  # follow a detour at most this long
REACH_SECONDS = 1.0  # rebuild the reachability map (which enemy units melee can get to) this often
REACH_WINDOW_MARGIN = 12  # the map covers our units and nearby enemies plus this many tiles
CLEAR_THE_WAY_RANGE = 10  # no reachable fighter: attack the closest structures within this range
MIN_OPEN_SIDES = 3  # an enemy unit is worth engaging in melee only with this many reachable cells around it
LEVEL_TOLERANCE = 1.0  # terrain z within this counts as the same level (cliff levels differ by ~2)
EXPOSURE_MARGIN = 0.5  # a spot is exposed within an enemy's weapon range + sizes + this
# With enemy fighters around: surroundable fighters before structures we can hit for free (True),
# or free structures first (False)
FIGHTERS_FIRST = True
# Coordinated arrival (`Assault` in bot/army.py): zerglings going for enemy fighters gather at the last
# safe cell before them and go in together once ASSAULT_MIN (or ASSAULT_SHARE of them) are there, or
# after ASSAULT_MAX_WAIT seconds; then they stay committed for ASSAULT_COMMIT_SECONDS
ASSAULT_MIN = 12
ASSAULT_SHARE = 0.6
ASSAULT_MAX_WAIT = 8.0
ASSAULT_COMMIT_SECONDS = 6.0
ASSAULT_GATHER_RADIUS = 4.0
# How melee attackers spread over targets (structures and units): "back" = first ones in go deepest,
# later ones fill toward the front (keeps ramps and chokes flowing); "nearest" = closest target with
# room; "off" = no slots (closest target)
TARGET_FILL = "back"
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
