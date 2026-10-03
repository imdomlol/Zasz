"""Overlord scouting: watch the enemy's expansions without losing the overlords.

The first OVERLORD_SCOUTS overlords each take a post next to one of the enemy's expansion spots
(natural, third, fourth, ...), so we see new bases go down and armies move out. Overlords:

- travel along ares' air grid, avoiding known anti-air
- wait SCOUT_POST_OFFSET away from the expansion, on our side, rather than on top of it
- step away whenever they're inside known anti-air range (ares' `KeepUnitSafe`)
- abandon their post when shot (even by something unseen); that post is skipped for a while
  and the overlord goes home, and another overlord takes the next post

The army can also ask for vision of a spot (`request`, e.g. a sieged tank that fired at us from
the fog): the nearest free overlord flies over it (around known anti-air) until we see it; one that
gets shot on the way gives up, and the next one tries.

Every other overlord just keeps itself safe near our bases.
"""

from ares import AresBot
from ares.behaviors.combat.individual import KeepUnitSafe, PathUnitToTarget
from ares.consts import UnitRole
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.settings import OVERLORD_SCOUTS, SCOUT_POST_COOLDOWN, SCOUT_POST_OFFSET, SCOUT_REQUEST_SECONDS

OVERLORD_TYPES = {UnitTypeId.OVERLORD, UnitTypeId.OVERLORDTRANSPORT}


class Scouting:
    def __init__(self) -> None:
        self.posts: dict[int, Point2] = {}  # overlord tag -> its post
        self.avoid_until: dict[Point2, float] = {}  # post -> skip until this time (got shot there)
        self.requests: dict[Point2, float] = {}  # spots the army wants seen -> when asked
        self.sent: dict[int, Point2] = {}  # overlord tag -> requested spot it's flying to

    def request(self, ai: AresBot, spot: Point2) -> None:
        """Ask for vision of `spot` (ignored if one nearby is already asked for)."""
        if not any(spot.distance_to(s) < 6 for s in self.requests):
            self.requests[spot] = ai.time

    def on_damage(self, ai: AresBot, unit: Unit) -> None:
        if unit.tag in self.sent:
            self.sent.pop(unit.tag)  # shot on the way: give up, another one tries
            ai.mediator.assign_role(tag=unit.tag, role=UnitRole.IDLE)
        if unit.tag in self.posts:
            post = self.posts.pop(unit.tag)
            self.avoid_until[post] = ai.time + SCOUT_POST_COOLDOWN
            ai.mediator.assign_role(tag=unit.tag, role=UnitRole.IDLE)

    def control(self, ai: AresBot) -> None:
        overlords = [u for u in ai.units if u.type_id in OVERLORD_TYPES]
        alive = {u.tag for u in overlords}
        self.posts = {tag: post for tag, post in self.posts.items() if tag in alive}
        self.assign_requests(ai, overlords, alive)
        self.assign_posts(ai, overlords)

        air_grid = ai.mediator.get_air_grid
        for overlord in overlords:
            # Safety first: step out of known anti-air range
            if not ai.mediator.is_position_safe(grid=air_grid, position=overlord.position):
                ai.register_behavior(KeepUnitSafe(unit=overlord, grid=air_grid))
            elif overlord.tag in self.sent:
                if overlord.distance_to(self.sent[overlord.tag]) > 1:
                    ai.register_behavior(PathUnitToTarget(unit=overlord, grid=air_grid, target=self.sent[overlord.tag]))
            elif overlord.tag in self.posts:
                if overlord.distance_to(self.posts[overlord.tag]) > 1:
                    ai.register_behavior(
                        PathUnitToTarget(unit=overlord, grid=air_grid, target=self.posts[overlord.tag])
                    )
            elif ai.townhalls and ai.townhalls.closest_distance_to(overlord) > 15 and overlord.is_idle:
                # An ex-scout or stray: head back over our bases
                home = ai.townhalls.closest_to(overlord).position
                ai.register_behavior(PathUnitToTarget(unit=overlord, grid=air_grid, target=home))

    def assign_requests(self, ai: AresBot, overlords: list[Unit], alive: set[int]) -> None:
        """Requested spots: done once we can see them (or after SCOUT_REQUEST_SECONDS); each open one
        gets the nearest healthy overlord that isn't on a post or another request."""
        self.requests = {
            s: t for s, t in self.requests.items() if not ai.is_visible(s) and ai.time - t < SCOUT_REQUEST_SECONDS
        }
        self.sent = {tag: s for tag, s in self.sent.items() if tag in alive and s in self.requests}
        for spot in self.requests:
            if spot in self.sent.values():
                continue
            free = [
                u for u in overlords if u.tag not in self.posts and u.tag not in self.sent and u.health_percentage == 1
            ]
            if not free:
                break
            overlord = min(free, key=lambda u: u.distance_to(spot))
            self.sent[overlord.tag] = spot
            ai.mediator.assign_role(tag=overlord.tag, role=UnitRole.SCOUTING)

    def assign_posts(self, ai: AresBot, overlords: list[Unit]) -> None:
        open_posts = [p for p in self.scout_posts(ai) if p not in self.posts.values()]
        free = [u for u in overlords if u.tag not in self.posts and u.tag not in self.sent and u.health_percentage == 1]
        while open_posts and free and len(self.posts) < OVERLORD_SCOUTS:
            post = open_posts.pop(0)
            overlord = min(free, key=lambda u: u.distance_to(post))
            free.remove(overlord)
            self.posts[overlord.tag] = post
            ai.mediator.assign_role(tag=overlord.tag, role=UnitRole.SCOUTING)

    def scout_posts(self, ai: AresBot) -> list[Point2]:
        """Spots next to the enemy's likely expansions, closest to their main first.

        Skips their main (too much anti-air) and posts we recently got shot at."""
        posts = []
        for location, _distance in ai.mediator.get_enemy_expansions[1 : OVERLORD_SCOUTS + 1]:
            post = location.towards(ai.start_location, SCOUT_POST_OFFSET)
            if self.avoid_until.get(post, 0) <= ai.time:
                posts.append(post)
        return posts
