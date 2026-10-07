"""Squad jobs (`bot.squads`): where each squad goes (`squad_goal`), raids sized by the combat sim, help for losing
fights, rallies, pushes and squad scouts."""

from ares import AresBot
from sc2.position import Point2
from sc2.unit import Unit

from bot.army_notes import COUNTS, note
from bot.combat import weapon_range, wins
from bot.micro import rally_point
from bot.settings import (
    FIGHT_AT,
    GATHERED_FRACTION,
    HELP_RANGE,
    HELP_WIN_AT,
    LING_SCOUT_MIN_SECONDS,
    MAX_PATROLS_ATTACKING,
    PATROL_MIN_SQUADS,
    RAID_MIN,
    RALLY_SECONDS,
    SCOUTED_SECONDS,
    SITE_DEFENSE_RADIUS,
    SITE_RADIUS,
    SQUAD_SIZE,
    STAGING_MARGIN,
    UNDER_FIRE_SECONDS,
)
from bot.squads import (
    EnemyGroup,
    Rally,
    ScoutPlan,
    Site,
    Squad,
    center_of,
    choose_helpers,
    enemy_groups,
    enemy_sites,
    needed,
)
from bot.targeting import attack_capacity


class SquadJobs:
    """Squad job planning (part of `bot.army.Army`)."""

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
            self.squad_scouts[squad.id] = ScoutPlan(spot, ai.time + budget, ai.time, before)
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
