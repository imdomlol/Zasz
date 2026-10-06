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
# Enemies a squad looks at: out to THREAT_LOOKUP (the longest weapon reach, a sieged tank's ~14.5, plus
# the next-step and hold margins; `can_reach_soon` decides which of them can actually hit us). Fights
# worth going for are noticed among visible armed units within NOTICE_RANGE
THREAT_LOOKUP = 18
NOTICE_RANGE = 14
# After a squad is turned back by a fight the sim says we'd lose, attack targets inside that enemy
# group's reach (its spread + weapon range + AVOID_MARGIN) are skipped for AVOID_SECONDS: try other
# bases instead of walking back into it
AVOID_SECONDS = 45
AVOID_MARGIN = 3
# Enemies only count as a threat to a squad within their weapon range + this of one of its units:
# what can hit us within about the next step. An army still walking in doesn't make a squad back off
# from what it's doing (workers it's killing, a fight it's winning); it counts once it's in range
VISIBLE_THREAT_MARGIN = 1.0
MEMORY_THREAT_MARGIN = 1.0
# Squads (`bot/squads.py`): lasting groups of zerglings, each with its own job
SQUAD_SIZE = 8  # zerglings per squad: new ones fill a squad up to this, then start the next one
SQUAD_MAX = 12  # a squad bigger than this (out of combat) splits in two
JOIN_RADIUS = 12  # a new zergling joins a squad (with room) whose center is this close
MERGE_RADIUS = 8  # two squads (out of combat) this close that fit in one squad merge
PLAN_SECONDS = 2.0  # re-assign raids this often
ENEMY_GROUP_RADIUS = 10  # enemy units this close together are one group
RAID_MIN = 6  # smallest fighting force; the sim's estimate of what a fight needs grows in steps of this
SITE_RADIUS = 12  # enemy structures this close together are one site (a base, a proxy)
SITE_DEFENSE_RADIUS = 15  # enemy army units this close to a site defend it
STAGING_MARGIN = 6  # squads raiding a defended site gather this far outside its defenders' range...
GATHERED_FRACTION = 0.8  # ...and go in once this share of the zerglings assigned to it is there
# Patrol: squads not fighting go where we've had no vision the longest (expansions and a grid over
# the map), nearest first among equally stale spots
PATROL_GRID = 20  # spacing of the patrol grid's spots
PATROL_STALE_CAP = 120.0  # seconds; staler than this counts the same
PATROL_DISTANCE_COST = 0.5  # seconds of staleness one tile of distance is worth
PATROL_CLAIM_RADIUS = 15  # squads don't pick spots this close to another squad's spot
PATROL_FRESH = 30.0  # a spot one of our units saw this recently isn't worth a patrol
# Helping (`bot/squads.py`, `choose_helpers`): a squad in a fight the sim calls worse than
# HELP_WIN_AT gets help, if more zerglings fit around its targets (melee slots x HELP_ROOM_FACTOR,
# room for replacements) and some set of free squads within HELP_RANGE makes it a win
HELP_WIN_AT = EngagementResult.VICTORY_CLOSE
HELP_ROOM_FACTOR = 1.5
HELP_RANGE = 50
HELP_SECONDS = 0.5  # re-check every fight this often
# While attacking, at most this many squads patrol, and only with at least PATROL_MIN_SQUADS squads (a
# small army attacks with everything); every other squad without a job joins a raid, or (nothing known
# to raid) pushes toward their natural, then main, then other bases, as one group
MAX_PATROLS_ATTACKING = 1
PATROL_MIN_SQUADS = 4
REGIONS_SECONDS = 5.0  # re-label the walkable regions (what our army can walk to) this often
# Strikes (`Army.update_strikes`): enemies shooting our units that we can walk to get the fewest of
# the nearest zerglings (walking distance, up to STRIKE_RANGE) that win, re-checked every
# STRIKE_SECONDS; they gather within STRIKE_GATHER_RADIUS of a spot outside the enemies' reach and go
# in together (or after STRIKE_GATHER_SECONDS)
STRIKE_SECONDS = 0.5
STRIKE_RANGE = 40
STRIKE_GATHER_RADIUS = 4
STRIKE_GATHER_SECONDS = 15.0
RAID_TOGETHER = 10  # zerglings this close to their group's center are already together (no need to gather)
RALLY_SECONDS = 25.0  # squads gathering to take a fight together give up after this long
BACK_OFF_MARGIN = 2.0  # backing off: this far past the enemy's actual weapon range
BACK_OFF_HOLD_SECONDS = 4.0  # after backing off, hold outside that line this long (unless the fight turns)
# Unseen fire: damage no enemy seen in the last UNSEEN_EXPLAIN_SECONDS (within its weapon reach) could
# have dealt: cloaked or burrowed attackers. Hits within UNSEEN_RADIUS of each other in the last
# UNSEEN_WINDOW seconds form a circle. A force retreats (UNSEEN_FIRE_STEP back, and avoids the area)
# only on at least UNSEEN_MIN_HITS hits adding up to UNSEEN_DAMAGE_SHARE of the health of our zerglings
# around them: one stray hit (an enemy dying mid-attack) doesn't send everyone home
UNSEEN_EXPLAIN_SECONDS = 1.0
UNSEEN_RADIUS = 6.0
UNSEEN_WINDOW = 8.0
UNSEEN_MIN_HITS = 2
UNSEEN_DAMAGE_SHARE = 0.25
UNSEEN_FIRE_STEP = 8.0
# An enemy we can see standing where we have no vision (revealed by firing at us from the fog, e.g. a
# sieged tank): what's around it is unknown. Avoid its reach + PARTLY_SEEN_MARGIN until we see the
# spot (an overlord is sent), and don't fight it meanwhile
PARTLY_SEEN_MARGIN = 4
SCOUTED_SECONDS = 60.0  # an expansion a scouting squad has looked at isn't scouted again for this long
LING_SCOUT_MIN_SECONDS = 6.0  # a squad held back by a partly seen position scouts if the overlord is this far off
PARTLY_SEEN_RANGE = 7  # ...only enemies with at least this weapon range (they fire from beyond our sight)
PARTLY_SEEN_SECONDS = 20  # ...and the area is held at most this long
# Moving somewhere we can't see (SPREAD_LOOKAHEAD ahead): an open formation against splash, rows of
# up to SPREAD_ROW, SPREAD_SPACING apart
SPREAD_LOOKAHEAD = 10
SPREAD_ROW = 6
SPREAD_SPACING = 2.0
# Melee attackers per structure (`bot.targeting`): they fill this fraction of the ring around it,
# MELEE_SPACING attacker-widths apart; the rest pick the next structure, deeper in. 1.0: shoulder to
# shoulder (the wall_bunker debug count saw 5 zerglings hitting a bunker's open face at once; 1.25 gave 4)
MELEE_SURROUND_FRACTION = 0.75
MELEE_SPACING = 1.0
MELEE_UNIT_SURROUND_FRACTION = 1.0  # melee attackers per enemy *unit*: about a full ring around it
# A melee attacker that doesn't get PROGRESS_STEP closer to its target within BLOCKED_SECONDS while out
# of range is blocked: it skips that target for BLOCKED_MEMORY_SECONDS (another target, or around)
BLOCKED_SECONDS = 1.5
PROGRESS_STEP = 0.5
TARGET_MOVED = 1.0  # ...unless the target itself moved this far meanwhile: chasing isn't being blocked
# A slot held by an attacker more than SLOT_HOLD_GAP from its target (edge to edge: walking up, or stuck
# behind others) goes to an attacker that wants it and is SLOT_TAKE_OVER_MARGIN closer (`Assignments`)
SLOT_HOLD_GAP = 1.0
SLOT_TAKE_OVER_MARGIN = 1.0
# Walk around enemy units to a target only if that's this much longer than walking straight there
# (a zealot plugging the direct lane); otherwise a plain attack order is as good and follows the target
APPROACH_DETOUR_SLACK = 2.0
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
PLUG_HARM_RANGE = 2  # a unit blocking our way that can hit us from there (range >= this) is fought anyway
# A melee unit (range < PLUG_HARM_RANGE) with at most CHOKE_SLOTS open sides is a choke fight: taken
# only if the zerglings we'd lose killing it (that many at a time, under the fire of everything covering
# it) are worth no more than it (its cost in zerglings). Otherwise step out of its reach and do other
# work, or do something else
CHOKE_SLOTS = 2
# The breach target is kept while it's acceptable; another one has to be this much cheaper to switch.
# A commitment not confirmed for BREACH_KEEP_SECONDS lapses (the fight moved on)
BREACH_SWITCH_SAVING = 0.2
BREACH_KEEP_SECONDS = 10
# The breach is priced once per fight for the whole army: from all our army units within BREACH_VIEW_RANGE
# of the enemy fighters, fights told apart by which BREACH_VIEW_CELL-sized cell their center falls in
BREACH_VIEW_RANGE = 30
# Widening: breaking a building to open more sides of a target we can already reach (a bunker in a
# wall). Only for targets that stood still WIDEN_STILL_SECONDS (buildings always), only if the whole
# plan (break it, then kill the target) is estimated at WIDEN_MAX_SECONDS or less and at least
# BREACH_SWITCH_SAVING cheaper than head on. A plan older than WIDEN_MAX_SECONDS is given up, and that
# target gets none for WIDEN_GIVE_UP_SECONDS
WIDEN_STILL_SECONDS = 5.0
WIDEN_MAX_SECONDS = 30.0
WIDEN_GIVE_UP_SECONDS = 60.0
BREACH_VIEW_CELL = 20
# Safe work: a cell is in danger if an armed enemy's reach (weapon range + both radii) plus
# SAFE_WORK_MARGIN covers it. Structures outside the breach are worked only from safe spots next to
# them (`ReachMap.attack_spots`), one zergling per spot, walked to around danger
SAFE_WORK_MARGIN = 1.0
BREACH_DANGER_COST = 10.0  # walking to the breach: a step in enemy reach costs this many normal steps
# An enemy unit counts as active (it'll join a fight) once it has moved ACTIVE_MOVE, turned ACTIVE_TURN
# radians (units on hold turn to shoot) or dealt a hit we can match to it; idle again after
# ENEMY_IDLE_SECONDS. Idle supporters (like a forgotten group) don't count against a breach
ACTIVE_MOVE = 0.5
ACTIVE_TURN = 0.3
ENEMY_IDLE_SECONDS = 20
LEVEL_TOLERANCE = 1.0  # terrain z within this counts as the same level (cliff levels differ by ~2)
# How melee attackers spread over targets (structures and units): "back" = first ones in go deepest,
# later ones fill toward the front (keeps ramps and chokes flowing); "nearest" = closest target with
# room; "off" = no slots (closest target)
TARGET_FILL = "back"
# A squad takes a fight the sim calls at least FIGHT_AT, and once in, stays until it calls DISENGAGE_AT or
# worse (hysteresis: no flip-flopping on a close fight). The sim grades by the winner's health left:
# VICTORY_MARGINAL = we win with > 20% left, LOSS_DECISIVE = they win with > 60% left
FIGHT_AT = EngagementResult.VICTORY_MARGINAL
DISENGAGE_AT = EngagementResult.LOSS_DECISIVE
# Leave a sieged tank out of the combat sim once this many zerglings are inside its minimum range
# (the sim ignores minimum range). Judgement call: revert (set very high) if it causes big mistakes.
TANK_NEUTRALIZED_BY = 3
UNDER_FIRE_SECONDS = 1.5  # a unit damaged this recently counts as under fire
# Enemy bunkers: their cargo isn't visible to us, so each counts as BUNKER_MARINES marines shooting
# from it (MARINE_DPS each, at MARINE_RANGE + 1)
BUNKER_MARINES = 4
MARINE_DPS = 9.8  # 6 damage every 0.61 s (no upgrades)
MARINE_RANGE = 5
MARINE_HP = 45
ZERGLING_DPS_VS_MARINE = 10.0  # 5 damage every 0.497 s (no upgrades, marines have no armor)

# --- Queens and defense ---
CREEP_QUEENS = 2  # queens beyond one per hatchery, used to spread creep
QUEEN_DEFEND_RANGE = 12  # queens fight enemies this close while near our bases
SPORES_PER_BASE = 1  # built in each mineral line once the enemy shows air
EXPANSION_RECHECK_SECONDS = 10  # how often to re-check whether a blocked expansion is reachable again

# --- Scouting ---
OVERLORD_SCOUTS = 4  # overlords posted near enemy expansions (natural, third, fourth, ...)
SCOUT_POST_OFFSET = 9  # how far from the expansion spot an overlord waits, toward our side
SCOUT_POST_COOLDOWN = 90  # seconds to avoid a post after an overlord got shot there
SCOUT_REQUEST_SECONDS = 60  # the army's requests for vision of a spot expire after this long

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
