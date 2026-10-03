"""Army control: squads, fight-or-retreat, and per-unit target selection.

Zerglings get the ATTACKING role when they hatch (see `ZaszBot.on_unit_created`). ares groups
them into squads; each squad then:

1. Fights if enemies are close and the combat sim predicts at least an even fight (or we're
   defending a base). Zerglings dive siege tanks first instead of trading with them from the
   edge of their range.
2. Retreats as soon as the sim predicts a loss, but only until it's out of known enemy range,
   then re-engages if the fight looks winnable. Flip-flopping keeps pressure on; the rule is
   that units never stand in enemy fire without either fighting or leaving.
3. Otherwise regroups with the main squad before moving on, so we don't trickle in.

Threats are looked up around every unit in the squad (not just its center), and units that
are being shot from out of sight count as "under fire", so long-range units like sieged tanks
can't hit the front of the squad unnoticed.

Whether to attack at all is a decision (`Decision.attack`); this module only executes.
"""

from collections import Counter
from dataclasses import dataclass

import numpy as np
from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, AttackTarget, PathUnitToTarget, ShootTargetInRange
from ares.behaviors.combat.individual.combat_individual_behavior import CombatIndividualBehavior
from ares.consts import TOWNHALL_TYPES, WORKER_TYPES, EngagementResult, UnitRole, UnitTreeQueryType
from loguru import logger
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit
from scipy.ndimage import label

from bot import overlay
from bot.settings import (
    APPROACH_DISTANCE,
    AVOID_MARGIN,
    AVOID_SECONDS,
    BACK_OFF_HOLD_SECONDS,
    BACK_OFF_MARGIN,
    BREAKTHROUGH_RANGE,
    BUNKER_MARINES,
    CLEAR_THE_WAY_RANGE,
    DEFEND_RADIUS,
    DISENGAGE_AT,
    ENEMY_GROUP_RADIUS,
    ENGAGE_RANGE,
    FIGHT_AT,
    FIGHTER_PRIORITY_RANGE,
    FLANK_DISTANCE,
    GATHERED_FRACTION,
    HELP_RANGE,
    HELP_ROOM_FACTOR,
    HELP_SECONDS,
    HELP_WIN_AT,
    LEVEL_TOLERANCE,
    MARINE_DPS,
    MARINE_RANGE,
    MAX_PATROLS_ATTACKING,
    MEMORY_THREAT_MARGIN,
    MIN_OPEN_SIDES,
    PARTLY_SEEN_MARGIN,
    PARTLY_SEEN_RANGE,
    PARTLY_SEEN_SECONDS,
    PATROL_FIGHT_AT,
    PATROL_MIN_SQUADS,
    PLAN_SECONDS,
    RAID_MIN,
    RALLY_SECONDS,
    REACH_SECONDS,
    REGIONS_SECONDS,
    SITE_DEFENSE_RADIUS,
    SITE_RADIUS,
    SPREAD_LOOKAHEAD,
    SPREAD_ROW,
    SPREAD_SPACING,
    SQUAD_SIZE,
    STAGING_MARGIN,
    STRIKE_GATHER_RADIUS,
    STRIKE_GATHER_SECONDS,
    STRIKE_RANGE,
    STRIKE_SECONDS,
    TANK_NEUTRALIZED_BY,
    TARGET_FILL,
    UNDER_FIRE_SECONDS,
    UNSEEN_FIRE_RADIUS,
    UNSEEN_FIRE_STEP,
    VISIBLE_THREAT_MARGIN,
)
from bot.squads import (
    Battle,
    EnemyGroup,
    Patrol,
    Rally,
    Site,
    Squad,
    SquadBook,
    Strike,
    center_of,
    choose_helpers,
    cluster,
    enemy_groups,
    enemy_sites,
    needed,
    wins,
)
from bot.targeting import BlockTracker, Detours, ReachMap, TargetSlots, attack_capacity, pick_tiered

ARMY_TYPES = {UnitTypeId.ZERGLING}
# Debug overlay: this step's back-off lines (an enemy's position, how far from it we back off to),
# and each candidate target's priority (its targeting tier, 1 = first) and how many attackers it takes
BACK_OFF_CIRCLES: list[tuple[Point2, float]] = []
TARGET_NOTES: dict[int, tuple[int, int | None]] = {}  # enemy tag -> (priority, capacity)
COUNTS: Counter[str] = Counter()  # debug: zergling decisions since the last log (see Army.debug)
SIEGE_MIN_RANGE = 2.0  # sieged tanks can't fire at targets this close
IGNORED_THREATS = {UnitTypeId.LARVA, UnitTypeId.EGG, UnitTypeId.OVERLORD, UnitTypeId.OVERSEER}
# Long-range or splash units zerglings should dive first rather than trade with from range
DIVE_FIRST = {UnitTypeId.SIEGETANKSIEGED, UnitTypeId.SIEGETANK, UnitTypeId.LURKERMPBURROWED, UnitTypeId.COLOSSUS}
# Structures worth killing first once their defenders are dead: economy, then power/production
PRIORITY_STRUCTURES = TOWNHALL_TYPES | {UnitTypeId.PYLON, UnitTypeId.SPAWNINGPOOL}


@dataclass
class MoveTo(CombatIndividualBehavior):
    """Plain move to a point (a step along our own path)."""

    unit: Unit
    target: Point2

    def execute(self, ai: AresBot, config: dict, mediator) -> bool:
        self.unit.move(self.target)
        return True


