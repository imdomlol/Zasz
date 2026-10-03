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

from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, AttackTarget, PathUnitToTarget, ShootTargetInRange
from ares.behaviors.combat.individual.combat_individual_behavior import CombatIndividualBehavior
from ares.consts import TOWNHALL_TYPES, WORKER_TYPES, EngagementResult, UnitRole, UnitTreeQueryType
from loguru import logger
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.settings import (
    APPROACH_DISTANCE,
    AVOID_MARGIN,
    AVOID_SECONDS,
    BACK_OFF_MARGIN,
    BREAKTHROUGH_RANGE,
    CLEAR_THE_WAY_RANGE,
    DEFEND_RADIUS,
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
    MEMORY_THREAT_MARGIN,
    MIN_OPEN_SIDES,
    PATROL_FIGHT_AT,
    PLAN_SECONDS,
    RAID_MIN,
    RALLY_SECONDS,
    REACH_SECONDS,
    SITE_DEFENSE_RADIUS,
    SITE_RADIUS,
    SQUAD_SIZE,
    STAGING_MARGIN,
    TANK_NEUTRALIZED_BY,
    TARGET_FILL,
    UNDER_FIRE_SECONDS,
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
    center_of,
    choose_helpers,
    enemy_groups,
    enemy_sites,
    needed,
    wins,
)
from bot.targeting import BlockTracker, Detours, ReachMap, TargetSlots, attack_capacity, pick_tiered

