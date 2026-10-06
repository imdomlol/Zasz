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

import math
import os
import time
from collections import Counter
from dataclasses import dataclass

import numpy as np
from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, AttackTarget, PathUnitToTarget, ShootTargetInRange
from ares.behaviors.combat.individual.combat_individual_behavior import CombatIndividualBehavior
from ares.consts import IGNORE_IN_COST_DICT, TOWNHALL_TYPES, WORKER_TYPES, EngagementResult, UnitRole, UnitTreeQueryType
from loguru import logger
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit
from scipy.ndimage import label

from bot import overlay
from bot.settings import (
    ACTIVE_MOVE,
    ACTIVE_TURN,
    AVOID_MARGIN,
    AVOID_SECONDS,
    BACK_OFF_HOLD_SECONDS,
    BACK_OFF_MARGIN,
    BREACH_KEEP_SECONDS,
    BREACH_SWITCH_SAVING,
    BREACH_VIEW_CELL,
    BREACH_VIEW_RANGE,
    BREAKTHROUGH_RANGE,
    BUNKER_MARINES,
    CHOKE_SLOTS,
    CLEAR_THE_WAY_RANGE,
    DEFEND_RADIUS,
    DISENGAGE_AT,
    ENEMY_GROUP_RADIUS,
    ENEMY_IDLE_SECONDS,
    FIGHT_AT,
    FLANK_DISTANCE,
    GATHERED_FRACTION,
    HELP_RANGE,
    HELP_ROOM_FACTOR,
    HELP_SECONDS,
    HELP_WIN_AT,
    LEVEL_TOLERANCE,
    LING_SCOUT_MIN_SECONDS,
    MARINE_DPS,
    MARINE_HP,
    MARINE_RANGE,
    MAX_PATROLS_ATTACKING,
    MEMORY_THREAT_MARGIN,
    NOTICE_RANGE,
    PARTLY_SEEN_MARGIN,
    PARTLY_SEEN_RANGE,
    PARTLY_SEEN_SECONDS,
    PATROL_MIN_SQUADS,
    PLAN_SECONDS,
    PLUG_HARM_RANGE,
    RAID_MIN,
    RAID_TOGETHER,
    RALLY_SECONDS,
    REACH_KEEP_SECONDS,
    REACH_SECONDS,
    REACH_SHARE_RADIUS,
    REGIONS_SECONDS,
    SAFE_WORK_MARGIN,
    SCOUTED_SECONDS,
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
    THREAT_LOOKUP,
    UNDER_FIRE_SECONDS,
    UNSEEN_DAMAGE_SHARE,
    UNSEEN_EXPLAIN_SECONDS,
    UNSEEN_FIRE_STEP,
    UNSEEN_MIN_HITS,
    UNSEEN_RADIUS,
    UNSEEN_WINDOW,
    VISIBLE_THREAT_MARGIN,
    WIDEN_GIVE_UP_SECONDS,
    WIDEN_MAX_SECONDS,
    WIDEN_STILL_SECONDS,
    ZERGLING_DPS_VS_MARINE,
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
    fight_verdict,
    needed,
)
from bot.targeting import (
    Assignments,
    BlockTracker,
    Detours,
    ReachMap,
    RouteMap,
    TargetSlots,
    attack_capacity,
    pick_tiered,
    spot_key,
)

ARMY_TYPES = {UnitTypeId.ZERGLING}
# Debug overlay: this step's back-off lines (an enemy's position, how far from it we back off to),
# and each candidate target's priority (its targeting tier, 1 = first) and how many attackers it takes
BACK_OFF_CIRCLES: list[tuple[Point2, float]] = []
TARGET_NOTES: dict[int, tuple[int, int | None]] = {}  # enemy tag -> (priority, capacity)
CHOKE_NOTES: dict[int, tuple[float, float, bool]] = {}  # enemy tag -> (zerglings lost, worth, taken)
CHOKE_OPENERS: dict[int, str] = {}  # enemy tag -> the building to break first to open it up (overlay)
UNIT_NOTES: dict[int, str] = {}  # our unit tag -> its latest decision this step (overlay)


def note(units: list[Unit], text: str) -> None:
    """Record what these units were told to do, and why (the overlay shows it over each one)."""
    for u in units:
        UNIT_NOTES[u.tag] = text