class Army:
    """Squad jobs (`bot.squads`) and the per-unit memory that stops squads flip-flopping between
    fight and retreat."""

    def __init__(self) -> None:
        self.last_damaged: dict[int, float] = {}  # unit tag -> last time it took damage
        # Where squads were turned back by a fight the sim says we'd lose: (enemy position, until).
        # Sites and patrol spots near these are skipped for a while, so squads try elsewhere
        # instead of walking back into the same wall as soon as the enemy leaves vision.
        self.turned_back_at: list[tuple[Point2, float, float]] = []  # (center, radius, until)
        self.book = SquadBook()  # which zergling is in which squad
        self.patrol = Patrol()  # where we've had no vision the longest
        self.battles: dict[int, Battle] = {}  # squad id -> its fight, last step
        self.engaged: set[int] = set()  # tags of our units committed to fights this step
        self.taken: dict[int, int] = {}  # target tag -> attackers this step, across all squads
        self.rallies: dict[int, Rally] = {}  # losing squad id -> where it and its helpers gather
        self.strikes: list[Strike] = []  # enemy groups shooting us, and the zerglings sent for them
        self.backed_off: dict[int, float] = {}  # squad id -> until when it holds outside the back-off line
        self.fighting: set[int] = set()  # squads that took a fight (they stay until DISENGAGE_AT)
        self.partly_seen: set[int] = set()  # enemies seen on ground we have no vision of (`find_partly_seen`)
        self.struck_at = -999.0
        self.planned_at = -999.0
        # For the debug overlay (`bot.overlay`) only: what was decided, to draw
        self.site_notes: list[tuple[Point2, str]] = []  # raid sites: (center, 'raid A: 16/24 in')
        self.site_letters: dict[Point2, str] = {}  # raid site center -> its letter (kept across plans)
        self.breach_notes: dict[int, tuple[Point2, str, float]] = {}  # structure -> (where, text, when)
        self.avoid_reasons: dict[Point2, str] = {}  # avoid area center -> why
        self.regions = None  # labelled walkable regions of the ground grid (`can_walk_to`)
        self.regions_at = -999.0
        self.helped_at = -999.0
        self.gathered: set[Point2] = set()  # defended sites (centers) whose raiders have gathered
        self.blocks = BlockTracker()  # which zerglings are stuck behind others, per target
        self.detours = Detours()  # routes around our own crowd for blocked zerglings
        self.reach_cache: dict[tuple[int, int], tuple[float, ReachMap]] = {}  # area -> (built at, map)
        # Debug: count what zerglings decide, logged every few seconds (scenarios switch this on)
        self.debug = False
        self.counts = COUNTS
        self.counts_logged_at = 0.0
        self.helps: list[tuple[float, int, int]] = []  # (time, helper squad, helped squad), for scenarios
        # Committed: attack and keep fighting whatever the combat sim says (no avoid areas, no
        # retreats). Off in games; scenarios that test execution against a fight the sim calls a
        # loss switch it on
        self.committed = False

    def on_damage(self, ai: AresBot, unit: Unit) -> None:
        self.last_damaged[unit.tag] = ai.time

    def control(self, ai: AresBot, attacking: bool) -> None:
        """Every squad does its job (`bot.squads`): defend if enemies are at our bases; otherwise
        raid enemy sites while attacking, help squads in fights they'd lose, and patrol (scout)
        the rest of the time."""
        alive = {u.tag for u in ai.units}
        self.clear_empty_avoid_areas(ai)
        self.partly_seen = self.find_partly_seen(ai)
        BACK_OFF_CIRCLES.clear()
        TARGET_NOTES.clear()
        self.blocks.forget_dead(alive)
        self.detours.forget_dead(alive)
        if not self.patrol.spots:
            self.patrol.setup(ai)
        self.patrol.update(ai)
        attacking = attacking or self.committed
        lings = [u for u in ai.mediator.get_units_from_role(role=UnitRole.ATTACKING) if u.type_id in ARMY_TYPES]
        busy = set(self.battles) | {s.id for s in self.book.squads.values() if s.job == "help"}
        squads = self.book.update(lings, busy)
        self.log_debug(ai, squads)

        home_threats = [
            u for u in ai.mediator.get_main_ground_threats_near_townhall if u.type_id not in IGNORED_THREATS
        ]
        if home_threats:
            # Defending always comes first: every squad goes for the threat
            for squad in squads:
                squad.job, squad.target = "defend", Point2(home_threats[0].position)
        else:
            for squad in squads:
                if squad.job == "defend" or (squad.job in ("raid", "push") and not attacking):
                    squad.free()
            if attacking and ai.time - self.planned_at >= PLAN_SECONDS:
                self.planned_at = ai.time
                if not self.committed:
                    self.avoid_armies_we_cannot_beat(ai, lings)
                self.assign_raids(ai, squads)
            if ai.time - self.helped_at >= HELP_SECONDS:
                self.helped_at = ai.time
                self.coordinate_help(ai, squads)

        self.update_rallies(ai, squads)
        # Our units that will actually fight: squads fighting, raiding, helping, engaging or defending
        # (not a patrol standing nearby, nor squads waiting at a rally). A squad judges its fight with
        # these only
        self.engaged = {
            t
            for s in squads
            if s.job in ("raid", "push", "help", "engage", "defend") or (s.id in self.battles and s.job != "gather")
            for t in s.tags
        }
        self.taken.clear()  # target slots, shared by every squad this step
        self.update_strikes(ai, lings)
        striking = {t for s in self.strikes for t in s.lings}
        self.engaged |= striking
        battles: dict[int, Battle] = {}
        for squad in squads:
            # Zerglings on a strike are the strike's this step; the squad runs the rest
            squad.units = [u for u in squad.units if u.tag not in striking]
            if not squad.units:
                continue
            target, advancing = self.squad_goal(ai, squad, squads)
            # Not attacking, a patrol only takes fights it wins big (scouting, not looking for trouble)
            fight_at = FIGHT_AT if attacking or squad.job != "patrol" else PATROL_FIGHT_AT
            if battle := self.control_squad(ai, squad, target, advancing, fight_at):
                battles[squad.id] = battle
        self.battles = battles
        self.execute_strikes(ai)
        if overlay.enabled():
            overlay.draw(ai, self)

    def log_debug(self, ai: AresBot, squads: list[Squad]) -> None:
        if not self.debug or ai.time - self.counts_logged_at < 3:
            return
        self.counts_logged_at = ai.time
        jobs: Counter[str] = Counter()
        for s in squads:
            jobs[s.job] += len(s.tags)
        mode = " ".join(f"{job} {n}" for job, n in sorted(jobs.items()))
        lings = sum(len(s.tags) for s in squads)
        seen = Counter(u.type_id.name.lower() for u in ai.enemy_units if not u.is_memory)
        seen.update(s.type_id.name.lower() for s in ai.enemy_structures if not s.is_memory)
        summary = ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items()))
        enemy = " ".join(f"{k} {v}" for k, v in sorted(seen.items()))
        raids = "; ".join(text for _, text in self.site_notes)
        logger.info(
            f"{ai.time_formatted} army debug: {len(squads)} squads ({mode}), {lings} zerglings vs {enemy}"
            f" | raids: {raids or 'none'} | {summary}"
        )
        self.counts.clear()

    def squad_goal(self, ai: AresBot, squad: Squad, squads: list[Squad]) -> tuple[Point2, bool]:
        """Where this squad is headed, and whether it's advancing (attacking its way there) or just
        moving (patrol: around known enemy fire, backing off when shot)."""
        if squad.job == "defend":
            return squad.target, True
        if squad.job == "gather":
            return squad.target, False
        if squad.job == "engage":
            return squad.target, True
        if squad.job == "push":
            if squad.squad_position.distance_to(squad.target) < 6 or self.patrol.fresh(squad.target, ai.time):
                squad.target = self.push_target(ai)  # seen it: on to the next
            return squad.target, True
        if squad.job == "help":
            battle = self.battles.get(squad.helping) or self.battles.get(squad.id)
            if battle is None:
                squad.free()
            else:
                squad.target = battle.center
                return squad.target, True
        if squad.job == "raid" and squad.target is not None:
            return squad.target, True
        # Patrol: keep the current spot until we get there or something of ours has seen it lately (or
        # it turns dangerous), then the next
        if (
            squad.target is None
            or squad.squad_position.distance_to(squad.target) < 4
            or self.patrol.fresh(squad.target, ai.time)
            or self.blocked(ai, squad.target)
        ):
            claimed = [s.target for s in squads if s is not squad and s.job == "patrol" and s.target is not None]
            groups = enemy_groups(ai)

            def dangerous(p: Point2) -> bool:
                # Scouting, not fighting: stay out of reach of every known enemy army (and don't try
                # for spots we can't walk to, e.g. behind a mineral wall)
                return (
                    self.blocked(ai, p)
                    or not self.can_walk_to(ai, p)
                    or any(p.distance_to(g.center) < g.spread + g.reach + STAGING_MARGIN for g in groups)
                )

            spot = self.patrol.pick(ai.time, squad.squad_position, claimed, dangerous)
            squad.target = spot or rally_point(ai)
        return squad.target, False

    def assign_raids(self, ai: AresBot, squads: list[Squad]) -> None:
        """Enemy sites get as many zerglings as the sim says they need (or, undefended, as many as
        fit around their structures), from the nearest free squads; outlying sites first, their main
        last. Sites we can't staff are left alone (and avoided if defended)."""
        groups = enemy_groups(ai)
        structures = [s for s in ai.enemy_structures if not s.is_flying and not self.blocked(ai, s.position)]
        sites = [
            s
            for s in enemy_sites(ai, structures, groups)
            if not self.blocked(ai, s.center) and self.site_reachable(ai, s)
        ]
        # (Nothing known at their main is a patrol spot, not a raid: walking a raid into the unknown
        # loses zerglings to whatever sits there, e.g. a sieged tank)
        main = ai.enemy_start_locations[0]
        sites.sort(key=lambda s: -s.center.distance_to(main))
        self.site_notes = []
        self.label_sites(sites)

        # Sites move a little as structures die or get found (the center shifts): a raid follows its
        # site to the nearest one still there
        def same_site(spot: Point2, tags: set[int] | None = None) -> Site | None:
            # The site with any of the same enemy units or structures (it may have moved a lot: an
            # army walking off, a building at one end of a cluster dying), else the nearest one
            if tags and (kept := [s for s in sites if tags & {u.tag for u in s.defenders + s.structures}]):
                return min(kept, key=lambda s: s.center.distance_to(spot))
            near = [s for s in sites if s.center.distance_to(spot) < SITE_RADIUS]
            return min(near, key=lambda s: s.center.distance_to(spot)) if near else None

        for squad in squads:
            if squad.job == "raid":
                if (site := same_site(squad.site, squad.site_tags)) is None and squad.id in self.battles:
                    COUNTS["raid kept: fighting"] += 1
                    continue  # mid-fight (e.g. killing workers as their army walks in): keep at it
                if site is None:
                    blocked = any(self.blocked(ai, s.position) for s in ai.enemy_structures if s.tag in squad.site_tags)
                    COUNTS["raid dropped: site avoided" if blocked else "raid dropped: site gone"] += 1
                    squad.free()
                else:
                    squad.site = site.center
                    squad.site_tags = {u.tag for u in site.defenders + site.structures}
        raiding = [s.site for s in squads if s.job == "raid"]
        # Everyone who gathered at a site is gone: gather again first
        self.gathered = {
            site.center for g in self.gathered if (site := same_site(g)) is not None and site.center in raiding
        }
        for s in squads:
            if s.job == "push":
                s.free()  # re-planned below: joins a raid if there's one now, else pushes on
        free = [s for s in squads if s.job in ("patrol", "raid") and s.id not in self.battles]
        for site in sites:
            on_it = [s for s in free if s.job == "raid" and s.site == site.center]
            if any(b.center.distance_to(site.center) < SITE_DEFENSE_RADIUS for b in self.battles.values()):
                continue  # a fight is on there: reinforcing it is for the help logic, not one squad at a time
            others = sorted(
                (s for s in free if s.job == "patrol"), key=lambda s: s.squad_position.distance_to(site.center)
            )
            # Squads already raiding it count, fighting or not (left out, a raid in full swing looked
            # like one patrol squad against the defenders)
            fighting = [s for s in squads if s.job == "raid" and s.site == site.center and s not in on_it]
            pool = [u for s in on_it + fighting + others for u in s.units]
            if not pool:
                break
            if site.defenders:
                # Enough to win comfortably (fewer just-win zerglings means more of them lost), or at
                # least to win
                demand = needed(ai, pool, site.defenders, site.center, HELP_WIN_AT) or needed(
                    ai, pool, site.defenders, site.center
                )
                if demand is None:
                    # Can't staff it now: skip it this round (not an avoid area: only the whole army
                    # failing to beat a group makes one, see `avoid_armies_we_cannot_beat`)
                    COUNTS["raid skipped: not enough"] += 1
                    for s in on_it:
                        s.free()
                    continue
            elif site.structures:
                # Undefended: as many as fit around the structures
                demand = max(RAID_MIN, sum(attack_capacity(pool[0], s) or SQUAD_SIZE for s in site.structures))
            else:
                demand = RAID_MIN
            staffed = sum(len(s.tags) for s in on_it + fighting)
            for s in others:
                if staffed >= demand:
                    break
                s.job, s.site, s.helping = "raid", site.center, None
                s.site_tags = {u.tag for u in site.defenders + site.structures}
                on_it.append(s)
                staffed += len(s.tags)
            target = site.center
            if site.defenders and site.center not in self.gathered:
                # Gather outside the defenders' range, then go in together
                group = EnemyGroup(
                    site.defenders,
                    center_of(site.defenders),
                    max(u.ground_range + u.radius for u in site.defenders),
                    True,
                    max(u.position.distance_to(center_of(site.defenders)) for u in site.defenders),
                )
                staging = group.outside(rally_point(ai), STAGING_MARGIN)
                there = sum(1 for s in on_it for u in s.units if u.position.distance_to(staging) < 12)
                if there >= GATHERED_FRACTION * staffed:
                    self.gathered.add(site.center)
                else:
                    target = staging
            for s in on_it:
                s.target = target
            free = [s for s in free if s not in on_it]
            state = "gathering" if target != site.center else "in"
            self.site_notes.append((site.center, f"raid {self.site_letters[site.center]}: {staffed}/{demand} {state}"))
        self.no_idle_squads(ai, squads, sites, free)

    def label_sites(self, sites: list[Site]) -> None:
        """Letters for the overlay: a site keeps the letter of the previous plan's site within
        SITE_RADIUS of it (sites shift a little as structures die), new ones get the next free one."""
        old = self.site_letters
        new: dict[Point2, str] = {}
        for site in sites:
            near = [c for c in old if c.distance_to(site.center) < SITE_RADIUS and old[c] not in new.values()]
            if near:
                new[site.center] = old[min(near, key=lambda c: c.distance_to(site.center))]
        used = set(new.values())
        letters = (chr(ord("A") + i) for i in range(26))
        for site in sites:
            if site.center not in new:
                letter = next((x for x in letters if x not in used), "?")
                new[site.center] = letter
                used.add(letter)
        self.site_letters = new

    def no_idle_squads(self, ai: AresBot, squads: list[Squad], sites: list[Site], free: list[Squad]) -> None:
        """While attacking, squads with nothing to do don't sit around: at most MAX_PATROLS_ATTACKING
        keep patrolling (none in an army under PATROL_MIN_SQUADS squads), the rest join the nearest
        raid (more zerglings there just take more targets: slots keep them from piling up), or, with
        nothing known to raid, push toward their natural, then their main, then other bases, all to
        the same place so they go as one group."""
        idle = [s for s in free if s.job == "patrol"]
        keep = MAX_PATROLS_ATTACKING if len(squads) >= PATROL_MIN_SQUADS else 0
        patrols = sorted(idle, key=lambda s: len(s.tags))[:keep]
        spare = [s for s in idle if s not in patrols]
        if not spare:
            return
        if sites:
            for s in spare:
                site = min(sites, key=lambda t: t.center.distance_to(s.squad_position))
                s.job, s.site, s.helping, s.target = "raid", site.center, None, site.center
                s.site_tags = {u.tag for u in site.defenders + site.structures}
                COUNTS["joined a raid"] += 1
            return
        target = self.push_target(ai)
        for s in spare:
            s.job, s.site, s.helping, s.target = "push", None, None, target
        COUNTS["pushing"] += len(spare)

    def push_target(self, ai: AresBot) -> Point2:
        """Where spare squads push when nothing is known to raid: their natural, then their main, then
        the other bases, skipping places we've seen lately, avoid areas and places we can't walk to."""
        spots = [ai.mediator.get_enemy_nat, ai.enemy_start_locations[0]]
        spots += [spot for spot, _ in ai.mediator.get_enemy_expansions]
        for spot in spots:
            if self.patrol.fresh(spot, ai.time) or self.blocked(ai, spot) or not self.can_walk_to(ai, spot):
                continue
            return spot
        return ai.enemy_start_locations[0]

    def site_reachable(self, ai: AresBot, site: Site) -> bool:
        """Can we get next to it: ground beside any of its structures (a site's center can be inside a
        walled pocket), or for an army in the field, near its center."""
        if site.structures:
            return any(self.can_walk_to(ai, s.position, int(s.radius) + 2) for s in site.structures)
        return self.can_walk_to(ai, site.center)

    def can_walk_to(self, ai: AresBot, spot: Point2, near: int = 3) -> bool:
        """Is `spot` (or ground within `near` of it) in a walkable region our units are in? Minerals,
        rocks and buildings are walls: a base behind a mineral wall isn't, until it's mined out."""
        if ai.time - self.regions_at >= REGIONS_SECONDS or self.regions is None:
            self.regions_at = ai.time
            walkable = np.isfinite(ai.mediator.get_ground_grid)
            labels, _ = label(walkable, structure=np.ones((3, 3), dtype=bool))
            ours = {int(labels[int(u.position.x), int(u.position.y)]) for u in ai.units if not u.is_flying} - {0}
            self.regions = (labels, ours)
        labels, ours = self.regions
        w, h = labels.shape
        for dx in range(-near, near + 1):
            for dy in range(-near, near + 1):
                x, y = int(spot.x) + dx, int(spot.y) + dy
                if 0 <= x < w and 0 <= y < h and labels[x, y] in ours:
                    return True
        return False

    def coordinate_help(self, ai: AresBot, squads: list[Squad]) -> None:
        """Watch every fight: one the sim calls worse than HELP_WIN_AT, with room around the targets
        for more zerglings, gets the fewest free squads (closest first, within HELP_RANGE) that
        together make it a win; if none do, nobody goes and squads already on their way turn back."""
        for squad in squads:
            if squad.job == "help" and squad.helping not in self.battles and squad.id not in self.battles:
                squad.free()  # the fight we came for is over (and we're not in one ourselves)
            if (
                squad.job == "engage"
                and squad.id not in self.battles
                and squad.squad_position.distance_to(squad.target) < 10
            ):
                squad.free()  # went in together, and it's over
        gathering = {i for r in self.rallies.values() for i in r.squads}
        taken: set[int] = set(gathering)
        by_id = {s.id: s for s in squads}
        for fighting_id, battle in sorted(self.battles.items(), key=lambda kv: kv[1].result):
            if fighting_id in gathering:
                continue  # already waiting for help
            fighting = by_id.get(fighting_id)
            en_route = [s for s in squads if s.job == "help" and s.helping == fighting_id]
            if battle.result >= HELP_WIN_AT or battle.room <= 0:
                for s in en_route:
                    if s.id not in self.battles:
                        s.free()  # doing fine, or no room for more: patrol on
                continue
            present = battle.present + [u for s in en_route for u in s.units]
            room = battle.room - sum(len(s.tags) for s in en_route)
            candidates = sorted(
                (
                    s
                    for s in squads
                    if s.job == "patrol"  # a raiding squad has its own fight to get to
                    and s.id not in self.battles
                    and s.id not in taken
                    and s.squad_position.distance_to(battle.center) < HELP_RANGE
                ),
                key=lambda s: s.squad_position.distance_to(battle.center),
            )
            chosen = choose_helpers(
                present,
                battle.threats,
                [(s, s.units) for s in candidates],
                lambda own, enemies: wins(ai, own, enemies, HELP_WIN_AT),
                room,
                lambda own, enemies: wins(ai, own, enemies, FIGHT_AT),
            )
            if chosen is None:
                for s in en_route:
                    if s.id not in self.battles:
                        s.free()  # nothing in reach turns it: don't throw more in
                if battle.result < FIGHT_AT and fighting is not None and fighting.job != "defend":
                    # Losing, and nothing in reach turns it: keep away from it and patrol elsewhere
                    self.turned_back(ai, battle.threats, "no help possible")
                    fighting.free()
                    COUNTS["no help possible"] += 1
                continue
            if battle.result >= FIGHT_AT or fighting is None or fighting.job == "defend":
                # Winning, just not comfortably: the helpers join the fight as it is
                for s in chosen:
                    s.job, s.helping, s.site, s.target = "help", fighting_id, None, battle.center
                    taken.add(s.id)
                    self.helps.append((ai.time, s.id, fighting_id))
                    COUNTS["help sent"] += 1
                taken.update(s.id for s in en_route)
                continue
            # Losing, but these squads turn it: the losing squad waits outside the enemy's reach and
            # the helpers come to it; everyone goes in together (`update_rallies`)
            reach = max(weapon_range(t) + t.radius for t in battle.threats) + max(
                t.position.distance_to(battle.center) for t in battle.threats
            )
            spot = battle.center.towards(fighting.squad_position, reach + STAGING_MARGIN)
            members = [fighting, *chosen, *en_route]
            self.rallies[fighting_id] = Rally(spot, battle.center, [s.id for s in members], ai.time)
            for s in members:
                s.job, s.helping, s.site, s.target = "gather", None, None, spot
                taken.add(s.id)
            for s in chosen:
                self.helps.append((ai.time, s.id, fighting_id))
            COUNTS[f"rally ({len(chosen)} squads coming)"] += 1

    def update_rallies(self, ai: AresBot, squads: list[Squad]) -> None:
        """Squads gathering to take a fight together: once GATHERED_FRACTION of their zerglings are
        at the spot, all of them go in at once; a rally that takes longer than RALLY_SECONDS is
        called off (the fight gets judged again from scratch)."""
        by_id = {s.id: s for s in squads}
        for key, rally in list(self.rallies.items()):
            members = [by_id[i] for i in rally.squads if i in by_id and by_id[i].job == "gather"]
            if not members:
                del self.rallies[key]
                continue
            total = sum(len(s.tags) for s in members)
            there = sum(1 for s in members for u in s.units if u.position.distance_to(rally.spot) < 8)
            if there >= GATHERED_FRACTION * total:
                for s in members:
                    s.job, s.target = "engage", rally.enemy
                COUNTS["rally: go"] += 1
                del self.rallies[key]
            elif ai.time - rally.since > RALLY_SECONDS:
                for s in members:
                    s.free()
                COUNTS["rally: called off"] += 1
                del self.rallies[key]

    def update_strikes(self, ai: AresBot, lings: list[Unit]) -> None:
        """Enemies shooting our units that we can walk to get dealt with, however far: every
        STRIKE_SECONDS, for each group of armed enemies with any of our zerglings in their reach, find
        the fewest of the nearest zerglings (walking distance, up to STRIKE_RANGE) that win the fight
        (comfortably if they can), at most as many as fit around the enemies we can reach. Those
        zerglings gather at the last spot outside the enemies' reach on the way, and go in together
        once most of them are there (or after STRIKE_GATHER_SECONDS). The others keep their jobs.
        A strike keeps its zerglings while it lasts, and ends when the enemies are dead or out of
        reach, or the fight isn't winnable any more."""
        alive = {u.tag for u in lings}
        for strike in self.strikes:
            strike.lings &= alive
        if ai.time - self.struck_at < STRIKE_SECONDS:
            return
        self.struck_at = ai.time
        armed_enemies = [
            e
            for e in [*ai.enemy_units, *ai.enemy_structures]
            if not e.is_flying
            and not e.is_memory
            and armed(e)
            and e.type_id not in IGNORED_THREATS
            and e.type_id not in WORKER_TYPES
        ]
        strikes: list[Strike] = []
        taken: set[int] = set()
        for group in cluster(armed_enemies, ENEMY_GROUP_RADIUS):
            if not any(
                u.position.distance_to(e.position) < reach_of(e, u) + 1 for e in group if armed(e) for u in lings
            ):
                continue  # not shooting at us
            if any(e.tag in self.partly_seen for e in group):
                COUNTS["strike: partly seen, wait for vision"] += 1
                continue
            center = center_of(group)
            nearby = [u for u in lings if u.position.distance_to(center) < STRIKE_RANGE + 10 and u.tag not in taken]
            if not nearby:
                continue
            obstacles = [e for e in ai.enemy_units if not e.is_flying and e.position.distance_to(center) < 30]
            reach = ReachMap(ai.mediator.get_ground_grid, nearby, obstacles, ai.get_terrain_z_height)
            targets = [e for e in group if reach.open_sides(e) > 0]
            if not targets:
                continue  # sealed in: that's for a breach
            tags = {e.tag for e in group}
            old = next((s for s in self.strikes if s.targets & tags), None)
            distance = {u.tag: reach.walk_to(targets, u.position) for u in nearby}
            candidates = [u for u in nearby if distance[u.tag] <= STRIKE_RANGE]
            # The strike's own zerglings first (it keeps them), then the nearest
            candidates.sort(key=lambda u: (old is None or u.tag not in old.lings, distance[u.tag]))
            probe = candidates[0] if candidates else None
            room = (
                int(
                    sum(min(attack_capacity(probe, t) or len(candidates), reach.open_sides(t)) for t in targets)
                    * HELP_ROOM_FACTOR
                )
                if probe
                else 0
            )
            pool = candidates[: max(0, room)]
            # Who we'd fight: the group, plus any armed enemy whose reach covers it (sieged tanks
            # behind marines): those count in the sim, though only the group is a target
            support = [
                e
                for e in [*ai.enemy_units, *ai.enemy_structures]
                if e.tag not in tags
                and not e.is_flying
                and armed(e)
                and e.type_id not in WORKER_TYPES
                and any(e.position.distance_to(g.position) < weapon_range(e) + e.radius + 2 for g in group)
            ]
            n = self.strike_size(ai, pool, group + support)
            if self.debug:
                seen = Counter(e.type_id.name.lower() + ("?" if e.is_memory else "") for e in group + support)
                verdict = strike_result(ai, pool[:n] if n else pool, group + support).name
                logger.info(
                    f"{ai.time_formatted} strike sizing: vs {dict(seen)} (support {len(support)}), "
                    f"{len(pool)} zerglings in reach, chose {n}, sim {verdict}"
                )
            if n is None:
                COUNTS["strike: not winnable"] += 1
                continue
            chosen = pool[:n]
            shooters = [(e.position, weapon_range(e) + e.radius + 0.375 + BACK_OFF_MARGIN) for e in group if armed(e)]
            spot = old.spot if old is not None and old.spot is not None else reach.gather_spot(targets, shooters)
            strike = Strike(tags, {u.tag for u in chosen}, spot, old.since if old else ai.time, reach)
            strike.go = old is not None and old.go
            # Already fighting them (some of ours in their reach) or they're already hitting us:
            # trickling in costs nothing then, so don't hold anyone back to gather
            in_reach = any(
                u.position.distance_to(e.position) < reach_of(e, u) for e in group if armed(e) for u in lings
            )
            # (Hit by the group itself: our damaged units in its reach. Damage from elsewhere, e.g. tanks
            # behind it, doesn't make trickling in free)
            hit = any(
                ai.time - self.last_damaged.get(u.tag, -99) < UNDER_FIRE_SECONDS
                and any(u.position.distance_to(e.position) < reach_of(e, u) + 1 for e in group if armed(e))
                for u in ai.units
            )
            if not strike.go and (in_reach or hit):
                strike.go = True
                COUNTS["strike: already fighting, go"] += 1
            if not strike.go:
                there = sum(1 for u in chosen if spot is None or u.position.distance_to(spot) < STRIKE_GATHER_RADIUS)
                if there >= GATHERED_FRACTION * len(chosen) or ai.time - strike.since > STRIKE_GATHER_SECONDS:
                    strike.go = True
                    COUNTS[f"strike: go ({len(chosen)})"] += 1
            strikes.append(strike)
            taken |= strike.lings
        self.strikes = strikes

    def strike_size(self, ai: AresBot, pool: list[Unit], group: list[Unit]) -> int | None:
        """Fewest of `pool` (in order) that beat `group`: comfortably if possible, else at all."""
        for at in (HELP_WIN_AT, FIGHT_AT):
            for n in range(min(2, len(pool)), len(pool) + 1, 2):
                if strike_result(ai, pool[:n], group) >= at:
                    return n
            if pool and strike_result(ai, pool, group) >= at:
                return len(pool)
        return None

    def execute_strikes(self, ai: AresBot) -> None:
        by_tag = {u.tag: u for u in ai.units}
        enemies = {e.tag: e for e in [*ai.enemy_units, *ai.enemy_structures]}
        for strike in self.strikes:
            group = [enemies[t] for t in strike.targets if t in enemies]
            units = [by_tag[t] for t in strike.lings if t in by_tag]
            if not group or not units:
                continue
            if not strike.go:
                # Gather out of their reach (step out first if in it)
                COUNTS["strike: gathering"] += len(units)
                back_off(ai, units, group, strike.spot)
                continue
            COUNTS["strike: in"] += len(units)
            center = center_of(group)
            nearby = [e for e in enemies.values() if e.is_structure and e.position.distance_to(center) < 12]
            slots = TargetSlots(strike.reach, taken=self.taken)
            slots.strike = strike.targets
            for unit in front_first(units, group):
                fight(ai, unit, group, group + nearby, slots, center, self.blocks, self.detours)

    def avoid_armies_we_cannot_beat(self, ai: AresBot, lings: list[Unit]) -> None:
        """Mark every known enemy group (seen now or remembered) the whole army can't beat as an
        area to avoid, before walking into it rather than after losing half the zerglings."""
        for group in enemy_groups(ai):
            if not wins(ai, lings, group.units):
                COUNTS[f"avoid group of {len(group.units)}"] += 1
                self.turned_back(ai, group.units, "army can't beat")

    def reach_map(self, ai: AresBot, units: list[Unit], enemies: list[Unit]) -> ReachMap | None:
        """Which enemy units this squad can actually get to; rebuilt every REACH_SECONDS per area."""
        ground = [e for e in enemies if not e.is_flying]
        if not ground:
            return None
        center = center_of(units)
        key = (round(center.x / 10), round(center.y / 10))
        cached = self.reach_cache.get(key)
        if cached is None or ai.time - cached[0] >= REACH_SECONDS:
            cached = (ai.time, ReachMap(ai.mediator.get_ground_grid, units, ground, ai.get_terrain_z_height))
            self.reach_cache[key] = cached
        return cached[1]

    def turned_back(self, ai: AresBot, threats: list[Unit], reason: str = "") -> None:
        """Remember where a squad had to back off: the area those enemies cover (their spread plus
        weapon range), replacing an older entry for the same spot."""
        COUNTS[f"avoid area ({reason})"] += 1
        center = center_of(threats)
        spread = max(t.position.distance_to(center) for t in threats)
        radius = spread + max(weapon_range(t) + t.radius for t in threats) + AVOID_MARGIN
        self.turned_back_at = [(p, r, t) for p, r, t in self.turned_back_at if p.distance_to(center) > radius / 2]
        self.turned_back_at.append((center, radius, ai.time + AVOID_SECONDS))
        self.avoid_reasons[center] = reason

    def find_partly_seen(self, ai: AresBot) -> set[int]:
        """Armed enemies we can see standing on ground we have no vision of: revealed by firing at us
        from the fog (a sieged tank), so whatever stands around them is unknown. Their area becomes an
        avoid area until we see it, and an overlord is sent to look."""
        found = set()
        sight = [(u.position, u.sight_range) for u in ai.units]
        for e in [*ai.enemy_units, *ai.enemy_structures]:
            if (
                e.is_memory
                or e.is_flying
                or not armed(e)
                or e.type_id in WORKER_TYPES
                or weapon_range(e) < PARTLY_SEEN_RANGE  # only long-range units fire from beyond our sight
                or any(e.position.distance_to(p) < r + 1 for p, r in sight)  # in plain view (edge of vision)
            ):
                continue
            found.add(e.tag)
            if not any(
                self.avoid_reasons.get(c) == "scout needed" and c.distance_to(e.position) < r / 2
                for c, r, _ in self.turned_back_at
            ):
                self.mark_avoid(
                    ai, e.position, weapon_range(e) + e.radius + PARTLY_SEEN_MARGIN, "scout needed", PARTLY_SEEN_SECONDS
                )
            if scouting := getattr(ai, "scouting", None):
                scouting.request(ai, e.position)
        return found

    def mark_avoid(
        self, ai: AresBot, center: Point2, radius: float, reason: str, seconds: float = AVOID_SECONDS
    ) -> None:
        """An avoid area of a given size (`turned_back` works it out from the enemies seen)."""
        COUNTS[f"avoid area ({reason})"] += 1
        self.turned_back_at = [(p, r, t) for p, r, t in self.turned_back_at if p.distance_to(center) > radius / 2]
        self.turned_back_at.append((center, radius, ai.time + seconds))
        self.avoid_reasons[center] = reason

    def clear_empty_avoid_areas(self, ai: AresBot) -> None:
        """An avoid area whose center we can see with no armed enemy in it is over: the army that
        made it walked off (it used to stay up its full AVOID_SECONDS)."""
        armed_enemies = [e for e in ai.enemy_units if not e.is_memory and armed(e)]
        kept = []
        for center, radius, until in self.turned_back_at:
            unseen = self.avoid_reasons.get(center) == "unseen fire"  # the shooter isn't at the center
            if self.avoid_reasons.get(center) == "scout needed":
                if ai.is_visible(center):
                    COUNTS["avoid area cleared (scouted)"] += 1
                    continue
                kept.append((center, radius, until))
                continue
            if (
                not unseen
                and ai.is_visible(center)
                and not any(e.position.distance_to(center) < radius for e in armed_enemies)
            ):
                COUNTS["avoid area cleared (seen empty)"] += 1
                continue
            kept.append((center, radius, until))
        self.turned_back_at = kept

    def avoided(self, ai: AresBot) -> list[tuple[Point2, float]]:
        """Active avoid areas: (center, radius)."""
        self.turned_back_at = [(p, r, t) for p, r, t in self.turned_back_at if t > ai.time]
        return [(p, r) for p, r, _ in self.turned_back_at]

    def blocked(self, ai: AresBot, spot: Point2) -> bool:
        return any(spot.distance_to(p) < r for p, r in self.avoided(ai))

    def control_squad(
        self, ai: AresBot, squad: Squad, target: Point2, advancing: bool, fight_at: EngagementResult = FIGHT_AT
    ) -> Battle | None:
        """Fight, or back off just out of the enemy's reach, if enemies are close; otherwise head for
        `target`. Returns the fight (for the other squads to watch), if any."""
        units = squad.squad_units
        enemies = enemies_near(ai, units)
        # Just backed off: the enemies count out to the back-off line (their reach + margin), so the
        # squad holds outside it instead of walking back in and out across their range
        holding = self.backed_off.get(squad.id, -99) > ai.time
        extra = BACK_OFF_MARGIN + 0.5 if holding else 0.0
        threats = [e for e in enemies if (not e.is_structure or armed(e)) and can_reach_soon(e, units, extra)]
        under_fire = any(ai.time - self.last_damaged.get(u.tag, -99) < UNDER_FIRE_SECONDS for u in units)

        if threats:
            near_home = ai.townhalls.closest_distance_to(squad.squad_position) < DEFEND_RADIUS
            if not near_home and not self.committed and any(t.tag in self.partly_seen for t in threats):
                # Only part of their position is seen (something fired from the fog): don't fight
                # what we can see; back off until an overlord shows us the rest
                COUNTS["partly seen: back off"] += len(units)
                self.backed_off[squad.id] = ai.time + BACK_OFF_HOLD_SECONDS
                back_off(ai, units, threats, None, retreat=True)
                return None
            result, present = fight_result(ai, units, threats, self.engaged)
            battle = Battle(
                center_of(threats), threats, present, result, self.room(ai, units, threats, enemies, present)
            )
            if squad.job == "gather" and not near_home and not self.committed:
                # Waiting for help: stay out of their reach (the helpers count in the sim before
                # they're here, so going in now would be alone)
                COUNTS["gathering"] += len(units)
                back_off(ai, units, threats, squad.target)
                return battle
            # Engage at `fight_at` (VICTORY_MARGINAL, or stricter for a patrol not attacking); once in,
            # stay until the sim says LOSS_DECISIVE or worse (a close fight going a bit wrong isn't a
            # reason to turn around and take free hits)
            engaged = squad.id in self.fighting
            if engaged and result > DISENGAGE_AT or not engaged and result >= fight_at:
                self.fighting.add(squad.id)
            elif not (near_home or self.committed):
                self.fighting.discard(squad.id)
            if near_home or self.committed or squad.id in self.fighting:
                reach = self.reach_map(ai, units, enemies)
                slots = TargetSlots(reach, self.breach_caps(ai, reach, units, enemies, threats, present), self.taken)
                for unit in front_first(units, enemies):
                    fight(ai, unit, threats, enemies, slots, squad.squad_position, self.blocks, self.detours)
            elif self.cornered(ai, units, threats, enemies):
                # Nowhere safe to back off to (behind a mineral line, in a dead end, their army
                # between us and safety): do as much damage as we can instead of dying for free
                COUNTS["cornered: fight"] += len(units)
                reach = self.reach_map(ai, units, enemies)
                slots = TargetSlots(reach, taken=self.taken)
                for unit in front_first(units, enemies):
                    fight(ai, unit, threats, enemies, slots, squad.squad_position, self.blocks, self.detours)
            else:
                # Just out of their reach, not home: from there it waits for help or patrols on
                # (and isn't an avoid area: one squad losing isn't the army losing)
                COUNTS[f"back off ({result.name})"] += len(units)
                self.backed_off[squad.id] = ai.time + BACK_OFF_HOLD_SECONDS
                back_off(ai, units, threats, None, retreat=True)
            return battle

        self.fighting.discard(squad.id)  # no enemies close: not in a fight any more
        if under_fire and not self.committed:
            # Shot with nothing we can see in range to have done it: something in the fog (a sieged
            # tank on high ground). Step back whatever the job, and keep away from there for a while:
            # we don't push into fire we can't see
            back = squad.squad_position.towards(rally_point(ai), UNSEEN_FIRE_STEP)
            self.mark_avoid(ai, squad.squad_position, UNSEEN_FIRE_RADIUS, "unseen fire")
            COUNTS["unseen fire: back off"] += len(units)
            for unit in units:
                move(ai, unit, back)
            return None

        # Heading somewhere we can't see: spread out, so splash (tanks, storms, banelings) hits few
        center = squad.squad_position
        if (
            not threats
            and not any(e.is_structure for e in enemies)
            and len(units) > 1
            and center.distance_to(target) > (APPROACH_DISTANCE if advancing else 5)
            and not ai.is_visible(center.towards(target, min(SPREAD_LOOKAHEAD, center.distance_to(target))))
        ):
            COUNTS["spread out"] += len(units)
            spread_move(ai, units, center, target)
            return None

        slots = TargetSlots(self.reach_map(ai, units, enemies) if enemies else None, taken=self.taken)
        for unit in front_first(units, enemies):
            if advancing:
                if threats or any(e.is_structure for e in enemies):
                    fight(ai, unit, threats, enemies, slots, squad.squad_position, self.blocks, self.detours)
                elif under_fire or unit.distance_to(target) < APPROACH_DISTANCE:
                    # Close to the target (or shot from out of sight): attack-move in, all the way
                    # (AMove's default does nothing within 7 of the target: zerglings stood next
                    # to a lone zealot that wasn't a threat yet, doing nothing)
                    if not (unit.is_attacking and unit.order_target == target):
                        ai.register_behavior(AMove(unit=unit, target=target, success_at_distance=0))
                else:
                    # On the way: follow ares' path around known enemy fire (e.g. the army that
                    # turned us back) instead of walking straight through it
                    move(ai, unit, target)
            elif unit.distance_to(target) > 5:
                move(ai, unit, target)
        return None

    def breach_caps(
        self,
        ai: AresBot,
        reach: ReachMap | None,
        units: list[Unit],
        enemies: list[Unit],
        threats: list[Unit],
        present: list[Unit],
    ) -> dict[int, int]:
        """The structure worth breaking to get at enemy fighters we can't reach (`ReachMap.breaches`),
        as {tag: attackers}: exactly as many as can hit it at once (full damage, nobody piling up
        behind). Worth it if, after losing what breaking it costs (its health at that damage rate,
        times the damage the enemies covering it deal), we still win the fight behind it; the
        cheapest such breach. Worked out once per reach map (REACH_SECONDS)."""
        if reach is None:
            return {}
        if reach.breach_caps is not None:
            return reach.breach_caps
        reach.breach_caps = {}
        fighters = [e for e in threats if (not e.is_structure or armed(e)) and e.type_id not in WORKER_TYPES]
        structures = {e.tag: e for e in enemies if e.is_structure and not e.is_flying}
        if not fighters or not structures:
            return reach.breach_caps
        probe = units[0]
        ours = sorted(present, key=lambda u: u.health + u.shield, reverse=True)
        best: tuple[float, int, int] | None = None  # (zerglings lost, tag, attackers)
        for tag, (attackers, _opened) in reach.breaches(list(structures.values()), fighters).items():
            s = structures[tag]
            our_dps = attackers * max(probe.calculate_dps_vs_target(s), 0.1)
            seconds = (s.health + s.shield) / our_dps
            covering = [
                f
                for f in fighters
                if armed(f) and f.position.distance_to(s.position) - s.radius - f.radius < weapon_range(f) + 1.5
            ]
            incoming = sum(dps_vs(f, probe) for f in covering)
            lost = incoming * seconds / max(probe.health_max, 1)
            survivors = ours[: max(0, len(ours) - int(lost + 0.5))]
            COUNTS[f"breach considered (lose ~{lost:.0f})"] += 1
            if wins(ai, survivors, fighters) and (best is None or lost < best[0]):
                best = (lost, tag, attackers)
        if best is not None:
            reach.breach_caps = {best[1]: best[2]}
            chosen = structures[best[1]]
            self.breach_notes[best[1]] = (chosen.position, f"breach: lose ~{best[0]:.0f}, {best[2]} on it", ai.time)
        return reach.breach_caps

    def cornered(self, ai: AresBot, units: list[Unit], threats: list[Unit], enemies: list[Unit]) -> bool:
        """No ground we can walk to (around enemy units, buildings, minerals) out of every threat's
        reach: backing off would only be running into a wall while they shoot us."""
        reach = self.reach_map(ai, units, enemies)
        if reach is None:
            return False
        probe = units[0]
        shooters = [(t.position, reach_of(t, probe) + BACK_OFF_MARGIN) for t in threats if armed(t)]
        return bool(shooters) and not reach.safe_ground(shooters)

    def room(
        self, ai: AresBot, units: list[Unit], threats: list[Unit], enemies: list[Unit], present: list[Unit]
    ) -> int:
        """How many more of our melee units would usefully fit around these threats: their melee
        slots (capped by the open cells around each, see `TargetSlots`) x HELP_ROOM_FACTOR, minus our
        units already in the fight (`present`: committed ones, not a patrol standing nearby). 0 or
        less = surrounded: more would only pile on and block."""
        reach = self.reach_map(ai, units, enemies)
        probe = units[0]
        slots = 0
        for t in threats:
            capacity = attack_capacity(probe, t)
            if capacity is None:
                return len(units)  # not a melee problem (e.g. air): no space limit
            if reach is not None and not t.is_structure and not t.is_memory:
                capacity = min(capacity, reach.open_sides(t, probe.radius))
            slots += capacity
        mine = sum(1 for u in present if u.type_id in ARMY_TYPES)
        return int(slots * HELP_ROOM_FACTOR) - mine


