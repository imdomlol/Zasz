"""Offline tests for damage efficiency (`bot.efficiency`) and target spreading (`bot.targeting`).

Units are stand-ins with just the attributes the code reads, so no game is needed.
"""

from types import SimpleNamespace

from sc2.position import Point2

from bot.efficiency import COMMIT_SECONDS, DamageTracker, EfficiencyTotals
from bot.targeting import TargetSlots, attack_capacity

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


def test_no_cap_for_ranged_or_air_attackers_or_unit_targets():
    assert attack_capacity(mutalisk(), bay()) is None  # ranged/air: not limited by space
    assert attack_capacity(zergling(), zergling(1, 0)) is None  # units get surrounded from all sides


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
