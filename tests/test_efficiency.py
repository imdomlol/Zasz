"""Offline tests for damage efficiency (`bot.efficiency`) and target spreading (`bot.targeting`).

Units are stand-ins with just the attributes the code reads, so no game is needed.
"""

from types import SimpleNamespace

import numpy as np
from sc2.position import Point2

from bot.efficiency import COMMIT_SECONDS, DamageTracker, EfficiencyTotals
from bot.targeting import ReachMap, TargetSlots, attack_capacity, melee_width

_tags = iter(range(1, 10_000))


def unit(x=0.0, y=0.0, *, radius=0.375, ground_range=0.1, air_range=0.0, flying=False, structure=False, **extra):
    return SimpleNamespace(
        tag=next(_tags),
        position=Point2((x, y)),
        radius=radius,
        ground_range=ground_range,
        air_range=air_range,
        is_flying=flying,
        is_structure=structure,
        order_target=None,
        **extra,
    )


def zergling(x=0.0, y=0.0, **extra):
    return unit(x, y, radius=0.375, ground_range=0.1, **extra)


def mutalisk(x=0.0, y=0.0, **extra):
    return unit(x, y, radius=0.5, ground_range=3.0, air_range=3.0, flying=True, **extra)


def bay(x=0.0, y=0.0):
    return unit(x, y, radius=1.75, ground_range=0.0, structure=True)


def ultralisk(x=0.0, y=0.0, **extra):
    return unit(x, y, radius=1.0, ground_range=1.0, **extra)


# --- targeting ---


def test_melee_vs_structure_is_capped_by_space():
    capacity = attack_capacity(zergling(), bay())
    assert capacity is not None and 3 <= capacity <= 14


def test_no_cap_for_ranged_or_air_attackers():
    assert attack_capacity(mutalisk(), bay()) is None  # ranged/air: not limited by space
    assert attack_capacity(mutalisk(), zergling(1, 0)) is None


def test_melee_vs_unit_is_about_a_full_ring():
    vs_unit = attack_capacity(zergling(), zergling(1, 0))
    assert vs_unit is not None and 4 <= vs_unit <= 8


def test_fill_from_the_back():
    """The first attackers take the deepest structure; once it's full the next one is used."""
    front, back = bay(5, 0), bay(10, 0)
    slots = TargetSlots()
    capacity = attack_capacity(zergling(), back)
    picks = [slots.pick(zergling(0, 0), [front, back], depth_from=Point2((0, 0))) for _ in range(capacity + 2)]
    assert picks[:capacity] == [back] * capacity
    assert picks[capacity:] == [front, front]


def test_nearest_fill_without_depth():
    front, back = bay(5, 0), bay(10, 0)
    assert TargetSlots().pick(zergling(0, 0), [front, back]) is front


def test_keeps_current_target_while_it_has_room():
    front, back = bay(5, 0), bay(10, 0)
    attacker = zergling(0, 0)
    attacker.order_target = front.tag
    assert TargetSlots().pick(attacker, [front, back], depth_from=Point2((0, 0))) is front


def test_everything_full_falls_back_to_closest():
    target = bay(5, 0)
    slots = TargetSlots()
    for _ in range(attack_capacity(zergling(), target)):
        slots.take(target, zergling())
    assert slots.pick(zergling(0, 0), [target]) is target


def test_attackers_of_different_sizes_share_a_target_by_angle():
    # A bay in the open: one room left for a zergling is no room for an ultralisk, which stands further
    # out and takes more of the way round
    target = bay(5, 0)
    slots = TargetSlots()
    assert slots.has_room(ultralisk(0, 0), target)
    for _ in range(attack_capacity(zergling(), target) - 1):
        slots.take(target, zergling())
    assert slots.has_room(zergling(0, 0), target)
    assert not slots.has_room(ultralisk(0, 0), target)


def wall_with_a_bunker():
    """The wall_bunker scenario's weak side: a row of 3x3 buildings (tiles x 10..21, y 19..21) with the
    bunker second from the left, sealed ground behind it (y < 19), open ground in front (y >= 22)."""
    grid = np.full((40, 40), np.inf, dtype=np.float32)
    grid[:, 22:] = 1.0
    bunker = unit(14.5, 20.5, radius=1.8125, ground_range=6.0, structure=True, is_memory=False)
    others = [unit(x, 20.5, radius=1.75, structure=True, is_memory=False) for x in (11.5, 17.5, 20.5)]
    ling = zergling(14.5, 30.5)
    return ReachMap(grid, [ling], [bunker, *others], margin=20), bunker, ling