def front_first(units: list[Unit], enemies: list[Unit]) -> list[Unit]:
    """Units closest to the enemy first, so they claim the target slots they're already at."""
    if not enemies:
        return units
    return sorted(units, key=lambda u: min(u.position.distance_to(e.position) for e in enemies))


def can_reach_soon(enemy: Unit, units: list[Unit], extra: float = 0.0) -> bool:
    """Within the enemy's weapon range plus a margin of one of these units: a margin for visible
    enemies (they can step in), a smaller one for remembered ones (last seen in the fog). Enemies
    farther away don't turn a squad back, so going *around* an army at a safe distance works."""
    margin = MEMORY_THREAT_MARGIN if enemy.is_memory else VISIBLE_THREAT_MARGIN
    reach = weapon_range(enemy) + enemy.radius + margin + extra
    return any(u.position.distance_to(enemy.position) - u.radius <= reach for u in units)


def enemies_near(ai: AresBot, units: list[Unit]) -> list[Unit]:
    """Enemy ground units/structures within ENGAGE_RANGE of any unit in the squad.

    Includes ares' memory of units last seen in the fog (e.g. a sieged tank revealed by firing).
    Memory units aren't in python-sc2's per-frame distance cache, so always measure distances
    to enemies with positions (`a.position.distance_to(b.position)`), never `unit.distance_to(unit)`.
    """
    found: dict[int, Unit] = {}
    for group in ai.mediator.get_units_in_range(
        start_points=[u.position for u in units], distances=ENGAGE_RANGE, query_tree=UnitTreeQueryType.EnemyGround
    ):
        for e in group:
            if e.type_id not in IGNORED_THREATS:
                found[e.tag] = e
    return list(found.values())


