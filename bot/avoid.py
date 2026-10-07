"""Avoid areas: where squads were turned back, or that the whole army can't beat, or where something fires from the
fog. Patrols, raids and routes keep out of them until they expire or are seen empty."""

import math

from ares import AresBot
from ares.consts import WORKER_TYPES, UnitTreeQueryType
from sc2.position import Point2
from sc2.unit import Unit

from bot.army_notes import COUNTS
from bot.combat import armed, weapon_range, wins
from bot.settings import AVOID_MARGIN, AVOID_SECONDS, PARTLY_SEEN_MARGIN, PARTLY_SEEN_RANGE, PARTLY_SEEN_SECONDS
from bot.squads import center_of, enemy_groups


class AvoidAreas:
    """Avoid areas (part of `bot.army.Army`)."""

    def avoid_armies_we_cannot_beat(self, ai: AresBot, lings: list[Unit]) -> None:
        """Mark every known enemy group (seen now or remembered) the whole army can't beat as an
        area to avoid, before walking into it rather than after losing half the zerglings."""
        for group in enemy_groups(ai):
            if not wins(ai, lings, group.units):
                COUNTS[f"avoid group of {len(group.units)}"] += 1
                self.turned_back(ai, group.units, "army can't beat")

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