def test_attack_spots_fill_a_buildings_open_face_not_one_per_tile():
    reach, bunker, ling = wall_with_a_bunker()
    spots = reach.attack_spots(bunker, ling.radius, ling.ground_range)
    # Only on the open face, a zergling width (with spacing) apart; more than the 3 tiles along it
    assert all(p.y >= 22 for p in spots)
    assert all(a.distance_to(b) >= melee_width(ling.radius) * 0.95 for i, a in enumerate(spots) for b in spots[i + 1 :])
    assert 4 <= len(spots) <= 6
    assert reach.attack_slots(bunker, ling.radius, 99, ling.ground_range) == len(spots)
    # Bigger attackers get fewer spots
    big = reach.attack_spots(bunker, 1.0, 1.0)
    assert 1 <= len(big) < len(spots)


def test_attack_spots_go_all_the_way_round_a_building_in_the_open():
    grid = np.ones((30, 30), dtype=np.float32)
    grid[14:17, 14:17] = np.inf
    building = unit(15.5, 15.5, radius=1.75, structure=True)  # an engineering bay's radius
    ling = zergling(5.5, 5.5)
    reach = ReachMap(grid, [ling], [building], margin=20)
    spots = reach.attack_spots(building, ling.radius, ling.ground_range)
    ring = 2 * np.pi * (building.radius + ling.radius + 0.05)
    assert len(spots) == int(ring // melee_width(ling.radius))
    # The last spot doesn't crowd the first, round the back
    assert spots[0].distance_to(spots[-1]) >= melee_width(ling.radius) * 0.95


# --- efficiency ---


def test_approach_goes_around_a_plugged_lane():
    # A wall across y=10 from x=3 on, with a 1-tile gap at x=10 that a zealot plugs; open around
    # the left end. Pathing (which ignores units) would go through the gap; we go around.
    grid = np.ones((20, 20), dtype=np.float32)
    grid[3:, 10] = np.inf
    grid[10, 10] = 1.0
    zealot = unit(10.5, 10.5, radius=0.5)
    stalker = unit(10.5, 15.5, radius=0.625)
    ling = zergling(10.5, 5.5)
    reach = ReachMap(grid, [ling], [zealot, stalker], margin=20)
    step = reach.approach_step(stalker, ling.position)
    assert step is not None and step.x < ling.position.x  # heading for the open end on the left
    assert reach.approach_step(stalker, Point2((10.5, 14.5))) is None  # already next to it


def test_no_detour_in_the_open():
    # Nothing between the zergling and the marine: a plain attack order is the way in
    grid = np.ones((30, 30), dtype=np.float32)
    marine = unit(20.5, 15.5, ground_range=5.0)
    ling = zergling(12.5, 15.5)
    reach = ReachMap(grid, [ling], [marine], margin=20)
    assert reach.approach_step(marine, ling.position) is None


def test_walk_distance_open_vs_plugged_lane():
    # Open field: about the straight-line distance to a spot next to the marine
    grid = np.ones((30, 30), dtype=np.float32)
    marine = unit(20.5, 15.5, ground_range=5.0)
    ling = zergling(12.5, 15.5)
    reach = ReachMap(grid, [ling], [marine], margin=20)
    assert 5 <= reach.walk_distance(marine, ling.position, limit=10) <= 8
    # Behind a wall whose only gap a zealot plugs: a long way round, so out of a 10-tile walk
    grid = np.ones((20, 20), dtype=np.float32)
    grid[3:, 10] = np.inf
    grid[10, 10] = 1.0
    zealot = unit(10.5, 10.5, radius=0.5)
    stalker = unit(10.5, 15.5, radius=0.625)
    ling = zergling(10.5, 5.5)
    reach = ReachMap(grid, [ling], [zealot, stalker], margin=20)
    assert reach.walk_distance(stalker, ling.position, limit=10) == float("inf")
    assert reach.walk_distance(stalker, ling.position, limit=40) > 10


def test_totals_efficiency_and_summary():
    totals = EfficiencyTotals(potential=100, delivered=60, blocked=30, idle=10, damage_dealt=500, elapsed=10)
    assert totals.efficiency == 0.6
    assert totals.share(totals.blocked) == 0.3
    assert totals.dealt_per_second == 50
    assert "60%" in totals.summary()
    assert EfficiencyTotals().efficiency is None


def fighter(x, *, cooldown, ground_range=0.1, radius=0.375, speed=4.0, dps=10.0):
    u = unit(x, 0, radius=radius, ground_range=ground_range)
    u.weapon_cooldown = cooldown
    u.real_speed = speed
    u.calculate_dps_vs_target = lambda target: dps
    return u


def step_tracker(tracker, attacker, target, times):
    for t in times:
        tracker._measure(attacker, target, t, times[1] - times[0])


def test_committed_unit_in_range_and_swinging_is_delivered():
    tracker = DamageTracker({None})
    target = bay(0.375 + 1.75 + 0.05, 0)  # touching
    attacker = fighter(0, cooldown=5)
    attacker.order_target = target.tag
    step_tracker(tracker, attacker, target, [0.0, 0.5, COMMIT_SECONDS + 0.5, COMMIT_SECONDS + 1.0])
    assert tracker.total.potential > 0
    assert tracker.total.delivered == tracker.total.potential


def test_stuck_out_of_range_is_blocked_but_travelling_is_not():
    tracker = DamageTracker({None})
    target = bay(10, 0)
    stuck = fighter(0, cooldown=0)
    step_tracker(tracker, stuck, target, [0.0, 0.5, COMMIT_SECONDS + 0.5, COMMIT_SECONDS + 1.0])
    assert tracker.total.blocked > 0 and tracker.total.delivered == 0

    tracker = DamageTracker({None})
    walker = fighter(0, cooldown=0)
    for i, t in enumerate([0.0, 0.5, 1.0, 1.5, 2.0]):
        walker.position = Point2((i * 1.5, 0))  # closing in at 3/s, most of its speed
        tracker._measure(walker, target, t, 0.5)
    assert tracker.total.potential == 0


def test_mutalisk_in_range_counts_like_any_unit():
    """Ranged units use their own range: 3 away from the target edge is in range for a mutalisk."""
    tracker = DamageTracker({None})
    target = bay(0.5 + 1.75 + 2.5, 0)
    muta = fighter(0, cooldown=3, ground_range=3.0, radius=0.5)
    step_tracker(tracker, muta, target, [0.0, 0.5, COMMIT_SECONDS + 0.5, COMMIT_SECONDS + 1.0])
    assert tracker.total.delivered > 0 and tracker.total.blocked == 0


def test_chasing_a_target_moving_sideways_is_not_blocked():
    """A zergling that can't catch a unit running across in front of it is chasing, not stuck."""
    tracker = DamageTracker({None})
    target = zergling(5, 0)
    chaser = fighter(0, cooldown=0)
    for i, t in enumerate([0.0, 0.5, 1.0, 1.5, 2.0]):
        target.position = Point2((5, i * 2.0))  # moving sideways at 4/s; distance barely changes
        chaser.position = Point2((0, i * 2.0))
        tracker._measure(chaser, target, t, 0.5)
    assert tracker.total.blocked == 0


# --- blocking ---


def test_blocked_when_not_getting_closer_out_of_range():
    from bot.targeting import BlockTracker

    tracker = BlockTracker()
    attacker, target = zergling(0, 0), bay(10, 0)
    assert not tracker.update(attacker, target, 0.0)
    assert not tracker.update(attacker, target, 1.0)  # stuck, but not long enough yet
    assert tracker.update(attacker, target, 2.0)  # stuck 2 s > BLOCKED_SECONDS
    assert tracker.is_blocked(attacker, target, 3.0)
    assert tracker.reachable(attacker, [target], 3.0) == []


def test_chasing_a_moving_target_is_not_blocked():
    from bot.targeting import BlockTracker

    tracker = BlockTracker()
    attacker, target = zergling(0, 0), zergling(5, 0)
    for i, t in enumerate([0.0, 1.0, 2.0, 3.0, 4.0]):
        # Both run the same way at the same speed: no closer, but the target keeps moving
        attacker.position, target.position = Point2((i * 3.0, 0)), Point2((5 + i * 3.0, 0))
        assert not tracker.update(attacker, target, t)


def test_not_blocked_while_closing_in_or_in_range():
    from bot.targeting import BlockTracker

    tracker = BlockTracker()
    target = bay(10, 0)
    attacker = zergling(0, 0)
    for i, t in enumerate([0.0, 1.0, 2.0, 3.0]):
        attacker.position = Point2((i * 2.0, 0))  # closing in
        assert not tracker.update(attacker, target, t)
    attacker.position = Point2((10 - 1.75 - 0.375, 0))  # touching: in range
    for t in (4.0, 6.0, 8.0):
        assert not tracker.update(attacker, target, t)


def test_breach_is_the_building_between_us_and_sealed_off_enemies():
    # A wall of two 3x3 buildings across x=8..13 (y=9..11) closing the corridor x=8..13 between
    # cliffs; a stalker sealed in above it. Only buildings touching both sides are breaches.
    grid = np.full((20, 24), np.inf, dtype=np.float32)
    grid[8:14, 0:24] = 1.0  # a corridor 6 wide
    grid[8:14, 9:12] = np.inf  # the wall: two buildings
    left = unit(9.5, 10.5, radius=1.5, structure=True)
    right = unit(12.5, 10.5, radius=1.5, structure=True)
    elsewhere = unit(10.5, 2.5, radius=1.5, structure=True)  # on our side, not in the wall
    grid[9:12, 1:4] = np.inf
    stalker = unit(10.5, 18.5, radius=0.625, is_memory=False)
    ling = zergling(10.5, 6.5)
    reach = ReachMap(grid, [ling], [stalker], margin=20)
    assert reach.open_sides(stalker) == 0
    found = reach.breaches([left, right, elsewhere], [stalker])
    assert set(found) == {left.tag, right.tag}
    slots, opened = found[left.tag]
    assert opened == [stalker] and 1 <= slots <= 5
    # The spots attackers hit it from are on our side of the wall (below it), one per attack slot:
    # enemy range is measured to these, not to the building
    spots = reach.attack_cells[left.tag]
    assert len(spots) == slots
    assert all(p.y < 9 for p in spots)


def test_walk_to_a_group_and_the_gather_spot_outside_its_reach():
    # Open ground; two marines (reach 5 + radius + margin ~ 6.4) at x = 20; a zergling at x = 2
    grid = np.ones((40, 30), dtype=np.float32)
    marines = [unit(20.5, 14.5, ground_range=5.0, is_memory=False), unit(20.5, 16.5, ground_range=5.0, is_memory=False)]
    ling = zergling(2.5, 15.5)
    reach = ReachMap(grid, [ling], marines, margin=20)
    walk = reach.walk_to(marines, ling.position)
    assert 14 <= walk <= 18
    spot = reach.gather_spot(marines, [(m.position, 6.4) for m in marines])
    assert spot is not None
    assert all(spot.distance_to(m.position) > 6.4 for m in marines)  # outside their reach...
    assert spot.distance_to(marines[0].position) < 9  # ...but right at its edge, on our side
    assert spot.x < 20.5


def test_cornered_when_no_reachable_ground_is_out_of_reach():
    # A short dead end; a marine (reach 6.4) near its mouth covers all of it
    grid = np.full((30, 30), np.inf, dtype=np.float32)
    grid[10:13, 7:20] = 1.0  # a corridor, closed at y = 7 (the dead end) and open toward y = 19
    grid[5:25, 19:29] = 1.0  # the open area beyond
    marines = [unit(11.5, 12.5, ground_range=5.0, is_memory=False)]
    ling = zergling(11.5, 8.5)
    reach = ReachMap(grid, [ling], [], margin=20)
    covered = [(m.position, 6.4) for m in marines]
    assert reach.safe_ground(covered)  # the open area past the marine is reachable and out of reach...
    blocker = [unit(11.5, 14.5, radius=1.6, is_memory=False)]  # ...until something blocks the corridor
    reach = ReachMap(grid, [ling], blocker, margin=20)
    assert not reach.safe_ground(covered)


def test_safe_cells_are_the_faces_outside_enemy_reach_and_the_walk_goes_around():
    # A 3x3 building at (15, 15) in the open; a shooter whose reach covers its top face (y > 15)
    # but not the bottom. A zergling below-left. Safe attack cells: only below; the way there stays out
    # of the danger circle
    grid = np.ones((30, 30), dtype=np.float32)
    grid[14:17, 14:17] = np.inf
    building = unit(15.5, 15.5, radius=1.5, structure=True)
    shooter = unit(15.5, 22.5, ground_range=5.0, is_memory=False)
    ling = zergling(5.5, 5.5)
    reach = ReachMap(grid, [ling], [shooter], margin=20)
    reach.set_danger([(shooter.position, 6.0)], 1.0)
    area = reach.safe_area(ling.position)
    assert area > 0
    spots = reach.safe_attack_spots(building, ling.radius, area)
    assert spots
    assert not any(p.y > 17 for p in spots)  # nothing on the face it covers
    assert all(p.distance_to(shooter.position) > 6.0 for p in spots)
    # Walking to one of them never steps into the danger circle
    goal = reach.cell(spots[0])
    position = ling.position
    for _ in range(40):
        step = reach.safe_step(goal, position)
        assert step is not None
        assert step.distance_to(shooter.position) > 6.0
        if step == position:
            break
        position = step
    assert position.distance_to(reach.point(goal)) < 1.5


def test_routes_go_around_danger_and_skip_cut_off_destinations():
    from bot.targeting import RouteMap

    # Open 60x40 map; a danger circle (a sieged tank's reach) in the middle. Going from left to right
    # skirts it; a pocket sealed off by the circle and walls has no route
    grid = np.ones((60, 40), dtype=np.float32)
    tank = Point2((30.5, 20.5))
    routes = RouteMap(grid, [(tank, 10.0)])
    start, goal = Point2((5.5, 20.5)), Point2((55.5, 20.5))
    assert routes.reaches(start, goal)
    position = start
    for _ in range(200):
        step = routes.waypoint(position, goal)
        assert step is not None and step.distance_to(tank) > 10.0
        if step == position:
            break
        position = step
    assert position.distance_to(goal) < 1.5
    # A pocket behind the tank: walls around x 40..50, open only toward the tank's circle
    walled = np.ones((60, 40), dtype=np.float32)
    walled[38:52, 14:16] = np.inf
    walled[38:52, 26:28] = np.inf
    walled[50:52, 14:28] = np.inf
    pocket = RouteMap(walled, [(Point2((36.5, 20.5)), 7.0)])
    assert not pocket.reaches(start, Point2((45.5, 20.5)))


def test_corner_gaps_between_buildings_are_walkable():
    # Four walls around a 6x6 pocket (x 10..15, y 10..15), touching only at the corners, like a wall of
    # bays with the corner bays dead (wall_bays): SC2 units walk through those corner gaps
    grid = np.ones((30, 30), dtype=np.float32)
    grid[7:10, 10:16] = np.inf  # left
    grid[16:19, 10:16] = np.inf  # right
    grid[10:16, 7:10] = np.inf  # bottom
    grid[10:16, 16:19] = np.inf  # top
    marine = unit(12.5, 12.5, ground_range=5.0, is_memory=False)
    ling = zergling(3.5, 3.5)
    reach = ReachMap(grid, [ling], [marine], margin=20)
    assert reach.open_sides(marine) > 0


def test_buildings_only_busy_enemies_reach_are_conditionally_free():
    # The wall_bunker row: once zerglings are on the bunker, the bays beside it (in its reach) are free to
    # work; while nobody is on it they aren't
    from sc2.ids.unit_typeid import UnitTypeId

    from bot.army import covered_work

    grid = np.full((40, 40), np.inf, dtype=np.float32)
    grid[:, 22:] = 1.0
    bunker = unit(14.5, 20.5, radius=1.8125, structure=True, is_memory=False, type_id=UnitTypeId.BUNKER)
    bays = [
        unit(x, 20.5, radius=1.75, structure=True, is_memory=False, type_id=UnitTypeId.ENGINEERINGBAY)
        for x in (11.5, 17.5, 20.5)
    ]
    for b in [bunker, *bays]:
        b.can_attack_ground = False
    ling = zergling(14.5, 26.5)
    reach = ReachMap(grid, [ling], [bunker, *bays], margin=20)
    slots = TargetSlots(reach)
    assert covered_work(slots, bays, [bunker, *bays], ling) == []
    slots.take(bunker, zergling())
    found = covered_work(slots, bays, [bunker, *bays], ling)
    assert bays[0] in found and bays[1] in found
    assert slots.capacity(ling, bays[0]) == len(slots.covered[bays[0].tag]) > 0


def test_a_slot_held_from_far_away_goes_to_a_closer_attacker():
    # A zergling target (room for a ring of them); every slot held by zerglings stuck 5 tiles away. One
    # right next to it gets a slot: the furthest holder loses its own, and picks again when handled
    from bot.targeting import Assignments

    target = zergling(10, 0)
    assignments = Assignments()
    assignments.start(1.0, set())
    slots = TargetSlots(assignments=assignments)
    far = [zergling(4, 0) for _ in range(attack_capacity(zergling(), target))]
    for f in far:
        slots.assignments.release(f)
        slots.take(target, f)
        assignments.assign(f, target, 1.0)
    near = zergling(9, 0)
    assert slots.has_room(near, target)
    slots.take(target, near)
    assert sum(1 for f in far if assignments.target.get(f.tag) == target.tag) == len(far) - 1
    # Not from close by: holders already next to it keep their slots
    close = [zergling(9.2, 0) for _ in range(attack_capacity(zergling(), target))]
    assignments.start(2.0, set())
    slots = TargetSlots(assignments=assignments)
    for c in close:
        slots.assignments.release(c)
        slots.take(target, c)
        assignments.assign(c, target, 2.0)
    assert not slots.has_room(zergling(9, 0.5), target)