def fight_result(
    ai: AresBot, units: list[Unit], threats: list[Unit], engaged: set[int] | None = None
) -> tuple[EngagementResult, list[Unit]]:
    """The combat sim's verdict for a fight against `threats`, and the army units of ours it counted:
    every one near them (not just this squad: squads are small clusters, and judging each on its own against
    the whole enemy group made every one of them retreat from a fight the army as a whole wins)."""
    center = center_of(threats)
    own = [
        u
        for u in ai.mediator.get_units_in_range(
            start_points=[center], distances=ENGAGE_RANGE + 12, query_tree=UnitTreeQueryType.AllOwn
        )[0]
        if u.type_id == UnitTypeId.QUEEN or (u.type_id in ARMY_TYPES and (engaged is None or u.tag in engaged))
    ]
    mine = {u.tag for u in units}
    own = [u for u in own if u.tag not in mine] + list(units)
    threats = [t for t in threats if not tank_neutralized(t, own)]
    if not threats:
        return EngagementResult.VICTORY_EMPHATIC, own
    bunkers = [t for t in threats if t.type_id == UnitTypeId.BUNKER]
    melee = [u for u in own if u.type_id in ARMY_TYPES]
    if bunkers and melee:
        return through_bunkers(ai, own, melee, bunkers, [t for t in threats if t not in bunkers]), own
    return ai.mediator.can_win_fight(own_units=own, enemy_units=threats, workers_do_no_damage=True), own