def name_of(e: Unit) -> str:
    return f"{e.type_id.name.lower()}@{e.position.x:.0f},{e.position.y:.0f}"


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
        self.taken: dict[int, int] = {}  # target tag -> attackers this step, across all squads
        self.settled: dict[tuple[int, float], int | None] = {}  # (target, attacker radius) -> capacity (`TargetSlots`)
        self.cells_taken: set[tuple[float, float]] = set()  # attack spots claimed this step (`spot_key`)
        self.assignments = Assignments()  # each zergling's target, kept across steps
        # Map-wide routes around danger (`route_map`), per PLAN_SECONDS window: the enemy groups and avoid areas
        # as the window began; per squad (its units' tags), which of those groups it can't beat; and one
        # route map per set of groups to avoid (squads that lose to the same groups share it)
        self.route_groups: list[EnemyGroup] = []
        self.route_zones: list[tuple[Point2, float]] = []
        self.route_losing: dict[frozenset[int], frozenset[int]] = {}
        self.route_maps: dict[frozenset[int], RouteMap] = {}
        # Squads scouting while waiting on an overlord: squad id -> (spot, deadline, started, what it was
        # doing before: job, target, site, site tags, helping)
        self.squad_scouts: dict[int, tuple] = {}
        self.scouted_at: dict[Point2, float] = {}  # expansion spots our scouts have looked at -> when
        self.routes_at = -99.0
        self.enemy_state: dict[int, tuple[Point2, float, float]] = {}  # tag -> (where, facing, last active)
        self.enemy_moved: dict[int, float] = {}  # enemy unit tag -> when it last moved (or was first seen)
        # Widening plans: target tag -> (building to break, when the plan started); given up: tag -> until
        self.widening: dict[int, tuple[int, float]] = {}
        self.widen_given_up: dict[int, float] = {}
        self.rallies: dict[int, Rally] = {}  # losing squad id -> where it and its helpers gather
        self.breach_kept: dict[int, float] = {}  # the breach targets we're committed to -> last confirmed
        self.breach_evals: dict[tuple[int, int], tuple[float, dict[int, int]]] = {}  # fight area -> (when, caps)
        self.strikes: list[Strike] = []  # enemy groups shooting us, and the zerglings sent for them
        self.recent_shooters: dict[int, tuple[Point2, float, float]] = {}  # armed enemy -> (where, reach, seen)
        self.shooters_at = -1.0
        self.unseen_hits: list[tuple[Point2, float, float]] = []  # damage nobody seen could have dealt
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
        self.staging_since: dict[Point2, float] = {}  # defended site -> when its raiders started gathering
        self.raids_ok = True  # every raid staffed, gathered and not short of the bar (patrols allowed)
        self.blocks = BlockTracker()  # which zerglings are stuck behind others, per target
        self.detours = Detours()  # routes around our own crowd for blocked zerglings
        # (area, walled-off region) -> (built at, map, the enemy tags it covers)
        self.reach_cache: dict[tuple[int, int, int], tuple[float, ReachMap, frozenset[int]]] = {}
        # Debug: count what zerglings decide, logged every few seconds (scenarios switch this on)
        self.debug = False
        self.last_note: dict[int, tuple[float, str]] = {}  # our unit -> (when, its last decision), kept
        # Enemy building -> (most zerglings in range of it at once, the room it had then, its name) (`note_hitting`)
        self.most_hitting: dict[int, tuple[int, int | None, str]] = {}
        self.hitting_now: dict[int, int] = {}  # armed building -> zerglings in range of it last step
        self.step_ms: list[float] = []  # army time per step (ms), logged and reset with the debug counts
        self.route_ms: list[float] = []  # time spent on route checks (ms), same
        self.counts = COUNTS
        # A new game starts with no counts or reach dumps left from an earlier one in this process
        # (`benchmark.py --jobs 1` plays its games in one process)
        COUNTS.clear()
        REACH_DUMPED.clear()
        self.diagnostics = False  # debug or the overlay is on this step: build what only they read
        self.counts_logged_at = 0.0
        self.helps: list[tuple[float, int, int]] = []  # (time, helper squad, helped squad), for scenarios
        # Committed: attack and keep fighting whatever the combat sim says (no avoid areas, no
        # retreats). Off in games; scenarios that test execution against a fight the sim calls a
        # loss switch it on
        self.committed = False

    def on_damage(self, ai: AresBot, unit: Unit, amount: float = 0.0) -> None:
        self.last_damaged[unit.tag] = ai.time
        self.remember_shooters(ai)  # damage arrives before the step: a tank revealed by this very shot counts
        # Who could have done it: any enemy seen in the last UNSEEN_EXPLAIN_SECONDS with its weapon reach
        # covering the unit (no distance limit: a sieged tank hits from ~14.5). Nobody: unseen fire
        explained = any(
            ai.time - seen <= UNSEEN_EXPLAIN_SECONDS
            and unit.position.distance_to(position) <= reach + unit.radius + 1.0
            for position, reach, seen in self.recent_shooters.values()
        )
        # The visible enemies that could have dealt this hit (in reach, and the damage fits their
        # weapon) have done something: they count as active
        for e in ai.enemy_units:
            if (
                armed(e)
                and unit.position.distance_to(e.position) <= reach_of(e, unit) + 0.5
                and hit_fits(e, unit, amount)
            ):
                self.mark_active(e, ai.time)
        if not explained and unit.type_id in ARMY_TYPES:
            self.unseen_hits.append((unit.position, amount, ai.time))
            COUNTS["unseen hit"] += 1

    def on_death(self, ai: AresBot, tag: int) -> None:
        """Debug log: one of our army units died. Where, what it was last doing, and which enemies could
        hit that spot (with how far inside their reach it stood)."""
        if not self.debug:
            self.last_note.pop(tag, None)
            return
        unit = ai._units_previous_map.get(tag)
        if unit is None or unit.type_id not in ARMY_TYPES:
            return
        when, text = self.last_note.pop(tag, (ai.time, "no decision recorded"))
        spot = unit.position
        enemies = ai.mediator.get_units_in_range(
            start_points=[spot], distances=16, query_tree=UnitTreeQueryType.EnemyGround
        )[0]
        hitting = sorted(
            (
                (reach_of(e, unit) - spot.distance_to(e.position), e)
                for e in enemies
                if armed(e) and spot.distance_to(e.position) <= reach_of(e, unit)
            ),
            key=lambda x: -x[0],
        )
        inside = ", ".join(f"{name_of(e)} ({depth:.1f} inside)" for depth, e in hitting[:3]) or "nothing in reach"
        COUNTS["died"] += 1
        logger.info(
            f"{ai.time_formatted} DIED {unit.type_id.name.lower()} at {spot.x:.1f},{spot.y:.1f}:"
            f" last decision ({ai.time - when:.1f}s ago) '{text}'; in reach of {inside}"
        )

    def track_activity(self, ai: AresBot) -> None:
        """Note when each visible enemy unit last did something: moved, or turned (a unit on hold turns
        to shoot). See `active`."""
        for e in ai.enemy_units:
            prev = self.enemy_state.get(e.tag)
            if prev is None:
                self.enemy_state[e.tag] = (e.position, e.facing, -math.inf)  # idle until it does something
                self.enemy_moved[e.tag] = ai.time
                continue
            where, facing, active = prev
            turn = abs((e.facing - facing + math.pi) % (2 * math.pi) - math.pi)
            moved = e.position.distance_to(where) > ACTIVE_MOVE
            if moved:
                self.enemy_moved[e.tag] = ai.time
            if moved or turn > ACTIVE_TURN:
                self.enemy_state[e.tag] = (e.position, e.facing, ai.time)

    def mark_active(self, e: Unit, now: float) -> None:
        where, facing, _ = self.enemy_state.get(e.tag, (e.position, e.facing, 0.0))
        self.enemy_state[e.tag] = (where, facing, now)

    def active(self, ai: AresBot, e: Unit) -> bool:
        """Has this enemy unit done anything (moved, turned, hit us) in the last ENEMY_IDLE_SECONDS? An
        idle one (on hold and untouched, like a forgotten group) won't come to help its friends."""
        state = self.enemy_state.get(e.tag)
        return state is not None and ai.time - state[2] < ENEMY_IDLE_SECONDS

    def remember_shooters(self, ai: AresBot) -> None:
        """Every armed enemy we see this step: where it is, its weapon reach, when (to explain damage)."""
        if self.shooters_at == ai.time:
            return
        self.shooters_at = ai.time
        for e in [*ai.enemy_units, *ai.enemy_structures]:
            if not e.is_memory and armed(e):
                self.recent_shooters[e.tag] = (e.position, weapon_range(e) + e.radius, ai.time)
        self.recent_shooters = {
            t: v for t, v in self.recent_shooters.items() if ai.time - v[2] <= UNSEEN_EXPLAIN_SECONDS
        }
        self.unseen_hits = [h for h in self.unseen_hits if ai.time - h[2] <= UNSEEN_WINDOW]

    def unseen_circles(self) -> list[tuple[Point2, float, int]]:
        """Recent unseen hits grouped by place: (center, damage, hits)."""
        circles: list[list[tuple[Point2, float, float]]] = []
        for hit in self.unseen_hits:
            for c in circles:
                if any(hit[0].distance_to(h[0]) < UNSEEN_RADIUS for h in c):
                    c.append(hit)
                    break
            else:
                circles.append([hit])
        return [(center_of_points([h[0] for h in c]), sum(h[1] for h in c), len(c)) for c in circles]

    def unseen_fire_on(self, ai: AresBot, units: list[Unit]) -> Point2 | None:
        """Where unseen fire is hurting this force enough to leave: at least UNSEEN_MIN_HITS unseen hits
        near its units, adding up to UNSEEN_DAMAGE_SHARE of the health of our zerglings around them."""
        near = [h for h in self.unseen_hits if any(h[0].distance_to(u.position) < UNSEEN_RADIUS for u in units)]
        if len(near) < UNSEEN_MIN_HITS:
            return None
        center = center_of_points([h[0] for h in near])
        ours = [u for u in ai.units if u.type_id in ARMY_TYPES and u.position.distance_to(center) < UNSEEN_RADIUS * 2]
        health = sum(u.health_max for u in ours) or 1
        if sum(h[1] for h in near) < UNSEEN_DAMAGE_SHARE * health:
            return None
        return center

    def control(self, ai: AresBot, attacking: bool) -> None:
        started = time.perf_counter()
        try:
            self._control(ai, attacking)
        finally:
            if self.debug:
                self.step_ms.append((time.perf_counter() - started) * 1000)
            else:
                # Nothing reads the counts without debug (`log_debug`): don't let them pile up all game
                self.counts.clear()

    def _control(self, ai: AresBot, attacking: bool) -> None:
        """Every squad does its job (`bot.squads`): defend if enemies are at our bases; otherwise
        raid enemy sites while attacking, help squads in fights they'd lose, and patrol (scout)
        the rest of the time."""
        alive = {u.tag for u in ai.units}
        self.diagnostics = self.debug or overlay.enabled()
        self.clear_empty_avoid_areas(ai)
        self.breach_kept = {t: when for t, when in self.breach_kept.items() if ai.time - when < BREACH_KEEP_SECONDS}
        self.remember_shooters(ai)
        self.track_activity(ai)
        self.partly_seen = self.find_partly_seen(ai)
        BACK_OFF_CIRCLES.clear()
        TARGET_NOTES.clear()
        CHOKE_NOTES.clear()
        CHOKE_OPENERS.clear()
        UNIT_NOTES.clear()
        self.blocks.forget_dead(alive)
        self.detours.forget_dead(alive)
        if not self.patrol.spots:
            self.patrol.setup(ai)
        self.patrol.update(ai)
        attacking = attacking or self.committed
        lings = [u for u in ai.mediator.get_units_from_role(role=UnitRole.ATTACKING) if u.type_id in ARMY_TYPES]
        busy = set(self.battles) | {s.id for s in self.book.squads.values() if s.job == "help"}
        squads = self.book.update(lings, busy)
        # Scouting plans of squads that died or were merged away: their spot is free to scout again
        self.squad_scouts = {i: plan for i, plan in self.squad_scouts.items() if i in self.book.squads}
        # Area maps not used for a while: rebuilt on next use anyway (`reach_map`), so don't keep them
        self.reach_cache = {k: v for k, v in self.reach_cache.items() if ai.time - v[0] < REACH_KEEP_SECONDS}
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
        self.taken.clear()  # target slots, shared by every squad this step
        self.settled.clear()
        self.cells_taken.clear()
        if self.assignments.taken_over:
            COUNTS["slot taken over (closer zergling)"] += self.assignments.taken_over
            self.assignments.taken_over = 0
        self.assignments.start(ai.time, {u.tag for u in ai.units if u.type_id in ARMY_TYPES})
        self.update_strikes(ai, lings)
        striking = {t for s in self.strikes for t in s.lings}
        battles: dict[int, Battle] = {}
        for squad in squads:
            # Zerglings on a strike are the strike's this step; the squad runs the rest
            squad.units = [u for u in squad.units if u.tag not in striking]
            if not squad.units:
                continue
            target, advancing = self.squad_goal(ai, squad, squads)
            if battle := self.control_squad(ai, squad, target, advancing, FIGHT_AT):
                battles[squad.id] = battle
        self.battles = battles
        self.execute_strikes(ai)
        if self.debug:
            self.note_hitting(ai, lings)
        for tag, text in UNIT_NOTES.items():
            self.last_note[tag] = (ai.time, text)
        if overlay.enabled():
            overlay.draw(ai, self)

    def note_hitting(self, ai: AresBot, lings: list[Unit]) -> None:
        """Debug: the most zerglings in range of each enemy building at once, with the room the slots gave
        it then (`TargetSlots.capacity`), to check that estimate against the game."""
        for s in ai.enemy_structures:
            if s.is_flying or s.is_memory:
                continue
            n = sum(1 for u in lings if u.target_in_range(s))
            if armed(s):
                self.note_refill(ai, lings, s, n)
            if n and n >= self.most_hitting.get(s.tag, (0, None, ""))[0]:
                room = next((v for (tag, _), v in self.settled.items() if tag == s.tag), None)
                self.most_hitting[s.tag] = (n, room, name_of(s))

    def note_refill(self, ai: AresBot, lings: list[Unit], target: Unit, n: int) -> None:
        """Debug: every change in how many zerglings are in range of an armed building, with who holds its
        slots without hitting it (assigned, not in range: how far) and what the zerglings within 3 of it
        are doing instead."""
        before = self.hitting_now.get(target.tag, 0)
        self.hitting_now[target.tag] = n
        if n == before:
            return
        holders = [
            f"{u.distance_to(target) - u.radius - target.radius:.1f}"
            for u in lings
            if self.assignments.target.get(u.tag) == target.tag and not u.target_in_range(target)
        ]
        near = Counter(
            UNIT_NOTES.get(u.tag, "?").split(" [")[0].split(": ")[0]
            for u in lings
            if self.assignments.target.get(u.tag) != target.tag and u.distance_to(target) - target.radius < 3
        )
        logger.info(
            f"{ai.time_formatted} HITTING {name_of(target)}: {before} -> {n}, room"
            f" {next((v for (t, _), v in self.settled.items() if t == target.tag), '-')};"
            f" assigned not in range {len(holders)} (gap {', '.join(holders) or '-'}); within 3: {dict(near)}"
        )

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
        steps = self.step_ms or [0.0]
        timing = f"army {sum(steps) / len(steps):.1f} ms/step avg, {max(steps):.1f} max"
        if self.route_ms:
            timing += f"; routes {len(self.route_ms)} checks, {sum(self.route_ms) / len(self.route_ms):.1f} ms avg"
            timing += f", {max(self.route_ms):.1f} max"
        hitting = sorted(self.most_hitting.values(), key=lambda x: -x[0])[:4]
        most = ", ".join(f"{name} {n} (room {'-' if room is None else room})" for n, room, name in hitting)
        logger.info(
            f"{ai.time_formatted} army debug: {len(squads)} squads ({mode}), {lings} zerglings vs {enemy}"
            f" | raids: {raids or 'none'} | most hitting at once: {most or 'none'} | {timing} | {summary}"
        )
        self.counts.clear()
        self.step_ms.clear()
        self.route_ms.clear()

    def squad_goal(self, ai: AresBot, squad: Squad, squads: list[Squad]) -> tuple[Point2, bool]:
        """Where this squad is headed, and whether it's advancing (attacking its way there) or just
        moving (patrol: around known enemy fire, backing off when shot)."""
        if squad.job == "defend":
            return squad.target, True
        if squad.job == "gather":
            return squad.target, False
        if squad.job == "engage":
            return squad.target, True
        if squad.job == "scout":
            return self.scout_goal(ai, squad)
        if squad.job == "push":
            # Seen it, or it turned dangerous (an avoid area, or in reach of a group we can't beat: a push
            # only travels, so it doesn't walk back into a known position): on to the next, right away
            units = squad.units
            if (
                squad.squad_position.distance_to(squad.target) < 6
                or self.patrol.fresh(squad.target, ai.time)
                or self.dangerous_for(ai, units, squad.target)
                or not self.route_ok(ai, units, squad.target)
            ):
                squad.target = self.push_target(ai, units)
            if squad.target is not None:
                return squad.target, True
            # Nowhere left to push safely: patrol (below) until the next plan finds somewhere
            COUNTS["push: nowhere safe"] += len(units)
            squad.free()
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
            # Stay out of reach of enemy groups this squad can't beat (ones it beats are fair game), and
            # don't try for spots we can't walk to (e.g. behind a mineral wall)
            groups = [g for g in enemy_groups(ai) if not wins(ai, squad.units, g.units)]
            # ... and the way there doesn't cross their reach either (one field from here covers every spot)
            routes = self.route_map(ai, squad.units) if squad.units else None
            here = squad.squad_position if squad.units else None

            def dangerous(p: Point2) -> bool:
                return (
                    self.blocked(ai, p)
                    or not self.can_walk_to(ai, p)
                    or any(p.distance_to(g.center) < g.spread + g.reach + STAGING_MARGIN for g in groups)
                    or (routes is not None and not routes.reaches_from(here, p))
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
        # A raid's gather clock follows its site too: an army in the field is a site whose center moves every
        # time it shifts, and a clock keyed by the old center restarted, so the raid gathered forever
        staging: dict[Point2, float] = {}
        for c, since in self.staging_since.items():
            if (site := same_site(c)) is not None:
                staging[site.center] = min(since, staging.get(site.center, since))
        self.staging_since = staging
        for s in squads:
            if s.job == "push":
                s.free()  # re-planned below: joins a raid if there's one now, else pushes on
        free = [s for s in squads if s.job in ("patrol", "raid", "scout") and s.id not in self.battles]
        # Spare capacity only exists if every raid is going well (staffed, gathered, its fights at the
        # engage bar or better); otherwise spare squads join a raid instead of patrolling
        self.raids_ok = all(
            b.result >= FIGHT_AT
            for sid, b in self.battles.items()
            if (sq := self.book.squads.get(sid)) is not None and sq.job in ("raid", "help", "engage", "gather")
        )
        for site in sites:
            on_it = [s for s in free if s.job == "raid" and s.site == site.center]
            if any(b.center.distance_to(site.center) < SITE_DEFENSE_RADIUS for b in self.battles.values()):
                continue  # a fight is on there: reinforcing it is for the help logic, not one squad at a time
            others = sorted(
                (s for s in free if s.job in ("patrol", "scout")),
                key=lambda s: s.squad_position.distance_to(site.center),
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
                demand = needed(ai, pool, site.defenders, site.center, HELP_WIN_AT, wins) or needed(
                    ai, pool, site.defenders, site.center, FIGHT_AT, wins
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
                self.squad_scouts.pop(s.id, None)
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
                since = self.staging_since.setdefault(site.center, ai.time)
                if there >= GATHERED_FRACTION * staffed or ai.time - since > RALLY_SECONDS:
                    # Gathered, or waited RALLY_SECONDS (some can't get there): go in with what's there
                    if there < GATHERED_FRACTION * staffed:
                        COUNTS["raid staging timed out"] += 1
                    self.gathered.add(site.center)
                    self.staging_since.pop(site.center, None)
                else:
                    target = staging
            for s in on_it:
                s.target = target
            free = [s for s in free if s not in on_it]
            state = "gathering" if target != site.center else "in"
            if staffed < demand or state == "gathering":
                self.raids_ok = False
            self.site_notes.append((site.center, f"raid {self.site_letters[site.center]}: {staffed}/{demand} {state}"))
        self.staging_since = {c: t for c, t in self.staging_since.items() if c in raiding}
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
        keep = MAX_PATROLS_ATTACKING if len(squads) >= PATROL_MIN_SQUADS and self.raids_ok else 0
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
        target = self.push_target(ai, [u for s in spare for u in s.units])
        if target is None:
            COUNTS["push: nowhere safe"] += len(spare)
            return  # they keep patrolling
        for s in spare:
            s.job, s.site, s.helping, s.target = "push", None, None, target
        COUNTS["pushing"] += len(spare)

    def push_target(self, ai: AresBot, units: list[Unit] | None = None) -> Point2 | None:
        """Where spare squads push when nothing is known to raid: their natural, then their main, then
        the other bases, skipping places we've seen lately, avoid areas, places in reach of a group
        `units` can't beat, and places we can't walk to (or not without crossing such reach). None if
        every one is skipped: walking into their main anyway is what the checks are there to stop."""
        spots = [ai.mediator.get_enemy_nat, ai.enemy_start_locations[0]]
        spots += [spot for spot, _ in ai.mediator.get_enemy_expansions]
        for spot in spots:
            if (
                self.patrol.fresh(spot, ai.time)
                or self.dangerous_for(ai, units or [], spot)
                or not self.can_walk_to(ai, spot)
                or not self.route_ok(ai, units or [], spot)
            ):
                continue
            return spot
        return None

    def send_squad_scout(self, ai: AresBot, squad: Squad) -> None:
        """A squad held back waiting for an overlord to look at a partly seen position doesn't just wait:
        it becomes a scout for an enemy expansion we haven't looked at lately (the closest one it can get
        to and back from, along the route around danger, before the overlord's vision arrives; one spot
        per squad). See `scout_goal` for coming back."""
        if squad.id in self.squad_scouts or not squad.units:
            return
        areas = [(c, r, u) for c, r, u in self.turned_back_at if self.avoid_reasons.get(c) == "scout needed"]
        if not areas:
            return
        center, _, until = min(areas, key=lambda x: x[0].distance_to(squad.squad_position))
        scouting = getattr(ai, "scouting", None)
        eta = scouting.eta(ai, center) if scouting is not None else None
        # No overlord on its way: our squads are the only scouts, so they can go farther (a round trip up
        # to SCOUTED_SECONDS)
        budget = eta if eta is not None else SCOUTED_SECONDS
        if budget < LING_SCOUT_MIN_SECONDS:
            COUNTS[f"squad scout: no time ({budget:.0f}s)"] += 1
            return
        units = squad.units
        routes = self.route_map(ai, units)
        busy = [plan[0] for i, plan in self.squad_scouts.items() if i in self.book.squads]
        speed = max(min(u.real_speed for u in units), 0.1)
        sight = min(u.sight_range for u in units)
        position = squad.squad_position
        for spot, _ in sorted(ai.mediator.get_enemy_expansions, key=lambda e: e[0].distance_to(position)):
            if ai.is_visible(spot) or ai.time - self.scouted_at.get(spot, -999) < SCOUTED_SECONDS:
                COUNTS["squad scout: skip seen"] += 1
                continue
            if any(spot.distance_to(b) < 10 for b in busy):
                COUNTS["squad scout: skip taken"] += 1
                continue
            if self.dangerous_for(ai, units, spot):
                COUNTS["squad scout: skip dangerous"] += 1
                continue
            # There and back (seeing it from sight range) before the overlord gets there
            walk = max(0.0, routes.distance(position, spot) - sight)
            if 2 * walk / speed > budget:
                COUNTS[f"squad scout: too far ({2 * walk / speed:.0f}s > {budget:.0f}s)"] += 1
                continue
            before = (squad.job, squad.target, squad.site, set(squad.site_tags), squad.helping)
            self.squad_scouts[squad.id] = (spot, ai.time + budget, ai.time, before)
            squad.job, squad.target = "scout", spot
            COUNTS[f"squad scout sent ({2 * walk / speed:.0f}s of {budget:.0f}s)"] += 1
            return

    def scout_goal(self, ai: AresBot, squad: Squad) -> tuple[Point2, bool]:
        """A scouting squad heads for its spot; once it has seen it, half its time is gone, or it's being
        shot, it's done: the spot counts as looked at and the squad goes back to what it was doing before
        (its job, target, site; a push whose target turned dangerous re-plans itself)."""
        plan = self.squad_scouts.get(squad.id)
        if plan is None:
            squad.free()
            return self.squad_goal(ai, squad, list(self.book.squads.values()))
        spot, deadline, started, before = plan
        seen = ai.is_visible(spot) and any(u.distance_to(spot) <= u.sight_range + 1 for u in squad.units)
        # Hit since it set out (not the fire it was backing away from when it was sent)
        hit = any(
            self.last_damaged.get(u.tag, -99) > started + UNDER_FIRE_SECONDS
            and ai.time - self.last_damaged[u.tag] < UNDER_FIRE_SECONDS
            for u in squad.units
        )
        if seen or hit or ai.time >= started + (deadline - started) / 2:
            if seen:
                self.scouted_at[spot] = ai.time
            del self.squad_scouts[squad.id]
            COUNTS["squad scout done (seen)" if seen else "squad scout done (time or hit)"] += 1
            squad.job, squad.target, squad.site, squad.site_tags, squad.helping = before
            return self.squad_goal(ai, squad, list(self.book.squads.values()))
        note(squad.units, f"scout: {spot.x:.0f},{spot.y:.0f} (back by {deadline - ai.time:.0f}s)")
        return spot, True

    def route_map(self, ai: AresBot, units: list[Unit]) -> RouteMap:
        """Routes around danger for these units, rebuilt every PLAN_SECONDS: avoid areas, and the reach
        (+ BACK_OFF_MARGIN) of every unit in an enemy group *these* units can't beat. (One map for every
        squad sent a weak patrol through a group only a big raid beats, or kept the raid out.)"""
        if ai.time - self.routes_at >= PLAN_SECONDS:
            self.routes_at = ai.time
            self.route_groups = enemy_groups(ai)
            self.route_zones = list(self.avoided(ai))
            self.route_losing, self.route_maps = {}, {}
        own = frozenset(u.tag for u in units)
        losing = self.route_losing.get(own)
        if losing is None:
            losing = frozenset(i for i, g in enumerate(self.route_groups) if not (units and wins(ai, units, g.units)))
            self.route_losing[own] = losing
        routes = self.route_maps.get(losing)
        if routes is None:
            started = time.perf_counter()
            zones = self.route_zones + [
                (u.position, weapon_range(u) + u.radius + 0.375 + BACK_OFF_MARGIN)
                for i in sorted(losing)
                for u in self.route_groups[i].units
                if armed(u)
            ]
            routes = self.route_maps[losing] = RouteMap(ai.mediator.get_ground_grid, zones)
            if self.debug:
                self.route_ms.append((time.perf_counter() - started) * 1000)
        return routes

    def route_ok(self, ai: AresBot, units: list[Unit], spot: Point2) -> bool:
        """Can these units travel to `spot` without crossing danger (`route_map`)?"""
        if not units:
            return True
        routes = self.route_map(ai, units)
        started = time.perf_counter()
        ok = routes.reaches(center_of(units), spot)
        if self.debug:
            self.route_ms.append((time.perf_counter() - started) * 1000)
        return ok

    def dangerous_for(self, ai: AresBot, units: list[Unit], spot: Point2) -> bool:
        """Is `spot` somewhere a travelling squad shouldn't go: an avoid area, or within the reach of an
        enemy group these units can't beat (the same test patrols use)."""
        if self.blocked(ai, spot):
            return True
        if not units:
            return False
        return any(
            spot.distance_to(g.center) < g.spread + g.reach + STAGING_MARGIN
            for g in enemy_groups(ai)
            if not wins(ai, units, g.units)
        )

    def site_reachable(self, ai: AresBot, site: Site) -> bool:
        """Can we get next to it: ground beside any of its structures (a site's center can be inside a
        walled pocket), or for an army in the field, near its center."""
        if site.structures:
            return any(self.can_walk_to(ai, s.position, int(s.radius) + 2) for s in site.structures)
        return self.can_walk_to(ai, site.center)

    def walk_regions(self, ai: AresBot) -> tuple[np.ndarray, set[int]]:
        """The ground grid's walkable cells labelled by connected region (minerals, rocks and buildings are
        walls), and the regions our ground units are in; refreshed every REGIONS_SECONDS."""
        if ai.time - self.regions_at >= REGIONS_SECONDS or self.regions is None:
            self.regions_at = ai.time
            walkable = np.isfinite(ai.mediator.get_ground_grid)
            labels, _ = label(walkable, structure=np.ones((3, 3), dtype=bool))
            ours = {int(labels[int(u.position.x), int(u.position.y)]) for u in ai.units if not u.is_flying} - {0}
            self.regions = (labels, ours)
        return self.regions

    def region_at(self, ai: AresBot, p: Point2) -> int:
        """`walk_regions` label at `p` (0: not walkable or off the map)."""
        labels, _ = self.walk_regions(ai)
        x, y = int(p.x), int(p.y)
        return int(labels[x, y]) if 0 <= x < labels.shape[0] and 0 <= y < labels.shape[1] else 0

    def can_walk_to(self, ai: AresBot, spot: Point2, near: int = 3) -> bool:
        """Is `spot` (or ground within `near` of it) in a walkable region our units are in? Minerals,
        rocks and buildings are walls: a base behind a mineral wall isn't, until it's mined out."""
        labels, ours = self.walk_regions(ai)
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
            spot = self.outside_reach(
                ai, battle.center.towards(fighting.squad_position, reach + STAGING_MARGIN), battle.center
            )
            members = [fighting, *chosen, *en_route]
            self.rallies[fighting_id] = Rally(spot, battle.center, [s.id for s in members], ai.time)
            for s in members:
                s.job, s.helping, s.site, s.target = "gather", None, None, spot
                taken.add(s.id)
            for s in chosen:
                self.helps.append((ai.time, s.id, fighting_id))
            COUNTS[f"rally ({len(chosen)} squads coming)"] += 1

    def outside_reach(self, ai: AresBot, spot: Point2, away_from: Point2) -> Point2:
        """`spot`, moved further from `away_from` until no armed enemy around can hit it (reach plus
        BACK_OFF_MARGIN; every enemy, not only the ones in that fight): a gather spot inside a bunker's range
        lost zerglings while they waited."""
        around = [
            e
            for e in ai.mediator.get_units_in_range(
                start_points=[spot], distances=20, query_tree=UnitTreeQueryType.EnemyGround
            )[0]
            if armed(e) and e.type_id not in WORKER_TYPES
        ]
        for _ in range(20):
            if not any(
                spot.distance_to(e.position) < weapon_range(e) + e.radius + 0.375 + BACK_OFF_MARGIN for e in around
            ):
                return spot
            spot = spot.towards(away_from, -1.0)
        return spot

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
                    sum(
                        reach.attack_slots(
                            t, probe.radius, attack_capacity(probe, t) or len(candidates), probe.ground_range
                        )
                        for t in targets
                    )
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
            n = self.strike_size(ai, pool, group + support, reach)
            if self.debug:
                seen = Counter(e.type_id.name.lower() + ("?" if e.is_memory else "") for e in group + support)
                verdict = strike_result(ai, pool[:n] if n else pool, group + support, reach).name
                logger.info(
                    f"{ai.time_formatted} strike sizing: vs {dict(seen)} (support {len(support)}), "
                    f"{len(pool)} zerglings in reach, chose {n}, sim {verdict}"
                )
            if n is None:
                COUNTS["strike: not winnable"] += 1
                continue
            chosen = pool[:n]
            # Every armed enemy around, not just the group (a bunker next to it shoots the gather spot too)
            around = ai.mediator.get_units_in_range(
                start_points=[center_of(group)], distances=25, query_tree=UnitTreeQueryType.EnemyGround
            )[0]
            shooters = [
                (e.position, weapon_range(e) + e.radius + 0.375 + BACK_OFF_MARGIN)
                for e in {*group, *around}
                if armed(e) and e.type_id not in WORKER_TYPES
            ]
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
                # Already together (one clump, not spread over the map): nothing to wait for
                middle = center_of(chosen)
                together = sum(1 for u in chosen if u.position.distance_to(middle) < RAID_TOGETHER)
                if (
                    max(there, together) >= GATHERED_FRACTION * len(chosen)
                    or ai.time - strike.since > STRIKE_GATHER_SECONDS
                ):
                    strike.go = True
                    COUNTS[f"strike: go ({len(chosen)})"] += 1
            strikes.append(strike)
            taken |= strike.lings
        if self.debug:
            # Strike start / end events (to see whether a strike survives its first attackers dying)
            for old in self.strikes:
                if not any(new.targets & old.targets for new in strikes):
                    logger.info(f"{ai.time_formatted} STRIKE ENDED: {len(old.lings)} zerglings left, go={old.go}")
            for new in strikes:
                if not any(new.targets & old.targets for old in self.strikes):
                    logger.info(f"{ai.time_formatted} STRIKE STARTED: {len(new.lings)} zerglings, go={new.go}")
        self.strikes = strikes

    def strike_size(
        self, ai: AresBot, pool: list[Unit], group: list[Unit], reach: ReachMap | None = None
    ) -> int | None:
        """Fewest of `pool` (in order) that beat `group`: comfortably if possible, else at all."""
        # Each size is judged once, for both bars (`through_bunkers` is more than one sim)
        sizes = [*range(min(2, len(pool)), len(pool) + 1, 2), *([len(pool)] if pool else [])]
        verdicts: dict[int, EngagementResult] = {}
        for at in (HELP_WIN_AT, FIGHT_AT):
            for n in sizes:
                if n not in verdicts:
                    verdicts[n] = strike_result(ai, pool[:n], group, reach)
                if verdicts[n] >= at:
                    return n
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
                note(units, "strike: gathering")
                back_off(ai, units, group, strike.spot)
                continue
            COUNTS["strike: in"] += len(units)
            center = center_of(group)
            # Structures to break or work safely while there: any in the strike's map (safe work decides
            # which are worth it), not just those within 12 of the enemies
            if strike.reach is not None:
                nearby = [e for e in enemies.values() if e.is_structure and strike.reach.cell(e.position) is not None]
            else:
                nearby = [e for e in enemies.values() if e.is_structure and e.position.distance_to(center) < 12]
            slots = TargetSlots(
                strike.reach,
                taken=self.taken,
                settled=self.settled,
                cells=self.cells_taken,
                assignments=self.assignments,
            )
            slots.strike = strike.targets
            for unit in front_first(units, group):
                fight(ai, unit, group, group + nearby, slots, center, self.blocks, self.detours, self.diagnostics)
                note([unit], f"strike | {UNIT_NOTES.get(unit.tag, '')}")

    def avoid_armies_we_cannot_beat(self, ai: AresBot, lings: list[Unit]) -> None:
        """Mark every known enemy group (seen now or remembered) the whole army can't beat as an
        area to avoid, before walking into it rather than after losing half the zerglings."""
        for group in enemy_groups(ai):
            if not wins(ai, lings, group.units):
                COUNTS[f"avoid group of {len(group.units)}"] += 1
                self.turned_back(ai, group.units, "army can't beat")

    def reach_map(self, ai: AresBot, units: list[Unit], enemies: list[Unit]) -> ReachMap | None:
        """Which enemy units our zerglings here can actually get to; one map per area and walled-off region
        (`walk_regions`: squads on either side of a wall get their own, each reached from its own side),
        rebuilt every REACH_SECONDS, or sooner if this squad's enemies aren't all in it. It's reached from
        every zergling of ours in that region nearby and covers the enemies near all of them, so the squads
        there share it (one map per squad doubled the builds)."""
        ground = [e for e in enemies if not e.is_flying]
        if not ground:
            return None
        center = center_of(units)
        region = self.region_at(ai, center)
        key = (round(center.x / 10), round(center.y / 10), region)
        tags = frozenset(e.tag for e in ground)
        cached = self.reach_cache.get(key)
        if cached is None or ai.time - cached[0] >= REACH_SECONDS or not tags <= cached[2]:
            mine = {u.tag for u in units}
            own = list(units) + [
                u
                for u in ai.units
                if u.type_id in ARMY_TYPES
                and u.tag not in mine
                and u.position.distance_to(center) < REACH_SHARE_RADIUS
                and self.region_at(ai, u.position) == region
            ]
            near = {e.tag: e for e in enemies_near(ai, own) if not e.is_flying}
            near.update({e.tag: e for e in ground})
            built = ReachMap(ai.mediator.get_ground_grid, own, list(near.values()), ai.get_terrain_z_height)
            cached = (ai.time, built, frozenset(near))
            self.reach_cache[key] = cached
        return cached[1]

    def turned_back(self, ai: AresBot, threats: list[Unit], reason: str = "") -> None:
        """Remember where a squad had to back off: the area those enemies cover (their spread plus
        weapon range), replacing an older entry for the same spot."""
        center = center_of(threats)
        spread = max(t.position.distance_to(center) for t in threats)
        radius = spread + max(weapon_range(t) + t.radius for t in threats) + AVOID_MARGIN
        self.mark_avoid(ai, center, radius, reason)

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
        # Until a "scout needed" area has really been looked at, everything armed in it stays partly seen,
        # remembered units included: once a tank's reveal from firing wears off it's only remembered, and
        # was then treated as a plain target (the sim saw 2 tanks, not what stands around them)
        areas = [(c, r) for c, r, _ in self.turned_back_at if self.avoid_reasons.get(c) == "scout needed"]
        if areas:
            for group in ai.mediator.get_units_in_range(
                start_points=[c for c, _ in areas],
                distances=[r for _, r in areas],
                query_tree=UnitTreeQueryType.EnemyGround,
            ):
                for e in group:
                    if armed(e) and e.type_id not in WORKER_TYPES:
                        found.add(e.tag)
        return found

    @staticmethod
    def scouted(ai: AresBot, center: Point2, radius: float) -> bool:
        """Have we actually looked at this area: its center and points around it (8 directions, 60% of
        the way out) all in vision now. Seeing just the unit at the center doesn't count."""
        if not ai.is_visible(center):
            return False
        for i in range(8):
            angle = i * math.pi / 4
            p = Point2((center.x + math.cos(angle) * radius * 0.6, center.y + math.sin(angle) * radius * 0.6))
            if (
                0 <= p.x < ai.game_info.map_size.width
                and 0 <= p.y < ai.game_info.map_size.height
                and not ai.is_visible(p)
            ):
                return False
        return True

    def mark_avoid(
        self, ai: AresBot, center: Point2, radius: float, reason: str, seconds: float = AVOID_SECONDS
    ) -> None:
        """An avoid area of a given size (`turned_back` works it out from the enemies seen)."""
        COUNTS[f"avoid area ({reason})"] += 1
        self.set_avoid_areas([(p, r, t) for p, r, t in self.turned_back_at if p.distance_to(center) > radius / 2])
        self.turned_back_at.append((center, radius, ai.time + seconds))
        self.avoid_reasons[center] = reason

    def set_avoid_areas(self, areas: list[tuple[Point2, float, float]]) -> None:
        """Replace the avoid areas, forgetting why the dropped ones were made (unless another area has the
        same center)."""
        if len(areas) < len(self.turned_back_at):
            centers = {p for p, _, _ in areas}
            self.avoid_reasons = {c: why for c, why in self.avoid_reasons.items() if c in centers}
        self.turned_back_at = areas

    def clear_empty_avoid_areas(self, ai: AresBot) -> None:
        """An avoid area whose center we can see with no armed enemy in it is over: the army that
        made it walked off (it used to stay up its full AVOID_SECONDS)."""
        armed_enemies = [e for e in ai.enemy_units if not e.is_memory and armed(e)]
        kept = []
        for center, radius, until in self.turned_back_at:
            unseen = self.avoid_reasons.get(center) == "unseen fire"  # the shooter isn't at the center
            if self.avoid_reasons.get(center) == "scout needed":
                if self.scouted(ai, center, radius):
                    COUNTS["avoid area cleared (scouted)"] += 1
                    continue
                scouting = getattr(ai, "scouting", None)
                if until <= ai.time + 1 and scouting is not None and scouting.eta(ai, center) is not None:
                    until = ai.time + 1  # an overlord is on its way: keep it until it has looked
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
        self.set_avoid_areas(kept)

    def avoided(self, ai: AresBot) -> list[tuple[Point2, float]]:
        """Active avoid areas: (center, radius)."""
        self.set_avoid_areas([(p, r, t) for p, r, t in self.turned_back_at if t > ai.time])
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
        if not threats:
            # Nothing can hit us yet, but enemy units in sight (NOTICE_RANGE) we'd beat are worth going
            # for: the next-step margin decides when to back off, not which fights we notice
            seen = [
                e
                for e in enemies
                if not e.is_structure
                and not e.is_memory
                and armed(e)
                and any(e.position.distance_to(u.position) < NOTICE_RANGE for u in units)
            ]
            if seen and fight_result(ai, units, seen, self.members(ai, squad, seen))[0] >= fight_at:
                COUNTS["go for a winnable fight"] += len(units)
                threats = seen

        if not self.committed and (fire := self.unseen_fire_on(ai, units)) is not None:
            # Repeated hits from something we can't see (cloaked, burrowed) costing this force a real
            # share of its health: leave, and keep away from there for a while
            back = squad.squad_position.towards(fire, -UNSEEN_FIRE_STEP)
            self.mark_avoid(ai, fire, UNSEEN_RADIUS * 2, "unseen fire")
            COUNTS["unseen fire: back off"] += len(units)
            note(units, "unseen fire: back off")
            for unit in units:
                move(ai, unit, back)
            return None

        if threats:
            near_home = ai.townhalls.closest_distance_to(squad.squad_position) < DEFEND_RADIUS
            if not near_home and not self.committed and any(t.tag in self.partly_seen for t in threats):
                # Only part of their position is seen (something fired from the fog): don't fight
                # what we can see; back off until an overlord shows us the rest
                COUNTS["partly seen: back off"] += len(units)
                note(units, "partly seen: back off")
                self.backed_off[squad.id] = ai.time + BACK_OFF_HOLD_SECONDS
                back_off(ai, units, threats, None, retreat=True)
                self.send_squad_scout(ai, squad)
                return None
            result, present = fight_result(ai, units, threats, self.members(ai, squad, threats))
            battle = Battle(
                center_of(threats), threats, present, result, self.room(ai, units, threats, enemies, present)
            )
            if squad.job == "gather" and not near_home and not self.committed:
                # Waiting for help: stay out of their reach (the helpers count in the sim before
                # they're here, so going in now would be alone)
                COUNTS["gathering"] += len(units)
                note(units, "gathering for help")
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
                slots = TargetSlots(
                    reach,
                    self.breach_caps(ai, reach, enemies),
                    self.taken,
                    self.settled,
                    self.cells_taken,
                    self.assignments,
                )
                busy = 0
                position = squad.squad_position  # once, not per zergling (a sum over the squad)
                for unit in front_first(units, enemies):
                    busy += fight(
                        ai,
                        unit,
                        threats,
                        enemies,
                        slots,
                        position,
                        self.blocks,
                        self.detours,
                        self.diagnostics,
                    )
                refused = [t for t in threats if t.tag in CHOKE_NOTES and not CHOKE_NOTES[t.tag][2]]
                if not busy and not (near_home or self.committed):
                    # Every zergling came up empty: the targets are choke fights not worth their cost,
                    # or out of reach with no breach worth making and nothing safe to break. Leave them
                    # and do something else
                    reason = "choke not worth it" if refused else "nothing to do safely"
                    COUNTS[f"leave: {reason}"] += len(units)
                    note(units, f"leave: {reason}")
                    self.fighting.discard(squad.id)
                    self.turned_back(ai, refused or threats, reason)
                    if squad.job != "defend":
                        squad.free()
                    back_off(ai, units, threats, None, retreat=True)
            elif self.cornered(ai, units, threats, enemies):
                # Nowhere safe to back off to (behind a mineral line, in a dead end, their army
                # between us and safety): do as much damage as we can instead of dying for free
                COUNTS["cornered: fight"] += len(units)
                reach = self.reach_map(ai, units, enemies)
                slots = TargetSlots(
                    reach, taken=self.taken, settled=self.settled, cells=self.cells_taken, assignments=self.assignments
                )
                position = squad.squad_position
                for unit in front_first(units, enemies):
                    fight(
                        ai,
                        unit,
                        threats,
                        enemies,
                        slots,
                        position,
                        self.blocks,
                        self.detours,
                        self.diagnostics,
                    )
            else:
                # Just out of their reach, not home: from there it waits for help or patrols on
                # (and isn't an avoid area: one squad losing isn't the army losing)
                COUNTS[f"back off ({result.name})"] += len(units)
                note(units, f"back off: sim {result.name}")
                self.backed_off[squad.id] = ai.time + BACK_OFF_HOLD_SECONDS
                back_off(ai, units, threats, None, retreat=True)
            return battle

        self.fighting.discard(squad.id)  # no enemies close: not in a fight any more

        if squad.job in ("push", "patrol", "raid", "scout") and units:
            # Travelling: follow the route around danger (a sieged tank's range, avoid areas), not ares'
            # path, which goes straight through when there's no cheap way round
            routes = self.route_map(ai, units)
            started = time.perf_counter()
            step = routes.waypoint(squad.squad_position, target)
            if self.debug:
                self.route_ms.append((time.perf_counter() - started) * 1000)
            if step is not None and step.distance_to(target) > 3:
                target = step

        # Heading somewhere we can't see: spread out, so splash (tanks, storms, banelings) hits few
        center = squad.squad_position
        if (
            not threats
            and not any(e.is_structure for e in enemies)
            and len(units) > 1
            and center.distance_to(target) > 5
            and not ai.is_visible(center.towards(target, min(SPREAD_LOOKAHEAD, center.distance_to(target))))
        ):
            COUNTS["spread out"] += len(units)
            note(units, f"spread out: {squad.job}")
            spread_move(ai, units, center, target)
            return None

        slots = TargetSlots(
            self.reach_map(ai, units, enemies) if enemies else None,
            taken=self.taken,
            settled=self.settled,
            cells=self.cells_taken,
            assignments=self.assignments,
        )
        for unit in front_first(units, enemies):
            if advancing:
                if threats or any(e.is_structure for e in enemies):
                    fight(
                        ai,
                        unit,
                        threats,
                        enemies,
                        slots,
                        center,
                        self.blocks,
                        self.detours,
                        self.diagnostics,
                    )
                else:
                    # Follow ares' path around known enemy fire all the way in (an attack-move for the last
                    # stretch walked straight through it). Enemies on the way are handled as fights: one
                    # that can reach us is a threat, one we'd beat in sight is a winnable fight
                    note([unit], f"{squad.job}: moving")
                    move(ai, unit, target)
            elif unit.distance_to(target) > 5:
                note([unit], f"{squad.job}: moving")
                move(ai, unit, target)
            else:
                note([unit], f"{squad.job}: there")
        return None

    def members(self, ai: AresBot, squad: Squad, threats: list[Unit]) -> list[Unit]:
        """Who else fights these enemies with this squad: the squad it's helping and the squads helping
        it, squads gathering with it, squads raiding the same site, squads whose last fight shared enemies
        with this one, zerglings on a strike at these enemies, and queens there (they defend without a
        squad). Not a squad that only happens to be near (fighting something else, passing by)."""
        tags = {t.tag for t in threats}
        striking = {t for s in self.strikes for t in s.lings}
        ids: set[int] = set()
        if squad.helping is not None:
            ids.add(squad.helping)
        for s in self.book.squads.values():
            if s.id == squad.id:
                continue
            if (
                s.helping == squad.id
                or (squad.site is not None and s.site == squad.site and s.job in ("raid", "push"))
                or any(squad.id in r.squads and s.id in r.squads for r in self.rallies.values())
                or (s.id in self.battles and tags & {t.tag for t in self.battles[s.id].threats})
            ):
                ids.add(s.id)
        found: dict[int, Unit] = {}
        for s in self.book.squads.values():
            if s.id in ids:
                for u in s.units:
                    if u.tag not in striking:
                        found[u.tag] = u
        lings = {t for s in self.strikes if s.targets & tags for t in s.lings}
        for u in ai.units.tags_in(lings):
            found[u.tag] = u
        if threats:
            for u in ai.mediator.get_units_in_range(
                start_points=[center_of(threats)], distances=NOTICE_RANGE + 12, query_tree=UnitTreeQueryType.AllOwn
            )[0]:
                if u.type_id == UnitTypeId.QUEEN:
                    found[u.tag] = u
        return list(found.values())

    def breach_caps(
        self,
        ai: AresBot,
        reach: ReachMap | None,
        enemies: list[Unit],
    ) -> dict[int, int]:
        """The structure worth breaking to get at enemy fighters we can't reach (`ReachMap.breaches`),
        as {tag: attackers}: exactly as many as can hit it at once (full damage, nobody piling up
        behind). Worked out once per fight for the whole army (every REACH_SECONDS), from one view of
        it: a reach map from all our army units around those enemies. Each squad pricing the breaches
        from its own side made squads on opposite sides disagree and flip the breach back and forth.

        A breach's cost: its health at the damage rate of the attackers that fit, times the damage of
        the enemies that can hit the cells those attackers stand on (their weapon range measured to
        the attack cells, not to the structure: a stalker reaching only the far corner of a gateway
        doesn't hit zerglings on its near face). Acceptable if, after losing that, we still win the
        fight behind it. The army keeps its breach while it's acceptable; another one has to be more
        than BREACH_SWITCH_SAVING cheaper to take its place."""
        if reach is None:
            return {}
        sealed = [e for e in enemies if not e.is_memory and armed(e) and e.type_id not in WORKER_TYPES]
        if not sealed or not any(e.is_structure and not e.is_flying for e in enemies):
            return {}
        center = center_of(sealed)
        key = (round(center.x / BREACH_VIEW_CELL), round(center.y / BREACH_VIEW_CELL))
        cached = self.breach_evals.get(key)
        if cached is not None and ai.time - cached[0] < REACH_SECONDS:
            return cached[1]
        caps = self.evaluate_breach(ai, center)
        self.breach_evals[key] = (ai.time, caps)
        return caps

    def evaluate_breach(self, ai: AresBot, center: Point2) -> dict[int, int]:
        """`breach_caps` for the fight around `center`, from all our army units near it."""
        ours = [u for u in ai.units if u.type_id in ARMY_TYPES and u.position.distance_to(center) < BREACH_VIEW_RANGE]
        if not ours:
            return {}
        enemies = enemies_near(ai, ours)
        ground = [e for e in enemies if not e.is_flying]
        fighters = [
            e for e in enemies if (not e.is_structure or armed(e)) and e.type_id not in WORKER_TYPES and armed(e)
        ]
        structures = {e.tag: e for e in ground if e.is_structure}
        if not fighters or not structures:
            return {}
        reach = ReachMap(ai.mediator.get_ground_grid, ours, ground, ai.get_terrain_z_height)
        probe = next((u for u in ours if u.type_id == UnitTypeId.ZERGLING), ours[0])
        survivors_from = sorted(ours, key=lambda u: u.health + u.shield, reverse=True)
        candidates = reach.breaches(list(structures.values()), fighters, probe.radius, probe.ground_range)
        best: tuple[float, int, int] | None = None  # (zerglings lost, tag, attackers)
        acceptable: dict[int, tuple[float, int, int]] = {}
        for tag, (attackers, opened) in candidates.items():
            s = structures[tag]
            cells = reach.attack_cells.get(tag, [])
            seconds = (s.health + s.shield) / (attackers * max(probe.calculate_dps_vs_target(s), 0.1))
            # Who can hit our attackers: weapon range (plus the usual step-in margin) to any attack cell
            covering = [
                f
                for f in fighters
                if any(
                    f.position.distance_to(c) <= weapon_range(f) + f.radius + probe.radius + VISIBLE_THREAT_MARGIN
                    for c in cells
                )
            ]
            lost = sum(dps_vs(f, probe) for f in covering) * seconds / max(probe.health_max, 1)
            survivors = survivors_from[: max(0, len(survivors_from) - int(lost + 0.5))]
            # The fight behind it: what it opens up, plus every enemy that's active (it would come to help);
            # idle ones (on hold, untouched) are left out until they move or attack
            counted = [f for f in fighters if f in opened or self.active(ai, f)]
            ok = not counted or wins(ai, survivors, counted)
            COUNTS[f"breach considered (lose ~{lost:.0f})"] += 1
            if self.debug:
                logger.info(
                    f"{ai.time_formatted} breach eval: {s.type_id.name}@({s.position.x:.0f}, {s.position.y:.0f})"
                    f" hp {s.health + s.shield:.0f}, {attackers} attack cells, covered by {len(covering)}"
                    f" ({', '.join(sorted(f.type_id.name.lower() for f in covering)) or 'nothing'}),"
                    f" {seconds:.1f}s, lose ~{lost:.1f}, vs {len(counted)} of {len(fighters)} fighters (active or"
                    f" opened), {'acceptable' if ok else 'not acceptable'}"
                )
            if ok:
                acceptable[tag] = (lost, tag, attackers)
                if best is None or lost < best[0]:
                    best = (lost, tag, attackers)
        # Stick with the army's breach while it's still acceptable: switching walks the zerglings
        # through the enemy's fire and throws away the damage done. Only an option more than
        # BREACH_SWITCH_SAVING cheaper is worth it (finishing a half-broken one gets cheaper anyway)
        kept = next((acceptable[t] for t in self.breach_kept if t in acceptable), None)
        if kept is not None and best is not None and best[0] >= kept[0] * (1 - BREACH_SWITCH_SAVING):
            best = kept
        elif best is not None and kept is not None:
            COUNTS["breach switched (cheaper)"] += 1
            if self.debug:
                logger.info(f"{ai.time_formatted} breach switched: lose ~{kept[0]:.1f} -> ~{best[0]:.1f}")
        if best is None:
            return self.widen_plan(ai, reach, ours, fighters, structures, probe)
        self.widening.clear()  # an access breach comes first
        self.assignments.deferred = set()
        # One breach per fight: drop any other commitment among this fight's options
        for t in [t for t in self.breach_kept if t in candidates and t != best[1]]:
            del self.breach_kept[t]
        self.breach_kept[best[1]] = ai.time
        chosen = structures[best[1]]
        self.breach_notes[best[1]] = (chosen.position, f"breach: lose ~{best[0]:.0f}, {best[2]} on it", ai.time)
        return {best[1]: best[2]}

    def widen_plan(
        self,
        ai: AresBot,
        reach: ReachMap,
        ours: list[Unit],
        fighters: list[Unit],
        structures: dict[int, Unit],
        probe: Unit,
    ) -> dict[int, int]:
        """A building worth breaking to open more sides of an armed target we can already reach, but only
        from part of its ring (a bunker in a wall: one face outside, the others sealed behind bays).

        Per still target (buildings, or units that haven't moved for WIDEN_STILL_SECONDS: one stuck in a
        choke for a moment isn't worth splitting the army for) and per building bordering both our
        ground and the sealed part of its ring: zerglings lost = breaking the building (its health at the
        rate of the attackers that fit, times the damage covering its attack cells) + killing the target
        with the extra attackers that then fit. Taken if it's at least BREACH_SWITCH_SAVING cheaper than
        head on (the attackers that fit now) and the whole plan takes at most WIDEN_MAX_SECONDS. While it
        runs, the target isn't attacked head on (`Assignments.deferred`). Worked out again every
        evaluation: dropped when the target moves, dies or it stops paying off, and given up after
        WIDEN_MAX_SECONDS (none for that target for WIDEN_GIVE_UP_SECONDS)."""
        now = ai.time
        regions = reach.regions()
        ours_ids = set(np.unique(regions[reach.reached & reach.walkable]).tolist()) - {0}
        z_hp = max(probe.health_max, 1)
        options: dict[int, tuple[float, int, int, int, float, float]] = {}  # target -> best plan for it
        for t in fighters:
            if t.is_memory or not armed(t):
                continue
            if not t.is_structure and now - self.enemy_moved.get(t.tag, now) < WIDEN_STILL_SECONDS:
                continue  # moving (or just stopped): no plan around it
            if self.widen_given_up.get(t.tag, -1) > now:
                continue
            if t.tag not in self.widening and any(u.position.distance_to(t.position) < reach_of(t, u) for u in ours):
                # Already engaged with it: the walk up is paid for, so fight it (backing up is only for a
                # lost fight, never to go round to another way in)
                COUNTS["widen: not started (already engaged)"] += 1
                continue
            # Where attackers could stand round it if nothing were in the way, and the ones we reach now
            ring = reach.attack_spots(t, probe.radius, probe.ground_range, reached=False)
            open_now = reach.attack_spots(t, probe.radius, probe.ground_range)
            closed = [p for p in ring if not reach.reached[reach.cell(p)]]
            if not open_now or not closed:
                continue  # unreachable (the access breach's job) or open all round
            full = attack_capacity(probe, t) or len(ring)
            before = min(full, len(open_now))
            closed_ids = {int(regions[reach.cell(p)]) for p in closed} - ours_ids - {0}
            if not closed_ids or before >= full:
                continue
            points = ring
            covering = [
                f
                for f in fighters
                if armed(f)
                and any(
                    f.position.distance_to(q) <= weapon_range(f) + f.radius + probe.radius + VISIBLE_THREAT_MARGIN
                    for q in points
                )
            ]
            incoming = sum(dps_vs(f, probe) for f in covering)
            hp = t.health + t.shield
            rate = max(probe.calculate_dps_vs_target(t), 0.1)
            head_on = incoming * hp / (before * rate) / z_hp
            for s in structures.values():
                if s.tag == t.tag:
                    continue
                border = [
                    c
                    for c in reach._cells_within(s.position, s.radius + 1.2, reach.walkable.shape)
                    if reach.walkable[c]
                ]
                touching = {int(regions[c]) for c in border}
                if not (touching & ours_ids and touching & closed_ids):
                    continue
                after = min(full, before + sum(1 for p in closed if int(regions[reach.cell(p)]) in touching))
                if after <= before:
                    continue
                spots = reach.attack_spots(s, probe.radius, probe.ground_range)
                attackers = max(1, len(spots))
                break_secs = (s.health + s.shield) / (attackers * max(probe.calculate_dps_vs_target(s), 0.1))
                on_s = sum(
                    dps_vs(f, probe)
                    for f in fighters
                    if armed(f)
                    and any(
                        f.position.distance_to(q) <= weapon_range(f) + f.radius + probe.radius + VISIBLE_THREAT_MARGIN
                        for q in spots
                    )
                )
                kill_secs = hp / (after * rate)
                lost = on_s * break_secs / z_hp + incoming * kill_secs / z_hp
                secs = break_secs + kill_secs
                ok = secs <= WIDEN_MAX_SECONDS and lost <= head_on * (1 - BREACH_SWITCH_SAVING)
                if self.debug:
                    logger.info(
                        f"{ai.time_formatted} widen eval: {name_of(t)} via {name_of(s)}: lose ~{lost:.1f}"
                        f" in {secs:.0f}s ({before} -> {after} on it) vs head on ~{head_on:.1f},"
                        f" {'take' if ok else 'no'}"
                    )
                if ok and (t.tag not in options or lost < options[t.tag][0]):
                    options[t.tag] = (lost, s.tag, attackers, t.tag, head_on, secs)
        # Keep a running plan while it still pays off (its target still and the 30 s not used up)
        for tag, (_building, started) in list(self.widening.items()):
            if now - started > WIDEN_MAX_SECONDS:
                COUNTS["widen: given up (too long)"] += 1
                self.widen_given_up[tag] = now + WIDEN_GIVE_UP_SECONDS
                del self.widening[tag]
            elif tag not in options:
                COUNTS["widen: dropped (target moved, died or no longer cheaper)"] += 1
                del self.widening[tag]
        if not options:
            self.assignments.deferred = set()
            return {}
        lost, s_tag, attackers, t_tag, head_on, secs = min(options.values())
        kept = next(((t, plan) for t, plan in self.widening.items() if t in options), None)
        if kept is not None:
            t_tag = kept[0]
            lost, s_tag, attackers, _, head_on, secs = options[t_tag]
        started = self.widening.get(t_tag, (s_tag, now))[1]
        self.widening = {t_tag: (s_tag, started)}
        self.assignments.deferred = {t_tag}
        target, building = next(e for e in fighters if e.tag == t_tag), structures[s_tag]
        COUNTS["widen: on"] += 1
        self.breach_notes[s_tag] = (
            building.position,
            f"widen {name_of(target)}: lose ~{lost:.0f} vs ~{head_on:.0f} head on,"
            f" {now - started:.0f}/{WIDEN_MAX_SECONDS:.0f}s",
            now,
        )
        return {s_tag: attackers}

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
        if reach is not None:
            # Work there besides fighting units: the breach (as many as hit it at once) and the safe cells
            # next to other structures (outside every enemy's reach)
            caps = self.breach_caps(ai, reach, enemies)
            slots += sum(caps.values())
            if reach.danger_stamp is not None:
                slots += sum(
                    reach.safe_spot_count(s, probe.radius, probe.ground_range)
                    for s in enemies
                    if s.is_structure and not s.is_flying and s.tag not in caps
                )
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
    """Enemy ground units/structures within THREAT_LOOKUP of any unit in the squad (as far as anything
    can hit us from: `can_reach_soon` then picks the ones that actually can).

    Includes ares' memory of units last seen in the fog (e.g. a sieged tank revealed by firing).
    Memory units aren't in python-sc2's per-frame distance cache, so always measure distances
    to enemies with positions (`a.position.distance_to(b.position)`), never `unit.distance_to(unit)`.
    """
    found: dict[int, Unit] = {}
    for group in ai.mediator.get_units_in_range(
        start_points=[u.position for u in units], distances=THREAT_LOOKUP, query_tree=UnitTreeQueryType.EnemyGround
    ):
        for e in group:
            if e.type_id not in IGNORED_THREATS:
                found[e.tag] = e
    return list(found.values())


def fight_result(
    ai: AresBot, units: list[Unit], threats: list[Unit], others: list[Unit] | None = None
) -> tuple[EngagementResult, list[Unit]]:
    """The combat sim's verdict for a fight against `threats`, and the army units of ours it counted:
    `units` plus `others`, the rest of the fight's members (`Army.members`: squads helping or fighting
    the same enemies, strikes on them, queens there), not whoever happens to be nearby."""
    mine = {u.tag for u in units}
    own = [u for u in (others or []) if u.tag not in mine] + list(units)
    return combat_estimate(ai, own, threats), own


def combat_estimate(
    ai: AresBot, own: list[Unit], threats: list[Unit], reach: ReachMap | None = None
) -> EngagementResult:
    """The combat sim's verdict for `own` against `threats`, corrected where it's wrong: a sieged tank we're
    already on top of can't shoot us (`tank_neutralized`), and a bunker shoots (`through_bunkers`, its spots
    counted on `reach` if given). Every fight, strike, raid and help judgement goes through this."""
    if not own:
        return EngagementResult.LOSS_EMPHATIC
    threats = [t for t in threats if not tank_neutralized(t, own)]
    if not threats:
        return EngagementResult.VICTORY_EMPHATIC
    bunkers = [t for t in threats if t.type_id == UnitTypeId.BUNKER]
    melee = [u for u in own if u.type_id in ARMY_TYPES]
    if bunkers and melee:
        return through_bunkers(ai, own, melee, bunkers, [t for t in threats if t not in bunkers], reach)
    return fight_verdict(ai, own, threats)


def wins(ai: AresBot, own: list[Unit], enemies: list[Unit], at: EngagementResult = FIGHT_AT) -> bool:
    """Do `own` beat `enemies` (at least `at`), by `combat_estimate`."""
    return combat_estimate(ai, own, enemies) >= at


def through_bunkers(
    ai: AresBot,
    own: list[Unit],
    melee: list[Unit],
    bunkers: list[Unit],
    others: list[Unit],
    reach: ReachMap | None = None,
) -> EngagementResult:
    """The sim doesn't know a bunker shoots (its marines are inside), so judge it in two steps: the
    zerglings that fit around the bunkers (the cells next to them we reach, given `reach`) kill them
    while the marines inside fire; then the survivors fight the marines that come out of the dead
    bunkers (BUNKER_MARINES each: we can't see inside) and whatever else is there."""
    probe = melee[0]
    attackers = min(
        len(melee),
        sum(
            reach.attack_slots(b, probe.radius, attack_capacity(probe, b) or len(melee), probe.ground_range)
            if reach is not None
            else (attack_capacity(probe, b) or len(melee))
            for b in bunkers
        ),
    )
    our_dps = attackers * max(probe.calculate_dps_vs_target(bunkers[0]), 0.1)
    seconds = sum(b.health + b.shield for b in bunkers) / our_dps
    lost = sum(dps_vs(b, probe) for b in bunkers) * seconds / max(probe.health_max, 1)
    COUNTS[f"bunker fight (lose ~{lost:.0f})"] += 1
    keep = max(0, len(melee) - int(lost + 0.5))
    survivors = sorted(melee, key=lambda u: u.health, reverse=True)[:keep]
    if not survivors:
        return EngagementResult.LOSS_OVERWHELMING
    # The marines that come out of the dead bunkers
    keep, marines_left = duel(keep, max(probe.health_max, 1), ZERGLING_DPS_VS_MARINE, BUNKER_MARINES * len(bunkers))
    COUNTS[f"bunker fight: marines after (we keep {keep})"] += 1
    if keep == 0:
        return (
            EngagementResult.LOSS_DECISIVE
            if marines_left > BUNKER_MARINES * len(bunkers) / 2
            else EngagementResult.LOSS_MARGINAL
        )
    survivors = survivors[:keep]
    if others:
        rest = [u for u in own if u.type_id not in ARMY_TYPES] + survivors
        return fight_verdict(ai, rest, others)
    share = keep / len(melee)  # what's left of us once the bunkers are dead
    if share >= 0.75:
        return EngagementResult.VICTORY_DECISIVE
    if share >= 0.5:
        return EngagementResult.VICTORY_CLOSE
    return EngagementResult.VICTORY_MARGINAL if share >= 0.25 else EngagementResult.LOSS_MARGINAL


def duel(zerglings: int, zergling_hp: float, zergling_dps: float, marines: int) -> tuple[int, int]:
    """Zerglings against marines in the open, one target at a time on each side (every zergling hits one
    marine, every marine shoots one zergling): (zerglings left, marines left)."""
    z_hp, m_hp = [zergling_hp] * zerglings, [float(MARINE_HP)] * marines
    for _ in range(600):  # 0.1 s steps, 60 s at most
        if not z_hp or not m_hp:
            break
        m_hp[0] -= len(z_hp) * zergling_dps * 0.1
        z_hp[0] -= len(m_hp) * MARINE_DPS * 0.1
        if m_hp[0] <= 0:
            m_hp.pop(0)
        if z_hp and z_hp[0] <= 0:
            z_hp.pop(0)
    return len(z_hp), len(m_hp)


def strike_result(ai: AresBot, own: list[Unit], group: list[Unit], reach: ReachMap | None = None) -> EngagementResult:
    """The verdict for `own` against `group` (`combat_estimate`)."""
    return combat_estimate(ai, own, group, reach)


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
    diagnostics: bool = False,
) -> bool:
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
    # Its own slot from last step is its to take again: release the reservation before any room check (the
    # "all work full: hold" check counted it as taken, so zerglings on the breach left it every other step)
    slots.assignments.release(unit)
    dive: list[Unit] = []
    in_the_way: list[Unit] = []
    covered: list[Unit] = []
    clear: list[Unit] = []
    key: list[Unit] = []
    fighters = [e for e in threats if e.type_id not in WORKER_TYPES]
    # Every fighter in this fight is a candidate, however far: whether to engage it (and with whom) was
    # decided for the group (the squad's sim verdict, a strike, a raid), not by each zergling. Plugs
    # are filtered out below; slots cap how many go for each
    near_fighters = list(fighters)
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

        plugs = slots.reach.plugs([e for e in fighters if not e.is_structure], fighters)
        # Danger: every cell an armed enemy can hit (its reach plus a margin; melee units included, so a
        # zealot in a gap makes the gateway faces next to it unsafe too)
        slots.reach.set_danger(
            [(e.position, reach_of(e, unit) + SAFE_WORK_MARGIN) for e in enemies if armed(e)], ai.time
        )

        def surroundable(e: Unit) -> bool:
            # Reachable, and not plugging a gap. A plug blocks our way to other enemy units (a zealot
            # between gateways or on a ramp, stalkers behind) and isn't hitting us from there (a melee
            # unit): fighting through it 1-on-1 loses. Anything else is fair game, slots capped by the
            # open cells around it (a clump of marines in the open, a marine shooting from a wall),
            # unless it's a choke fight not worth its cost (`choke_worth`)
            if slots.reach.open_sides(e, unit.radius) == 0:
                return False
            if e.tag in slots.assignments.deferred:
                return False  # a widening plan is opening more sides of it first (`Army.widen_plan`)
            if e.tag in plugs and weapon_range(e) < PLUG_HARM_RANGE:
                return False
            return choke_worth(ai, unit, e, slots, fighters, structures)

        pinned = [
            e
            for e in near_fighters
            if touching(e) and not surroundable(e) and not e.is_structure and e.ground_range <= 1
        ]
        if pinned:
            nearest = min(pinned, key=lambda e: e.position.distance_to(position))
            COUNTS["step away (gap)"] += 1
            why = "plug" if nearest.tag in plugs else "choke not worth it"
            if nearest.tag in CHOKE_OPENERS:
                why = f"open it up first: {CHOKE_OPENERS[nearest.tag]}"
            elif nearest.tag in CHOKE_NOTES:
                lost, worth, _ = CHOKE_NOTES[nearest.tag]
                why += f", lose ~{lost:.0f} vs worth {worth:.0f}"
            note([unit], f"step away from {name_of(nearest)} ({why})")
            unit.move(nearest.position.towards(position, nearest.radius + unit.radius + 2.0))
            return False
        near_fighters = [e for e in near_fighters if surroundable(e) or touching(e)]
        workers = [e for e in workers if slots.reach.reachable(e)]
    if near_fighters:
        dive = [e for e in near_fighters if e.type_id in DIVE_FIRST]
        # When every close fighter is full or blocked (e.g. zealots in 1-tile gaps between
        # gateways), break through the structure in the way rather than die in the lane
        in_the_way = safe_work(
            ai, slots, [s for s in structures if s.position.distance_to(position) - s.radius < BREAKTHROUGH_RANGE], unit
        )
        if not any(slots.has_room(unit, e) for e in near_fighters):
            # Every fighter full: the buildings only busy enemies can hit are free to work
            covered = covered_work(slots, structures, enemies, unit)
        tiers = [
            (dive, True),
            ([e for e in near_fighters if e not in dive], True),
            (workers, False),
            (breach, False),
            (in_the_way, False),
            (covered, False),
        ]
    elif any(e.type_id not in WORKER_TYPES for e in threats):
        # Fighters around but none we can reach: the breach target (inside their range, capped at its
        # slots), else the closest structures we can hit from outside their range
        # Any structure with a free safe cell we can walk to around the danger, however far: distance adds no
        # safety (a zergling parked down the ramp had "nothing to do" with every gateway 10+ tiles away).
        # Without a reach map (no safe cells), only the ones close by
        if slots.reach is not None:
            clear = safe_work(ai, slots, structures, unit)
        else:
            clear = safe_structures(
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
    if not tiers and fighters and slots.reach is not None:
        # Diagnostic: why every enemy unit here was left out (memory = only remembered, not seen now)
        for e in fighters:
            mem = " (remembered)" if e.is_memory else ""
            if slots.reach.open_sides(e, unit.radius) == 0:
                COUNTS[f"nothing to do: {e.type_id.name.lower()} unreachable{mem}"] += 1
                dump_reach(ai, slots.reach, unit, fighters, enemies)
            elif e.tag in plugs and weapon_range(e) < PLUG_HARM_RANGE:
                COUNTS[f"nothing to do: {e.type_id.name.lower()} plug{mem}"] += 1
            else:
                COUNTS[f"nothing to do: {e.type_id.name.lower()} choke refused{mem}"] += 1
    if not tiers:
        # Nothing worth doing here: replace whatever order it still has (an attack picked earlier would
        # carry it on into their fire) with stepping out of their reach and waiting there
        note([unit], "nothing to do (no safe work, no breach): hold outside their reach")
        hold(ai, unit, threats)
        return False  # nothing to go after (e.g. only far-off remembered units, or nothing safe to break)
    if not near_fighters and fighters and not any(slots.has_room(unit, e) for t, _ in tiers for e in t):
        # Only structure work, and all of it full (the breach target saturated): wait just outside
        # their reach to take the place of a breach attacker that dies, instead of piling on
        COUNTS["hold for the breach"] += 1
        note([unit], "hold outside their reach (all work full)")
        back_off(ai, [unit], [e for e in threats if armed(e)], None)
        return True

    now = ai.time
    maneuver = CombatManeuver()
    # On a detour around our own crowd: keep following it to that target
    if (route_target := detours.route_target(unit, now)) is not None:
        goal = next((e for t, _ in tiers for e in t if e.tag == route_target), None)
        waypoint = detours.next_waypoint(unit) if goal is not None else None
        if goal is not None and waypoint is not None and not unit.target_in_range(goal, 0.25):
            COUNTS["detour follow"] += 1
            # Still going for it: it holds a slot there (its move order doesn't say so)
            slots.assignments.release(unit)
            slots.take(goal, unit)
            slots.assignments.assign(unit, goal, now)
            note([unit], f"detour to {name_of(goal)}")
            maneuver.add(ShootTargetInRange(unit=unit, targets=[goal]))
            maneuver.add(
                PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=waypoint, sense_danger=False)
            )
            ai.register_behavior(maneuver)
            return True
        detours.drop(unit)

    if TARGET_FILL == "off":
        goal = min((e for t, _ in tiers for e in t), key=lambda e: e.position.distance_to(position))
        flank = False
    else:
        depth_from = squad_position if TARGET_FILL == "back" else None
        goal, flank = pick_tiered(slots, blocks, unit, tiers, depth_from, now)
    if goal is None:
        note([unit], "no target picked: hold outside their reach")
        hold(ai, unit, threats)
        return False
    if goal.is_structure and goal.tag in slots.safe_cells and goal.tag not in slots.caps:
        # Safe work: stand on a safe cell next to it (walking there around their reach), hit it from there
        return work_from_cell(ai, unit, goal, slots, threats)
    if goal.is_structure and goal.tag in slots.caps and (done := work_breach_cell(ai, unit, goal, slots)) is not None:
        # The breach: walk to a cell next to it around the danger (only the last tiles are in their fire)
        return done
    if goal in covered:
        room = f"{slots.taken.get(goal.tag, 0)}/{len(slots.covered[goal.tag])}"
        if (done := work_spot(ai, unit, goal, slots, slots.covered[goal.tag], "covered work", room)) is not None:
            COUNTS["covered work"] += 1
            return done
    same_level = goal.is_flying or abs(ai.get_terrain_z_height(unit) - ai.get_terrain_z_height(goal)) < LEVEL_TOLERANCE
    if not flank and blocks.update(unit, goal, now, same_level):
        # Just got stuck on this one. An enemy unit: go around our own crowd to it if there's a
        # reasonable way; otherwise (or a structure) take another target we can reach
        crowd = [u for u in ai.units if u.tag != unit.tag and u.type_id in ARMY_TYPES and u.distance_to(unit) < 8]
        if not goal.is_structure and not goal.is_memory and detours.plan(ai, unit, goal, crowd, now):
            COUNTS["detour planned"] += 1
            return fight(ai, unit, threats, enemies, slots, squad_position, blocks, detours, diagnostics)
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
    # Only for the debug counts and the overlay's note: nothing decides on it
    unsafe = diagnostics and not ai.mediator.is_position_safe(grid=ai.mediator.get_ground_grid, position=position)
    if unsafe:
        COUNTS[f"UNSAFE {category}"] += 1
    if goal in dive:
        tier = "dive"
    elif goal in near_fighters:
        tier = "fighter"
    elif goal in workers:
        tier = "worker"
    elif goal in breach:
        tier = "breach"
    elif goal in in_the_way:
        tier = "break through (safe)"
    elif goal in covered:
        tier = "covered work"
    elif goal in clear:
        tier = "clear the way (safe)"
    elif goal in key:
        tier = "key structure"
    else:
        tier = "target"
    capacity = slots.capacity(unit, goal)
    room = f" {slots.taken.get(goal.tag, 0)}/{'-' if capacity is None else capacity}"
    note([unit], f"{category}: {name_of(goal)} [{tier}{room}]{' IN RANGE' if unsafe else ''}")

    if flank and not goal.is_memory:
        side = 1 if unit.tag % 2 else -1  # alternate sides so they spread around the jam
        away = goal.position.towards(unit.position, 1) - goal.position
        around = goal.position + Point2((-away.y, away.x)) * side * (goal.radius + FLANK_DISTANCE)
        maneuver.add(ShootTargetInRange(unit=unit, targets=[e for t, _ in tiers for e in t]))
        maneuver.add(PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=around, sense_danger=False))
        ai.register_behavior(maneuver)
        return True
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
        return True
    if chase := chase_behavior(unit, goal):
        maneuver.add(chase)
    ai.register_behavior(maneuver)
    return True


def choke_worth(
    ai: AresBot, unit: Unit, target: Unit, slots: TargetSlots, fighters: list[Unit], structures: list[Unit]
) -> bool:
    """Whether to attack a melee unit that isn't in the open (a zealot in a lane or on a ramp).

    First: while a building touching it can be worked from safe cells, open it up instead. Breaking that
    building costs nothing and gives the unit more open sides, so the fight gets cheaper. Then, with room
    for at most CHOKE_SLOTS attackers, it's taken only if what we'd lose killing it that slowly is worth
    it: zerglings lost = the damage of everything covering its attack cells (itself included) over the
    time that many zerglings take to kill it, against its cost in zerglings. Worked out once per target
    per step (`CHOKE_NOTES`)."""
    if target.is_structure or weapon_range(target) >= PLUG_HARM_RANGE:
        # Only a unit that can't hit us from where it stands (a zealot) can be left alone: refusing a
        # stalker in a gap doesn't stop it shooting us
        return True
    # Units the game puts no price on (spawned ones like broodlings, burrowed forms, ...) aren't worth losing
    # zerglings to in a choke
    if (value := production_cost(ai, target.type_id)) is None:
        return False
    if target.tag in CHOKE_NOTES:
        return CHOKE_NOTES[target.tag][2]
    capacity = slots.capacity(unit, target)
    full = attack_capacity(unit, target)
    if (
        capacity is not None
        and full is not None
        and capacity < full
        and slots.reach is not None
        and slots.reach.danger_stamp is not None
    ):
        # Partly enclosed: a building next to it we can break without taking fire comes first
        opener = next(
            (
                s
                for s in structures
                if s.position.distance_to(target.position) - s.radius - target.radius < 1.5
                and slots.reach.safe_spot_count(s, unit.radius, unit.ground_range) > 0
            ),
            None,
        )
        if opener is not None:
            CHOKE_NOTES[target.tag] = (0.0, 0.0, False)
            CHOKE_OPENERS[target.tag] = name_of(opener)
            COUNTS["choke: open it up first"] += 1
            return False
    if capacity is None or capacity > CHOKE_SLOTS:
        return True
    seconds = (target.health + target.shield) / (capacity * max(unit.calculate_dps_vs_target(target), 0.1))
    cells = slots.reach.attack_points(target, unit.radius) if slots.reach is not None else []
    if cells:
        # Whoever can hit the cells our attackers would stand on (weapon reach to those cells)
        covering = [
            f
            for f in fighters
            if armed(f)
            and any(
                f.position.distance_to(c) <= weapon_range(f) + f.radius + unit.radius + VISIBLE_THREAT_MARGIN
                for c in cells
            )
        ]
    else:
        covering = [
            f
            for f in fighters
            if armed(f) and f.position.distance_to(target.position) - target.radius - f.radius < weapon_range(f) + 1.5
        ]
    if target not in covering and armed(target):
        covering.append(target)
    lost = sum(dps_vs(f, unit) for f in covering) * seconds / max(unit.health_max, 1)
    worth = value / 25  # a zergling costs 25
    taken = lost <= worth
    CHOKE_NOTES[target.tag] = (lost, worth, taken)
    COUNTS["choke: taken" if taken else "choke: not worth it"] += 1
    return taken


def production_cost(ai: AresBot, type_id: UnitTypeId) -> int | None:
    """Minerals + gas to make a unit of this type; None for types the game gives no production cost.
    ares lists spawned/free units (broodlings, locusts, ...), but other types (e.g. burrowed forms) have no
    creation ability either, and python-sc2's `calculate_cost` raises for them instead of returning zero:
    a benchmark game against Zerg crashed on it."""
    if type_id in IGNORE_IN_COST_DICT:
        return None
    try:
        cost = ai.calculate_cost(type_id)
    except (AttributeError, KeyError):
        return None
    return cost.minerals + cost.vespene


REACH_DUMPED: set[int] = set()


def dump_reach(ai: AresBot, reach: ReachMap, unit: Unit, fighters: list[Unit], enemies: list[Unit]) -> None:
    """Diagnostic (ZASZ_DUMP_REACH=path): save this reach map's view once per minute of game time, to see
    why enemy units count as unreachable."""
    path = os.environ.get("ZASZ_DUMP_REACH")
    minute = int(ai.time // 60)
    if not path or minute in REACH_DUMPED:
        return
    REACH_DUMPED.add(minute)
    np.savez(
        f"{path}-{int(ai.time)}s.npz",
        walkable=reach.walkable,
        reached=reach.reached,
        origin=np.array([reach.x0, reach.y0]),
        zergling=np.array([unit.position.x, unit.position.y]),
        fighters=np.array([[e.position.x, e.position.y, e.radius] for e in fighters]),
        structures=np.array([[e.position.x, e.position.y, e.radius] for e in enemies if e.is_structure]),
        ground=np.isfinite(ai.mediator.get_ground_grid).astype(np.uint8),
        pathing=np.array(ai.game_info.pathing_grid.data_numpy).astype(np.uint8),
    )


def hold(ai: AresBot, unit: Unit, threats: list[Unit]) -> None:
    """Step out of the threats' reach and wait there (cancelling any older order)."""
    if threats:
        back_off(ai, [unit], threats, None)
    elif unit.orders:
        unit.stop()


def covered_work(slots: TargetSlots, structures: list[Unit], enemies: list[Unit], unit: Unit) -> list[Unit]:
    """Buildings that are "conditionally free" while every fighter is full: free spots next to them in
    reach only of enemies already busy with our attackers (`TargetSlots.busy`). Those enemies are shooting
    the zerglings on them and can't shoot both, so an extra zergling here costs nothing more: it hits the bays
    beside a full bunker, or the gateways next to zealots being fought, instead of queuing in their fire.
    Within CLEAR_THE_WAY_RANGE, closest first; the spots go in `TargetSlots.covered`. Which enemies reach
    each spot is worked out once per step (`ReachMap.step_cache`); per zergling only who's busy is checked."""
    reach = slots.reach
    if reach is None:
        return []
    found = []
    for s in structures:
        if armed(s) or s.tag in slots.caps or s.position.distance_to(unit.position) - s.radius > CLEAR_THE_WAY_RANGE:
            continue
        key = ("covering", s.tag, unit.radius, unit.ground_range)
        covering = reach.step_cache.get(key)
        if covering is None:
            armed_enemies = [e for e in enemies if armed(e) and e.type_id not in WORKER_TYPES]
            covering = [
                (p, [e.tag for e in armed_enemies if p.distance_to(e.position) <= reach_of(e, unit)])
                for p in reach.attack_spots(s, unit.radius, unit.ground_range)
            ]
            reach.step_cache[key] = covering
        spots = [p for p, tags in covering if spot_key(p) not in slots.cells_taken and all(slots.busy(t) for t in tags)]
        if spots:
            slots.covered[s.tag] = spots
            found.append(s)
    return sorted(found, key=lambda s: s.position.distance_to(unit.position))


def safe_work(ai: AresBot, slots: TargetSlots, structures: list[Unit], unit: Unit) -> list[Unit]:
    """The structures this zergling can work without taking fire: ones with cells next to them outside
    every enemy's reach (`ReachMap.set_danger`), on the same patch of safe ground as the zergling (so
    it can walk there around the danger). Each takes as many zerglings as it has such cells
    (`TargetSlots.safe_cells`). Closest first. Without a reach map: `safe_structures`."""
    if slots.reach is None or slots.reach.danger_stamp is None:
        return safe_structures(ai, structures, unit)
    area = slots.reach.safe_area(unit.position)
    found = []
    for s in structures:
        cells = slots.reach.safe_attack_spots(s, unit.radius, area, unit.ground_range)
        if cells:
            slots.safe_cells[s.tag] = cells
            found.append(s)
    return sorted(found, key=lambda s: s.position.distance_to(unit.position))


def work_from_cell(ai: AresBot, unit: Unit, goal: Unit, slots: TargetSlots, threats: list[Unit]) -> bool:
    """Hit `goal` from a safe spot next to it (`ReachMap.attack_spots`), the closest free one; walk there
    around the danger. No free spot or no safe way: hold outside their reach."""
    reach = slots.reach
    free = [p for p in slots.safe_cells.get(goal.tag, []) if spot_key(p) not in slots.cells_taken]
    if not free:
        note([unit], f"hold: no free safe cell at {name_of(goal)}")
        hold(ai, unit, threats)
        return True
    spot = min(free, key=lambda p: p.distance_to(unit.position))
    slots.cells_taken.add(spot_key(spot))
    cell = reach.cell(spot)
    capacity = len(slots.safe_cells[goal.tag])
    room = f"{slots.taken.get(goal.tag, 0)}/{capacity}"
    if reach.safe_area(unit.position) and unit.target_in_range(goal, 0.05):
        note([unit], f"safe work: {name_of(goal)} [{room}] hitting it")
        if not (unit.is_attacking and unit.order_target == goal.tag):
            unit.attack(goal)
        return True
    step = reach.safe_step(cell, unit.position)
    if step is None:
        note([unit], f"hold: no safe way to {name_of(goal)}")
        hold(ai, unit, threats)
        return True
    note([unit], f"safe work: {name_of(goal)} [{room}] walking to its cell")
    unit.move(step if step.distance_to(unit.position) > 0.3 else spot)
    return True


def work_breach_cell(ai: AresBot, unit: Unit, goal: Unit, slots: TargetSlots) -> bool | None:
    """Hit the breach target from a spot next to it (`ReachMap.attack_spots`), see `work_spot`. None when
    that can't be worked out (no reach map or danger, no free spot, no way): plain attack instead."""
    reach = slots.reach
    if reach is None or reach.danger_stamp is None:
        COUNTS["breach walk: no reach map"] += 1
        return None
    room = f"{slots.taken.get(goal.tag, 0)}/{slots.caps.get(goal.tag, '-')}"
    return work_spot(ai, unit, goal, slots, reach.attack_spots(goal, unit.radius, unit.ground_range), "breach", room)


def work_spot(
    ai: AresBot, unit: Unit, goal: Unit, slots: TargetSlots, spots: list[Point2], what: str, room: str
) -> bool | None:
    """Hit `goal` from one of `spots` next to it: the one it's on if free, else a free one (out of danger
    first, then closest), walked to around the danger except for the last tiles. None when there's no free
    spot or no way there (the caller falls back to a plain attack)."""
    reach = slots.reach
    spots = [p for p in spots if spot_key(p) not in slots.cells_taken]
    if not spots:
        COUNTS[f"{what} walk: no free spot"] += 1
        return None
    nearest = min(spots, key=lambda p: p.distance_to(unit.position))
    spot = (
        nearest
        if nearest.distance_to(unit.position) < unit.radius
        else min(spots, key=lambda p: (reach.in_danger(p), p.distance_to(unit.position)))
    )
    if unit.target_in_range(goal, 0.05):
        slots.cells_taken.add(spot_key(nearest))
        note([unit], f"{what}: {name_of(goal)} [{room}] hitting it")
        if not (unit.is_attacking and unit.order_target == goal.tag):
            unit.attack(goal)
        return True
    step = reach.breach_step(reach.cell(spot), unit.position)
    if step is None:
        COUNTS[f"{what} walk: no way around danger"] += 1
        return None
    slots.cells_taken.add(spot_key(spot))
    note([unit], f"{what}: {name_of(goal)} [{room}] walking to its spot")
    unit.move(step if step.distance_to(unit.position) > 0.3 else spot)
    return True


def safe_structures(ai: AresBot, structures: list[Unit], unit: Unit) -> list[Unit]:
    """Only the structures we can hit from outside known enemy weapon range, closest first. Inside
    enemy range the one structure worth working is the breach target (`Army.breach_caps`); anything
    else there is zerglings dying for nothing."""
    grid = ai.mediator.get_ground_grid
    safe = [
        s
        for s in structures
        if ai.mediator.is_position_safe(
            grid=grid, position=s.position.towards(unit.position, s.radius + unit.radius + 0.2)
        )
    ]
    return sorted(safe, key=lambda s: s.position.distance_to(unit.position))


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


def hit_fits(enemy: Unit, unit: Unit, amount: float) -> bool:
    """Could one attack (or one of its hits, e.g. a zealot's two blades) from `enemy` deal `amount`?"""
    try:
        damage = enemy.calculate_damage_vs_target(unit)[0]
    except Exception:  # noqa: BLE001 - no weapon data: can't tell, don't count it
        return False
    return damage > 0 and (abs(amount - damage) <= 2 or abs(amount - damage / 2) <= 2)


def center_of_points(points: list[Point2]) -> Point2:
    return Point2((sum(p.x for p in points) / len(points), sum(p.y for p in points) / len(points)))


def reach_of(threat: Unit, unit: Unit) -> float:
    """How close `unit` can be to `threat` before it can be hit (edge to edge plus weapon range)."""
    return weapon_range(threat) + threat.radius + unit.radius


def back_off(
    ai: AresBot, units: list[Unit], threats: list[Unit], hold_at: Point2 | None, retreat: bool = False
) -> None:
    """Get out of the threats' actual weapon range (not ares' danger map, which pads every range by
    4 and sent zerglings all the way home), then stop: at `hold_at` if given (a rally), else right
    there. Units already out of reach wait."""
    if not threats:
        for unit in units:
            if unit.orders:
                unit.stop()
        return
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
