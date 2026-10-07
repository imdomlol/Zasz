"""Army control: squads, fight-or-retreat, and per-unit target selection.

Zerglings get the ATTACKING role when they hatch (see `ZaszBot.on_unit_created`). `SquadBook`
(`bot/squads.py`) keeps them in squads and gives each a job (patrol, raid, help, defend, ...).
`Army` runs it all every step (`_control`) and decides each squad's fight (`control_squad`); its
parts are in their own modules:

- `bot.jobs`: where each squad goes, raids, help for losing fights, rallies, pushes, squad scouts
- `bot.strikes`: zerglings sent for enemies shooting our units
- `bot.breach`: the building worth breaking to get at enemies behind it
- `bot.routes` and `bot.avoid`: routes around danger, reach maps, and areas to keep out of
- `bot.army_debug` and `bot.army_notes`: debug logs, counts and overlay notes
- `bot.micro`: what each zergling does in a fight (`fight`), and how units move
- `bot.combat`: who can hit whom, and who wins (the combat sim, corrected)

Each squad:

1. Fights if enemies are close and the combat sim predicts at least an even fight (or we're
   defending a base). Zerglings dive siege tanks first instead of trading with them from the
   edge of their range.
2. Retreats as soon as the sim predicts a loss, but only until it's out of known enemy range,
   then re-engages if the fight looks winnable. Flip-flopping keeps pressure on; the rule is
   that units never stand in enemy fire without either fighting or leaving.
3. Otherwise goes about its job, along routes around armies it can't beat; squads joining a
   fight gather outside the enemy's reach first, so they don't trickle in.

Threats are looked up around every unit in the squad (not just its center), and units that
are being shot from out of sight count as "under fire", so long-range units like sieged tanks
can't hit the front of the squad unnoticed.

Whether to attack at all is a decision (`Decision.attack`); this module only executes.
"""

import math
import time

from ares import AresBot
from ares.consts import EngagementResult, UnitRole, UnitTreeQueryType
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot import overlay
from bot.army_debug import ArmyDebug
from bot.army_notes import (
    BACK_OFF_CIRCLES,
    CHOKE_NOTES,
    CHOKE_OPENERS,
    COUNTS,
    REACH_DUMPED,
    TARGET_NOTES,
    UNIT_NOTES,
    note,
)
from bot.avoid import AvoidAreas
from bot.breach import Breaches
from bot.combat import (
    ARMY_TYPES,
    IGNORED_THREATS,
    armed,
    can_reach_soon,
    center_of_points,
    enemies_near,
    fight_result,
    front_first,
    hit_fits,
    reach_of,
    weapon_range,
)
from bot.jobs import SquadJobs
from bot.micro import back_off, fight, move, spread_move
from bot.routes import Routes
from bot.settings import (
    ACTIVE_MOVE,
    ACTIVE_TURN,
    BACK_OFF_HOLD_SECONDS,
    BACK_OFF_MARGIN,
    BREACH_KEEP_SECONDS,
    DEFEND_RADIUS,
    DISENGAGE_AT,
    ENEMY_IDLE_SECONDS,
    FIGHT_AT,
    HELP_ROOM_FACTOR,
    HELP_SECONDS,
    NOTICE_RANGE,
    PLAN_SECONDS,
    REACH_KEEP_SECONDS,
    SPREAD_LOOKAHEAD,
    UNSEEN_DAMAGE_SHARE,
    UNSEEN_EXPLAIN_SECONDS,
    UNSEEN_FIRE_STEP,
    UNSEEN_MIN_HITS,
    UNSEEN_RADIUS,
    UNSEEN_WINDOW,
)
from bot.squads import (
    Battle,
    EnemyGroup,
    Patrol,
    Rally,
    ScoutPlan,
    Squad,
    SquadBook,
    Strike,
    center_of,
)
from bot.strikes import Strikes
from bot.targeting import (
    Assignments,
    BlockTracker,
    Detours,
    ReachMap,
    RouteMap,
    TargetSlots,
    attack_capacity,
)


class Army(SquadJobs, Strikes, Breaches, Routes, AvoidAreas, ArmyDebug):
    """Squad jobs (`bot.squads`) and the per-unit memory that stops squads flip-flopping between
    fight and retreat. Its parts live in their own modules (`bot.jobs`, `bot.strikes`, `bot.breach`,
    `bot.routes`, `bot.avoid`, `bot.army_debug`); all the state is set up here, in `__init__`."""

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
        self.squad_scouts: dict[int, ScoutPlan] = {}  # squads scouting while waiting on an overlord
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
        # (area, the squad's unit tags) -> (built at, map, the enemy tags it was built with)
        self.reach_cache: dict[tuple, tuple[float, ReachMap, frozenset[int]]] = {}
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