def through_bunkers(
    ai: AresBot, own: list[Unit], melee: list[Unit], bunkers: list[Unit], others: list[Unit]
) -> EngagementResult:
    """The sim doesn't know a bunker shoots (its marines are inside), so judge it in two steps: the
    zerglings that fit around the bunkers kill them while the marines inside fire; then the
    survivors take on whatever else is there."""
    probe = melee[0]
    attackers = min(len(melee), sum(attack_capacity(probe, b) or len(melee) for b in bunkers))
    our_dps = attackers * max(probe.calculate_dps_vs_target(bunkers[0]), 0.1)
    seconds = sum(b.health + b.shield for b in bunkers) / our_dps
    lost = sum(dps_vs(b, probe) for b in bunkers) * seconds / max(probe.health_max, 1)
    COUNTS[f"bunker fight (lose ~{lost:.0f})"] += 1
    keep = max(0, len(melee) - int(lost + 0.5))
    survivors = sorted(melee, key=lambda u: u.health, reverse=True)[:keep]
    if not survivors:
        return EngagementResult.LOSS_OVERWHELMING
    if others:
        rest = [u for u in own if u.type_id not in ARMY_TYPES] + survivors
        return ai.mediator.can_win_fight(own_units=rest, enemy_units=others, workers_do_no_damage=True)
    share = keep / len(melee)  # what's left of us once the bunkers are dead
    if share >= 0.75:
        return EngagementResult.VICTORY_DECISIVE
    if share >= 0.5:
        return EngagementResult.VICTORY_CLOSE
    return EngagementResult.VICTORY_MARGINAL if share >= 0.25 else EngagementResult.LOSS_MARGINAL


