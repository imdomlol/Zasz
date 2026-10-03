"""Debug overlay: draws what the army is doing in the game window, for watching local games.

Off by default (nothing is drawn on the ladder); `ZASZ_OVERLAY=1` turns it on, as do the `--overlay`
flags of `scripts/benchmark.py` and `scripts/run_scenarios.py`. It only reads the army's state:
turning it on doesn't change a single decision.

- squads: a circle around each, colored by job, labelled `S3 raid A 8` (and its fight's sim verdict
  and the room left around the targets), with a line to where it's headed
- raids: each site's letter, zerglings assigned / needed, and whether its raiders are still gathering
- strikes: a ring around the enemy group, a box on the gathering spot, `strike 10: gathering`/`in`
- breach: the structure being broken and its estimated cost in zerglings
- avoid areas: circles with the reason and the seconds left
- backing off: a circle around each enemy at the distance squads back off to (its reach + margin)
- scout requests: a purple box where the army asked an overlord for vision
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from sc2.position import Point2, Point3

if TYPE_CHECKING:
    from ares import AresBot

    from bot.army import Army


def enabled() -> bool:
    """Read when drawing, not at import: the scripts set it after the bot is imported."""
    return os.environ.get("ZASZ_OVERLAY") == "1"


JOB_COLORS = {
    "patrol": (90, 170, 255),
    "raid": (255, 70, 70),
    "push": (255, 150, 0),
    "help": (255, 230, 0),
    "gather": (255, 190, 120),
    "engage": (255, 0, 255),
    "defend": (255, 255, 255),
}
RAID_COLOR = (255, 90, 90)
STRIKE_COLOR = (0, 255, 160)
BREACH_COLOR = (255, 255, 0)
AVOID_COLOR = (150, 150, 150)
BACK_OFF_COLOR = (0, 200, 255)
SCOUT_COLOR = (180, 90, 255)


def lifted(ai: AresBot, p: Point2, lift: float = 0.3) -> Point3:
    return Point3((p.x, p.y, ai.get_terrain_z_height(p) + lift))


def draw(ai: AresBot, army: Army) -> None:
    client = ai.client
    for squad in army.book.squads.values():
        units = [u for u in ai.units if u.tag in squad.tags]
        if not units:
            continue
        center = Point2((sum(u.position.x for u in units) / len(units), sum(u.position.y for u in units) / len(units)))
        radius = max(1.5, max(u.position.distance_to(center) for u in units) + 0.5)
        color = JOB_COLORS.get(squad.job, (200, 200, 200))
        client.debug_sphere_out(lifted(ai, center), radius, color)
        job = squad.job
        if job == "raid" and squad.site in army.site_letters:
            job = f"raid {army.site_letters[squad.site]}"
        label = f"S{squad.id} {job} {len(units)}"
        if battle := army.battles.get(squad.id):
            label += f" | {battle.result.name} room {battle.room}"
        client.debug_text_world(label, lifted(ai, center, 1.5), color, 12)
        if squad.target is not None:
            client.debug_line_out(lifted(ai, center), lifted(ai, squad.target), color)
    for center, text in army.site_notes:
        client.debug_box2_out(lifted(ai, center, 0.5), 1.0, RAID_COLOR)
        client.debug_text_world(text, lifted(ai, center, 2.5), RAID_COLOR, 14)
    enemies = {e.tag: e for e in [*ai.enemy_units, *ai.enemy_structures]}
    for strike in army.strikes:
        group = [enemies[t] for t in strike.targets if t in enemies]
        if not group:
            continue
        center = Point2((sum(e.position.x for e in group) / len(group), sum(e.position.y for e in group) / len(group)))
        radius = max(e.position.distance_to(center) for e in group) + 1.5
        client.debug_sphere_out(lifted(ai, center), radius, STRIKE_COLOR)
        state = "in" if strike.go else "gathering"
        client.debug_text_world(f"strike {len(strike.lings)}: {state}", lifted(ai, center, 2.0), STRIKE_COLOR, 14)
        if strike.spot is not None:
            client.debug_box2_out(lifted(ai, strike.spot, 0.5), 0.6, STRIKE_COLOR)
    for position, text, when in army.breach_notes.values():
        if ai.time - when < 3:
            client.debug_sphere_out(lifted(ai, position), 2.2, BREACH_COLOR)
            client.debug_text_world(text, lifted(ai, position, 2.0), BREACH_COLOR, 12)
    from bot.army import BACK_OFF_CIRCLES

    if scouting := getattr(ai, "scouting", None):
        for spot in scouting.requests:
            client.debug_box2_out(lifted(ai, spot, 0.5), 1.5, SCOUT_COLOR)
            client.debug_text_world("scout requested", lifted(ai, spot, 2.0), SCOUT_COLOR, 12)

    for position, radius in set(BACK_OFF_CIRCLES):
        client.debug_sphere_out(lifted(ai, position), radius, BACK_OFF_COLOR)
    for center, radius, until in army.turned_back_at:
        if until > ai.time:
            client.debug_sphere_out(lifted(ai, center), radius, AVOID_COLOR)
            reason = army.avoid_reasons.get(center, "")
            client.debug_text_world(
                f"avoid {until - ai.time:.0f}s ({reason})", lifted(ai, center, 1.0), AVOID_COLOR, 12
            )
