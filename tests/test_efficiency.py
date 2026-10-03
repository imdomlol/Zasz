"""Offline tests for damage efficiency (`bot.efficiency`) and target spreading (`bot.targeting`).

Units are stand-ins with just the attributes the code reads, so no game is needed.
"""

from types import SimpleNamespace

import numpy as np
from sc2.position import Point2

from bot.efficiency import COMMIT_SECONDS, DamageTracker, EfficiencyTotals
from bot.targeting import ReachMap, TargetSlots, attack_capacity

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


# --- targeting ---


def test_melee_vs_structure_is_capped_by_space():
    capacity = attack_capacity(zergling(), bay())
    assert capacity is not None and 3 <= capacity <= 12


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
        slots.take(target)
    assert slots.pick(zergling(0, 0), [target]) is target


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