def strike_result(ai: AresBot, own: list[Unit], group: list[Unit]) -> EngagementResult:
    """The sim's verdict for `own` against `group`, knowing that bunkers shoot."""
    if not own:
        return EngagementResult.LOSS_EMPHATIC
    bunkers = [e for e in group if e.type_id == UnitTypeId.BUNKER]
    if bunkers:
        return through_bunkers(ai, own, own, bunkers, [e for e in group if e not in bunkers])
    return ai.mediator.can_win_fight(own_units=own, enemy_units=group, workers_do_no_damage=True)


def tank_neutralized(enemy: Unit, own: list[Unit]) -> bool:
    """A sieged tank with several of our zerglings inside its minimum range.

    ares' combat sim ignores minimum range, so it rates such a tank at full damage even though it
    can't shoot those zerglings and will die before it can unsiege. We leave it out of the sim
    (it's still first in the dive priority, so it keeps getting attacked).

    NOTE: a judgement call that might need reverting if it causes big mistakes. The tank can
    still shoot zerglings that aren't on top of it, and leaving it out also drops its health
    from the sim, so predictions get a little optimistic. Requiring TANK_NEUTRALIZED_BY
    zerglings in its minimum range limits this to tanks we're clearly already on top of.
    """
    if enemy.type_id != UnitTypeId.SIEGETANKSIEGED:
        return False
    reach = SIEGE_MIN_RANGE + enemy.radius
    inside = sum(
        1 for u in own if u.type_id in ARMY_TYPES and u.position.distance_to(enemy.position) < reach + u.radius
    )
    return inside >= TANK_NEUTRALIZED_BY


