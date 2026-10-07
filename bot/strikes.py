"""Strikes: enemies shooting our units get dealt with by the fewest nearby zerglings that win, which gather outside
their reach and go in together."""

from collections import Counter

from ares import AresBot
from ares.consts import WORKER_TYPES, EngagementResult, UnitTreeQueryType
from loguru import logger
from sc2.unit import Unit

from bot.army_notes import COUNTS, UNIT_NOTES, note
from bot.combat import IGNORED_THREATS, armed, front_first, reach_of, strike_result, weapon_range
from bot.micro import back_off, fight
from bot.settings import (
    BACK_OFF_MARGIN,
    ENEMY_GROUP_RADIUS,
    FIGHT_AT,
    GATHERED_FRACTION,
    HELP_ROOM_FACTOR,
    HELP_WIN_AT,
    RAID_TOGETHER,
    STRIKE_GATHER_RADIUS,
    STRIKE_GATHER_SECONDS,
    STRIKE_RANGE,
    STRIKE_SECONDS,
    UNDER_FIRE_SECONDS,
)
from bot.squads import Strike, center_of, cluster
from bot.targeting import ReachMap, TargetSlots, attack_capacity


class Strikes:
    """Strikes (part of `bot.army.Army`)."""

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
