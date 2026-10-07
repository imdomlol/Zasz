"""Debug logging for the army (`Army.debug`, switched on by the scenarios): why units died, how many zerglings hit each
building, and a summary every few seconds."""

from collections import Counter

from ares import AresBot
from ares.consts import UnitTreeQueryType
from loguru import logger
from sc2.unit import Unit

from bot.army_notes import COUNTS, UNIT_NOTES, name_of
from bot.combat import ARMY_TYPES, armed, reach_of
from bot.squads import Squad


class ArmyDebug:
    """Debug logs (part of `bot.army.Army`)."""

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