def fight(
    ai: AresBot,
    unit: Unit,
    threats: list[Unit],
    enemies: list[Unit],
    slots: TargetSlots,
    squad_position: Point2,
    blocks: BlockTracker,
    detours: Detours,
) -> None:
    """Target choice for one zergling, in priority tiers; it takes the best-tier target that still
    has room (`TargetSlots`: melee attackers per target are capped by the space around it):

    1. siege tanks and other DIVE_FIRST units close by, then any other close enemy that fights back
    2. workers
    3. key structures (town halls, pylons, pools), then any structure, once no fighters are close
       (so zerglings don't chew on a barracks while marines and tanks shoot them). Exception: when
       every close fighter is full or blocked, the structure physically in the way (within
       BREAKTHROUGH_RANGE) is next, so zerglings break through instead of dying in a lane

    When a target is full the next zergling takes the next one (a full pylon doesn't hold up the
    gateway next to it). Targets fill from the back (TARGET_FILL "back"): zerglings are handled
    front-first and each takes the *deepest* target with room, so the first ones in run past the
    front line (and up ramps) and later ones fill in toward the front, instead of everyone stopping
    at the first enemy and blocking the ones behind. A zergling only stops to hit its own target.

    A zergling that stops getting closer to an enemy unit (`BlockTracker`) first takes a detour to
    it around our own crowd (`Detours`, e.g. around the outside of a block of gateways instead of
    queueing at a 1-tile gap). With no reasonable detour (or for a structure) it takes another
    target it can reach, breaking through the structure in its way if that's all there is; a
    zergling breaking through switches back to the units as soon as one is reachable.
    """
    position = unit.position
    fighters = [e for e in threats if e.type_id not in WORKER_TYPES]
    # Fighters we can walk to soon: a short walk (around enemy units) to a spot next to them, or
    # already touching. Walking distance, not straight-line: a stalker just behind a wall of
    # gateways is close but a long way round, and isn't worth streaming toward down the lanes
    # (keep clearing instead), while marines in the open a few tiles away are a short walk
    # (Measured to a structure's edge: a bunker's center is well behind the face we can hit)
    near_fighters = [
        e
        for e in fighters
        if e.tag in slots.strike
        or e.position.distance_to(position) - (e.radius if e.is_structure else 0) < FIGHTER_PRIORITY_RANGE
    ]
    if slots.reach is not None:
        near_fighters = [
            e
            for e in near_fighters
            if e.tag in slots.strike  # sent for it: however far (see `Army.update_strikes`)
            or e.position.distance_to(position) - e.radius - unit.radius < 0.75
            or slots.reach.walk_distance(e, position, FIGHTER_PRIORITY_RANGE) <= FIGHTER_PRIORITY_RANGE
        ]
    workers = [e for e in threats if e.type_id in WORKER_TYPES]
    structures = [e for e in enemies if e.is_structure]
    # The structure worth breaking to get at enemy fighters behind it (`Army.breach_caps`): before
    # any other structure, with only as many attackers as can hit it at once
    breach = [s for s in structures if s.tag in slots.caps]
    if slots.reach is not None:
        # Only structures we can actually stand next to (not ones on top of a cliff from here)
        structures = [s for s in structures if slots.reach.reachable(s)]

        # Only enemy units we can surround (enough open cells reachable around them); fighting one
        # through a 1-tile gap is a 1-on-1 we lose. Exception: a ranged one right next to us, hit it
        # back (stepping away doesn't get us out of its range). A melee one plugging a gap (a zealot
        # between two gateways): step out of its reach instead, it gets surrounded once the
        # buildings next to it fall
        def touching(e: Unit) -> bool:
            return e.position.distance_to(position) - e.radius - unit.radius < 0.75

        def surroundable(e: Unit) -> bool:
            return slots.reach.open_sides(e, unit.radius) >= MIN_OPEN_SIDES

        pinned = [
            e
            for e in near_fighters
            if touching(e) and not surroundable(e) and not e.is_structure and e.ground_range <= 1
        ]
        if pinned:
            nearest = min(pinned, key=lambda e: e.position.distance_to(position))
            COUNTS["step away (gap)"] += 1
            unit.move(nearest.position.towards(position, nearest.radius + unit.radius + 2.0))
            return
        near_fighters = [e for e in near_fighters if surroundable(e) or touching(e)]
        workers = [e for e in workers if slots.reach.reachable(e)]
    if near_fighters:
        dive = [e for e in near_fighters if e.type_id in DIVE_FIRST]
        # When every close fighter is full or blocked (e.g. zealots in 1-tile gaps between
        # gateways), break through the structure in the way rather than die in the lane
        in_the_way = by_safety(
            ai, [s for s in structures if s.position.distance_to(position) - s.radius < BREAKTHROUGH_RANGE], unit
        )
        tiers = [
            (dive, True),
            ([e for e in near_fighters if e not in dive], True),
            (workers, False),
            (breach, False),
            (in_the_way, False),
        ]
    elif any(e.type_id not in WORKER_TYPES for e in threats):
        # Fighters around but none we can reach: clear the way through the closest structures
        # (front first) instead of milling about in their fire
        clear = by_safety(
            ai, [s for s in structures if s.position.distance_to(position) - s.radius < CLEAR_THE_WAY_RANGE], unit
        )
        tiers = [(workers, False), (breach, False), (clear, False)]
    else:
        key = [s for s in structures if s.type_id in PRIORITY_STRUCTURES]
        tiers = [(fighters, True), (workers, True), (key, True), ([s for s in structures if s not in key], True)]
    tiers = [(t, back) for t, back in tiers if t]
    for rank, (tier, _) in enumerate(tiers, start=1):
        for e in tier:
            if e.tag not in TARGET_NOTES or TARGET_NOTES[e.tag][0] > rank:
                TARGET_NOTES[e.tag] = (rank, slots.capacity(unit, e))
    if not tiers:
        return  # nothing to go after (e.g. only far-off remembered units)

    now = ai.time
    maneuver = CombatManeuver()
    # On a detour around our own crowd: keep following it to that target
    if (route_target := detours.route_target(unit, now)) is not None:
        goal = next((e for t, _ in tiers for e in t if e.tag == route_target), None)
        waypoint = detours.next_waypoint(unit) if goal is not None else None
        if goal is not None and waypoint is not None and not unit.target_in_range(goal, 0.25):
            COUNTS["detour follow"] += 1
            maneuver.add(ShootTargetInRange(unit=unit, targets=[goal]))
            maneuver.add(
                PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=waypoint, sense_danger=False)
            )
            ai.register_behavior(maneuver)
            return
        detours.drop(unit)

    if TARGET_FILL == "off":
        goal = min((e for t, _ in tiers for e in t), key=lambda e: e.position.distance_to(position))
        flank = False
    else:
        depth_from = squad_position if TARGET_FILL == "back" else None
        goal, flank = pick_tiered(slots, blocks, unit, tiers, depth_from, now)
    if goal is None:
        return
    same_level = goal.is_flying or abs(ai.get_terrain_z_height(unit) - ai.get_terrain_z_height(goal)) < LEVEL_TOLERANCE
    if not flank and blocks.update(unit, goal, now, same_level):
        # Just got stuck on this one. An enemy unit: go around our own crowd to it if there's a
        # reasonable way; otherwise (or a structure) take another target we can reach
        crowd = [u for u in ai.units if u.tag != unit.tag and u.type_id in ARMY_TYPES and u.distance_to(unit) < 8]
        if not goal.is_structure and not goal.is_memory and detours.plan(ai, unit, goal, crowd, now):
            COUNTS["detour planned"] += 1
            fight(ai, unit, threats, enemies, slots, squad_position, blocks, detours)
            return
        COUNTS["blocked, repick"] += 1
        goal, flank = pick_tiered(slots, blocks, unit, tiers, None, now)
    if flank:
        category = "flank"
    elif goal.is_structure and near_fighters:
        category = "break through"
    elif goal.is_structure:
        category = "structure"
    else:
        category = "unit"
        if unit.target_in_range(goal, 0.25):
            COUNTS["unit in range"] += 1
    COUNTS[category] += 1
    if not ai.mediator.is_position_safe(grid=ai.mediator.get_ground_grid, position=position):
        COUNTS[f"UNSAFE {category}"] += 1

    if flank and not goal.is_memory:
        side = 1 if unit.tag % 2 else -1  # alternate sides so they spread around the jam
        away = goal.position.towards(unit.position, 1) - goal.position
        around = goal.position + Point2((-away.y, away.x)) * side * (goal.radius + FLANK_DISTANCE)
        maneuver.add(ShootTargetInRange(unit=unit, targets=[e for t, _ in tiers for e in t]))
        maneuver.add(PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=around, sense_danger=False))
        ai.register_behavior(maneuver)
        return
    # Only stop for *our* target: stopping at whatever we brush past blocks everyone behind
    maneuver.add(ShootTargetInRange(unit=unit, targets=[goal]))
    # An enemy unit: walk around the other enemy units to it (e.g. around the outside of a gateway
    # grid instead of down a lane a zealot plugs), then attack it
    if (
        not goal.is_structure
        and not goal.is_memory
        and slots.reach is not None
        and not unit.target_in_range(goal, 0.5)
        and (step := slots.reach.approach_step(goal, position)) is not None
    ):
        COUNTS["approach around"] += 1
        maneuver.add(MoveTo(unit=unit, target=step))
        ai.register_behavior(maneuver)
        return
    if chase := chase_behavior(unit, goal):
        maneuver.add(chase)
    ai.register_behavior(maneuver)


