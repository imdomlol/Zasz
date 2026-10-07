"""Where squads can go: routes around danger (per set of enemy groups a squad can't beat), walkable regions, and the
reach maps of the fights they're in."""

import time

import numpy as np
from ares import AresBot
from ares.consts import WORKER_TYPES, UnitTreeQueryType
from sc2.position import Point2
from sc2.unit import Unit
from scipy.ndimage import label

from bot.combat import armed, weapon_range, wins
from bot.settings import BACK_OFF_MARGIN, PLAN_SECONDS, REACH_SECONDS, REGIONS_SECONDS, STAGING_MARGIN
from bot.squads import Site, center_of, enemy_groups
from bot.targeting import ReachMap, RouteMap


class Routes:
    """Routes and reach maps (part of `bot.army.Army`)."""

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

    def can_walk_to(self, ai: AresBot, spot: Point2, near: int = 3) -> bool:
        """Is `spot` (or ground within `near` of it) in a walkable region our units are in? Minerals,
        rocks and buildings are walls: a base behind a mineral wall isn't, until it's mined out."""
        if ai.time - self.regions_at >= REGIONS_SECONDS or self.regions is None:
            self.regions_at = ai.time
            walkable = np.isfinite(ai.mediator.get_ground_grid)
            labels, _ = label(walkable, structure=np.ones((3, 3), dtype=bool))
            ours = {int(labels[int(u.position.x), int(u.position.y)]) for u in ai.units if not u.is_flying} - {0}
            self.regions = (labels, ours)
        labels, ours = self.regions
        w, h = labels.shape
        for dx in range(-near, near + 1):
            for dy in range(-near, near + 1):
                x, y = int(spot.x) + dx, int(spot.y) + dy
                if 0 <= x < w and 0 <= y < h and labels[x, y] in ours:
                    return True
        return False

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

    def reach_map(self, ai: AresBot, units: list[Unit], enemies: list[Unit]) -> ReachMap | None:
        """Which enemy units this squad can actually get to; rebuilt every REACH_SECONDS per area and squad
        (the map is reached from these units: another squad in the area may be on the other side of a
        wall), or sooner if an enemy it doesn't cover turns up. Enemies that died since stay in it (as
        obstacles) until then."""
        ground = [e for e in enemies if not e.is_flying]
        if not ground:
            return None
        center = center_of(units)
        key = (round(center.x / 10), round(center.y / 10), frozenset(u.tag for u in units))
        tags = frozenset(e.tag for e in ground)
        cached = self.reach_cache.get(key)
        if cached is None or ai.time - cached[0] >= REACH_SECONDS or not tags <= cached[2]:
            built = ReachMap(ai.mediator.get_ground_grid, units, ground, ai.get_terrain_z_height)
            cached = (ai.time, built, tags)
            self.reach_cache[key] = cached
        return cached[1]
