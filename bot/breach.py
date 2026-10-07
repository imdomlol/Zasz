"""Breaches: the building worth breaking to get at enemies we can't reach (`breach_caps`), or to open more sides of one
we can only reach from part of its ring (`widen_plan`)."""

import numpy as np
from ares import AresBot
from ares.consts import WORKER_TYPES
from loguru import logger
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.army_notes import COUNTS, name_of
from bot.combat import ARMY_TYPES, armed, dps_vs, enemies_near, reach_of, weapon_range, wins
from bot.settings import (
    BREACH_SWITCH_SAVING,
    BREACH_VIEW_CELL,
    BREACH_VIEW_RANGE,
    REACH_SECONDS,
    VISIBLE_THREAT_MARGIN,
    WIDEN_GIVE_UP_SECONDS,
    WIDEN_MAX_SECONDS,
    WIDEN_STILL_SECONDS,
)
from bot.squads import center_of
from bot.targeting import ReachMap, attack_capacity


class Breaches:
    """Breach and widening plans (part of `bot.army.Army`)."""

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