ARMY_TYPES = {UnitTypeId.ZERGLING}
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
        self.planned_at = -999.0
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
                if squad.job == "defend" or (squad.job == "raid" and not attacking):
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
            if s.job in ("raid", "help", "engage", "defend") or (s.id in self.battles and s.job != "gather")
            for t in s.tags
        }
        self.taken.clear()  # target slots, shared by every squad this step
        battles: dict[int, Battle] = {}
        for squad in squads:
            target, advancing = self.squad_goal(ai, squad, squads)
            # Not attacking, a patrol only takes fights it wins big (scouting, not looking for trouble)
            fight_at = FIGHT_AT if attacking or squad.job != "patrol" else PATROL_FIGHT_AT
            if battle := self.control_squad(ai, squad, target, advancing, fight_at):
                battles[squad.id] = battle
        self.battles = battles

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
        logger.info(
            f"{ai.time_formatted} army debug: {len(squads)} squads ({mode}), {lings} zerglings vs {enemy} | {summary}"
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
                # Scouting, not fighting: stay out of reach of every known enemy army
                return self.blocked(ai, p) or any(
                    p.distance_to(g.center) < g.spread + g.reach + STAGING_MARGIN for g in groups
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
        sites = [s for s in enemy_sites(ai, structures, groups) if not self.blocked(ai, s.center)]
        # (Nothing known at their main is a patrol spot, not a raid: walking a raid into the unknown
        # loses zerglings to whatever sits there, e.g. a sieged tank)
        main = ai.enemy_start_locations[0]
        sites.sort(key=lambda s: -s.center.distance_to(main))

        # Sites move a little as structures die or get found (the center shifts): a raid follows its
        # site to the nearest one still there
        def same_site(spot: Point2) -> Site | None:
            near = [s for s in sites if s.center.distance_to(spot) < SITE_RADIUS]
            return min(near, key=lambda s: s.center.distance_to(spot)) if near else None

        for squad in squads:
            if squad.job == "raid":
                if (site := same_site(squad.site)) is None:
                    COUNTS["raid dropped: site gone"] += 1
                    squad.free()
                else:
                    squad.site = site.center
        raiding = [s.site for s in squads if s.job == "raid"]
        # Everyone who gathered at a site is gone: gather again first
        self.gathered = {
            site.center for g in self.gathered if (site := same_site(g)) is not None and site.center in raiding
        }
        free = [s for s in squads if s.job in ("patrol", "raid") and s.id not in self.battles]
        for site in sites:
            on_it = [s for s in free if s.job == "raid" and s.site == site.center]
            if any(b.center.distance_to(site.center) < SITE_DEFENSE_RADIUS for b in self.battles.values()):
                continue  # a fight is on there: reinforcing it is for the help logic, not one squad at a time
            others = sorted(
                (s for s in free if s.job == "patrol"), key=lambda s: s.squad_position.distance_to(site.center)
            )
            pool = [u for s in on_it + others for u in s.units]
            if not pool:
                break
            if site.defenders:
                # Enough to win comfortably (fewer just-win zerglings means more of them lost), or at
                # least to win
                demand = needed(ai, pool, site.defenders, site.center, HELP_WIN_AT) or needed(
                    ai, pool, site.defenders, site.center
                )
                if demand is None:
                    self.turned_back(ai, site.defenders)
                    for s in on_it:
                        s.free()
                    continue
            elif site.structures:
                # Undefended: as many as fit around the structures
                demand = max(RAID_MIN, sum(attack_capacity(pool[0], s) or SQUAD_SIZE for s in site.structures))
            else:
                demand = RAID_MIN
            staffed = sum(len(s.tags) for s in on_it)
            for s in others:
                if staffed >= demand:
                    break
                s.job, s.site, s.helping = "raid", site.center, None
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
                    self.turned_back(ai, battle.threats)
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
            reach = max(t.ground_range + t.radius for t in battle.threats) + max(
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

    def avoid_armies_we_cannot_beat(self, ai: AresBot, lings: list[Unit]) -> None:
        """Mark every known enemy group (seen now or remembered) the whole army can't beat as an
        area to avoid, before walking into it rather than after losing half the zerglings."""
        for group in enemy_groups(ai):
            if not wins(ai, lings, group.units):
                COUNTS[f"avoid group of {len(group.units)}"] += 1
                self.turned_back(ai, group.units)

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

    def turned_back(self, ai: AresBot, threats: list[Unit]) -> None:
        """Remember where a squad had to back off: the area those enemies cover (their spread plus
        weapon range), replacing an older entry for the same spot."""
        center = center_of(threats)
        spread = max(t.position.distance_to(center) for t in threats)
        radius = spread + max(t.ground_range + t.radius for t in threats) + AVOID_MARGIN
        self.turned_back_at = [(p, r, t) for p, r, t in self.turned_back_at if p.distance_to(center) > radius / 2]
        self.turned_back_at.append((center, radius, ai.time + AVOID_SECONDS))

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
        threats = [e for e in enemies if (not e.is_structure or e.can_attack_ground) and can_reach_soon(e, units)]
        under_fire = any(ai.time - self.last_damaged.get(u.tag, -99) < UNDER_FIRE_SECONDS for u in units)

        if threats:
            near_home = ai.townhalls.closest_distance_to(squad.squad_position) < DEFEND_RADIUS
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
            # Fight while the sim predicts at least an even fight; any predicted loss means
            # backing off, so we never keep feeding units into a losing fight
            if near_home or self.committed or result >= fight_at:
                reach = self.reach_map(ai, units, enemies)
                slots = TargetSlots(reach, self.breach_caps(ai, reach, units, enemies, threats, present), self.taken)
                for unit in front_first(units, enemies):
                    fight(ai, unit, threats, enemies, slots, squad.squad_position, self.blocks, self.detours)
            else:
                # Just out of their reach, not home: from there it waits for help or patrols on
                # (and isn't an avoid area: one squad losing isn't the army losing)
                COUNTS[f"back off ({result.name})"] += len(units)
                back_off(ai, units, threats, None)
            return battle

        if under_fire and not advancing:
            # Shot by something we can't see, and not attacking: step back the way we came
            back = squad.squad_position.towards(rally_point(ai), UNSEEN_FIRE_STEP)
            for unit in units:
                move(ai, unit, back)
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
        fighters = [e for e in threats if not e.is_structure and e.type_id not in WORKER_TYPES]
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
                if f.can_attack_ground
                and f.position.distance_to(s.position) - s.radius - f.radius < f.ground_range + 1.5
            ]
            incoming = sum(f.calculate_dps_vs_target(probe) for f in covering)
            lost = incoming * seconds / max(probe.health_max, 1)
            survivors = ours[: max(0, len(ours) - int(lost + 0.5))]
            COUNTS[f"breach considered (lose ~{lost:.0f})"] += 1
            if wins(ai, survivors, fighters) and (best is None or lost < best[0]):
                best = (lost, tag, attackers)
        if best is not None:
            reach.breach_caps = {best[1]: best[2]}
        return reach.breach_caps

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


def can_reach_soon(enemy: Unit, units: list[Unit]) -> bool:
    """Within the enemy's weapon range plus a margin of one of these units: a margin for visible
    enemies (they can step in), a smaller one for remembered ones (last seen in the fog). Enemies
    farther away don't turn a squad back, so going *around* an army at a safe distance works."""
    margin = MEMORY_THREAT_MARGIN if enemy.is_memory else VISIBLE_THREAT_MARGIN
    reach = enemy.ground_range + enemy.radius + margin
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
    return ai.mediator.can_win_fight(own_units=own, enemy_units=threats, workers_do_no_damage=True), own


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
    near_fighters = [e for e in fighters if e.position.distance_to(position) < FIGHTER_PRIORITY_RANGE]
    if slots.reach is not None:
        near_fighters = [
            e
            for e in near_fighters
            if e.position.distance_to(position) - e.radius - unit.radius < 0.75
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

        pinned = [e for e in near_fighters if touching(e) and not surroundable(e) and e.ground_range <= 1]
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


def reach_of(threat: Unit, unit: Unit) -> float:
    """How close `unit` can be to `threat` before it can be hit (edge to edge plus weapon range)."""
    return threat.ground_range + threat.radius + unit.radius


def back_off(ai: AresBot, units: list[Unit], threats: list[Unit], hold_at: Point2 | None) -> None:
    """Get out of the threats' actual weapon range (not ares' danger map, which pads every range by
    4 and sent zerglings all the way home), then stop: at `hold_at` if given (a rally), else right
    there. Units already out of reach wait."""
    shooters = [t for t in threats if t.can_attack_ground]
    center = center_of(threats)
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