def by_safety(ai: AresBot, structures: list[Unit], unit: Unit) -> list[Unit]:
    """Structures we can hit from outside known enemy weapon range first, then the rest; closest
    first within each (front-row gateways come down without stalker fire)."""
    grid = ai.mediator.get_ground_grid

    def key(s: Unit) -> tuple[bool, float]:
        near_side = s.position.towards(unit.position, s.radius + unit.radius + 0.2)
        safe = ai.mediator.is_position_safe(grid=grid, position=near_side)
        return (not safe, s.position.distance_to(unit.position))

    return sorted(structures, key=key)


def chase_behavior(unit: Unit, goal: Unit) -> AttackTarget | AMove | None:
    """Go attack `goal`. Visible units get a direct attack order (only re-issued if the zergling
    isn't already on it). Remembered units in the fog can't be targeted directly, so attack-move
    to their last known spot; `success_at_distance=0` because AMove's default does nothing within
    7 of the target, which left zerglings stuck on their previous target."""
    if goal.is_memory:
        return AMove(unit=unit, target=goal.position, success_at_distance=0)
    if unit.order_target == goal.tag:
        return None
    return AttackTarget(unit=unit, target=goal)


def armed(enemy: Unit) -> bool:
    """Can it shoot ground units: anything with a ground weapon, and a bunker (no weapon of its own,
    but the marines inside shoot from it)."""
    return enemy.can_attack_ground or enemy.type_id == UnitTypeId.BUNKER


def weapon_range(enemy: Unit) -> float:
    """Ground weapon range; a bunker's is its marines' plus one."""
    if enemy.type_id == UnitTypeId.BUNKER:
        return MARINE_RANGE + 1
    return enemy.ground_range


def dps_vs(enemy: Unit, target: Unit) -> float:
    """Damage per second `enemy` deals to `target`. An enemy bunker's cargo isn't visible to us, so
    it's assumed full: BUNKER_MARINES marines."""
    if enemy.type_id == UnitTypeId.BUNKER:
        return BUNKER_MARINES * MARINE_DPS
    return enemy.calculate_dps_vs_target(target)


def reach_of(threat: Unit, unit: Unit) -> float:
    """How close `unit` can be to `threat` before it can be hit (edge to edge plus weapon range)."""
    return weapon_range(threat) + threat.radius + unit.radius


def back_off(
    ai: AresBot, units: list[Unit], threats: list[Unit], hold_at: Point2 | None, retreat: bool = False
) -> None:
    """Get out of the threats' actual weapon range (not ares' danger map, which pads every range by
    4 and sent zerglings all the way home), then stop: at `hold_at` if given (a rally), else right
    there. Units already out of reach wait."""
    shooters = [t for t in threats if armed(t)]
    center = center_of(threats)
    probe = units[0]
    if retreat:  # (overlay: only retreats draw the line, and only for enemies close to the units)
        BACK_OFF_CIRCLES.extend(
            (t.position, reach_of(t, probe) + BACK_OFF_MARGIN)
            for t in shooters
            if any(u.position.distance_to(t.position) < reach_of(t, probe) + BACK_OFF_MARGIN + 3 for u in units)
        )
    for unit in units:
        exposed = [t for t in shooters if unit.position.distance_to(t.position) < reach_of(t, unit) + BACK_OFF_MARGIN]
        if exposed:
            # Straight away from the closest one that can hit it, to just past its reach
            t = min(exposed, key=lambda e: unit.position.distance_to(e.position) - reach_of(e, unit))
            away = t.position.towards(unit.position, reach_of(t, unit) + BACK_OFF_MARGIN + 1)
            if unit.position.distance_to(center) < 0.5:
                away = center.towards(rally_point(ai), reach_of(t, unit) + BACK_OFF_MARGIN + 1)
            unit.move(away)
        elif hold_at is not None and unit.distance_to(hold_at) > 3:
            move(ai, unit, hold_at)
        elif unit.orders:
            unit.stop()


def spread_move(ai: AresBot, units: list[Unit], center: Point2, target: Point2) -> None:
    """Move toward `target` in an open formation: rows of up to SPREAD_ROW across the direction of
    travel, SPREAD_SPACING apart, anchored on the next point of the path (around known enemy fire),
    each unit taking the slot on its own side so they don't cross over."""
    anchor = ai.mediator.find_path_next_point(
        start=center, target=target, grid=ai.mediator.get_ground_grid, sensitivity=8, sense_danger=False
    )
    heading = anchor - center if anchor.distance_to(center) > 1 else target - center
    length = max((heading.x**2 + heading.y**2) ** 0.5, 0.01)
    forward = Point2((heading.x / length, heading.y / length))
    side = Point2((-forward.y, forward.x))
    slots = []
    for i in range(len(units)):
        row, col = divmod(i, SPREAD_ROW)
        in_row = min(SPREAD_ROW, len(units) - row * SPREAD_ROW)
        lateral = (col - (in_row - 1) / 2) * SPREAD_SPACING
        slots.append(anchor + side * lateral - forward * (row * SPREAD_SPACING))

    def across(p: Point2) -> float:
        return (p.x - center.x) * side.x + (p.y - center.y) * side.y

    for unit, slot in zip(sorted(units, key=lambda u: across(u.position)), sorted(slots, key=across), strict=True):
        unit.move(slot if ai.in_pathing_grid(slot) else anchor)


def move(ai: AresBot, unit: Unit, target: Point2) -> None:
    """Move along ares' path, avoiding known enemy fire."""
    ai.register_behavior(PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=target))


def rally_point(ai: AresBot) -> Point2:
    """In front of our base closest to the enemy."""
    base = ai.townhalls.closest_to(ai.enemy_start_locations[0])
    return base.position.towards(ai.game_info.map_center, 6)


def all_in(ai: AresBot) -> None:
    """No hatchery left: throw everything at the enemy."""
    for unit in ai.units.exclude_type({UnitTypeId.EGG, UnitTypeId.LARVA}):
        unit.attack(ai.enemy_start_locations[0])
