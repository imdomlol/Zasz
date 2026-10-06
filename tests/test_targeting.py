"""Offline tests for target choice as `fight()` makes it (`bot.targeting.pick_tiered`) and the slot accounting
behind it (`TargetSlots`, `Assignments`).

Tests marked xfail pin down known bugs from CODE_REVIEW.md: they fail now and should pass once the finding
is fixed (strict, so a fix that makes one pass shows up as a failure to remove the mark).
"""

import pytest
from sc2.position import Point2

from bot.targeting import Assignments, BlockTracker, TargetSlots, attack_capacity, melee_share, pick_tiered
from fakes import bay, marine, unit, zergling

ORIGIN = Point2((0, 0))


def new_step(now: float, assignments: Assignments | None = None, alive=()) -> TargetSlots:
    """A fresh step's slots, as `Army` makes them: assignments carried over, reservations redone."""
    assignments = assignments if assignments is not None else Assignments()
    assignments.start(now, {a.tag for a in alive})
    return TargetSlots(assignments=assignments)


def block(blocks: BlockTracker, attacker, target, until: float = 99.0) -> None:
    blocks.blocked.setdefault(attacker.tag, {})[target.tag] = until


# --- choosing a target ---


def test_no_candidates_no_target():
    assert pick_tiered(new_step(1.0), BlockTracker(), zergling(), [([], True)], ORIGIN, 1.0) == (None, False)


def test_fill_from_the_back():
    """The first attackers take the deepest structure; once it's full the next one is used."""
    front, back = bay(5, 0), bay(10, 0)
    slots, blocks = new_step(1.0), BlockTracker()
    capacity = attack_capacity(zergling(), back)
    picks = [
        pick_tiered(slots, blocks, zergling(0, 0), [([front, back], True)], ORIGIN, 1.0) for _ in range(capacity + 2)
    ]
    assert picks[:capacity] == [(back, False)] * capacity
    assert picks[capacity:] == [(front, False)] * 2


def test_nearest_fill_without_depth():
    front, back = bay(5, 0), bay(10, 0)
    assert pick_tiered(new_step(1.0), BlockTracker(), zergling(0, 0), [([front, back], True)], None, 1.0) == (
        front,
        False,
    )


def test_a_tier_not_filled_from_the_back_keeps_the_callers_order():
    front, back = bay(5, 0), bay(10, 0)
    target, _ = pick_tiered(new_step(1.0), BlockTracker(), zergling(0, 0), [([back, front], False)], ORIGIN, 1.0)
    assert target is back


def test_the_pick_is_remembered():
    slots, attacker, target = new_step(1.0), zergling(0, 0), bay(5, 0)
    pick_tiered(slots, BlockTracker(), attacker, [([target], True)], ORIGIN, 1.0)
    assert slots.assignments.target[attacker.tag] == target.tag
    assert slots.taken[target.tag] == 1


def test_keeps_its_game_order_target_while_it_has_room():
    front, back = bay(5, 0), bay(10, 0)
    attacker = zergling(0, 0)
    attacker.order_target = front.tag
    target, _ = pick_tiered(new_step(1.0), BlockTracker(), attacker, [([front, back], True)], ORIGIN, 1.0)
    assert target is front


def test_keeps_last_steps_target_across_steps():
    """A zergling walking around others has a move order (no target to read back): `Assignments`
    remembers what it went for, so the next step it keeps it rather than re-picking the deepest."""
    front, back = bay(5, 0), bay(10, 0)
    attacker = zergling(0, 0)
    assignments = Assignments()
    slots = new_step(1.0, assignments, [attacker])
    assert pick_tiered(slots, BlockTracker(), attacker, [([front], True)], ORIGIN, 1.0) == (front, False)
    slots = new_step(1.2, assignments, [attacker])
    assert pick_tiered(slots, BlockTracker(), attacker, [([front, back], True)], ORIGIN, 1.2) == (front, False)


def test_a_higher_tier_beats_keeping_the_target():
    """A zergling breaking through a building switches back to units as soon as one is reachable."""
    building, unit_target = bay(5, 0), marine(8, 0)
    attacker = zergling(0, 0)
    attacker.order_target = building.tag
    tiers = [([unit_target], True), ([building], True)]
    assert pick_tiered(new_step(1.0), BlockTracker(), attacker, tiers, ORIGIN, 1.0) == (unit_target, False)


def test_a_blocked_target_is_not_kept():
    front, back = bay(5, 0), bay(10, 0)
    attacker, blocks = zergling(0, 0), BlockTracker()
    attacker.order_target = front.tag
    block(blocks, attacker, front)
    assert pick_tiered(new_step(1.0), blocks, attacker, [([front, back], True)], ORIGIN, 1.0) == (back, False)


def test_blocked_from_everything_goes_around_the_closest():
    front, back = bay(5, 0), bay(10, 0)
    attacker, blocks = zergling(0, 0), BlockTracker()
    block(blocks, attacker, front)
    block(blocks, attacker, back)
    slots = new_step(1.0)
    assert pick_tiered(slots, blocks, attacker, [([front, back], True)], ORIGIN, 1.0) == (front, True)
    assert not slots.taken  # going around isn't a claim


def test_a_full_tier_gives_way_to_the_next_with_room():
    full, spare = bay(5, 0), marine(30, 0)
    slots = new_step(1.0)
    for _ in range(attack_capacity(zergling(), full)):
        slots.take(full, zergling(0, 0))
    tiers = [([full], True), ([spare], True)]
    assert pick_tiered(slots, BlockTracker(), zergling(0, 0), tiers, ORIGIN, 1.0) == (spare, False)


def test_everything_full_falls_back_to_the_closest_in_the_best_tier():
    near, deep, lower = bay(5, 0), bay(10, 0), bay(3, 3)
    slots = new_step(1.0)
    for target in (near, deep, lower):
        for _ in range(attack_capacity(zergling(), target)):
            slots.take(target, zergling(0, 0))
    tiers = [([near, deep], True), ([lower], True)]  # closest of the first tier, not the deepest or `lower`
    assert pick_tiered(slots, BlockTracker(), zergling(0, 0), tiers, ORIGIN, 1.0) == (near, False)


def test_a_slot_held_from_far_away_goes_to_a_closer_attacker():
    # A zergling target: every slot held by zerglings stuck 5 tiles away. One right next to it gets
    # a slot: the furthest holder loses its own, and picks again when handled
    target = zergling(10, 0)
    assignments = Assignments()
    far = [zergling(4, 0) for _ in range(attack_capacity(zergling(), target))]
    slots = new_step(1.0, assignments, far)
    for f in far:
        assert pick_tiered(slots, BlockTracker(), f, [([target], True)], None, 1.0) == (target, False)
    near = zergling(9, 0)
    assert pick_tiered(slots, BlockTracker(), near, [([target], True)], None, 1.0) == (target, False)
    assert sum(assignments.target.get(f.tag) == target.tag for f in far) == len(far) - 1


# --- slot accounting ---


def test_last_steps_holders_keep_their_slots_from_newcomers():
    target = bay(5, 0)
    assignments = Assignments()
    holders = [zergling(4, 2) for _ in range(attack_capacity(zergling(), target))]
    slots = new_step(1.0, assignments, holders)
    for h in holders:
        pick_tiered(slots, BlockTracker(), h, [([target], True)], None, 1.0)
    slots = new_step(1.2, assignments, holders)  # nobody handled yet this step
    assert not slots.has_room(zergling(4, 2), target)
    for h in holders:  # handled: they keep it, and it's still full
        assert pick_tiered(slots, BlockTracker(), h, [([target], True)], None, 1.2) == (target, False)
    assert not slots.has_room(zergling(4, 2), target)


def test_dead_attackers_free_their_slots():
    target = bay(5, 0)
    assignments = Assignments()
    holders = [zergling(4, 2) for _ in range(attack_capacity(zergling(), target))]
    slots = new_step(1.0, assignments, holders)
    for h in holders:
        pick_tiered(slots, BlockTracker(), h, [([target], True)], None, 1.0)
    slots = new_step(1.2, assignments, holders[1:])
    assert slots.has_room(zergling(4, 2), target)


def test_releasing_twice_releases_once():
    target = bay(5, 0)
    assignments = Assignments()
    a, b = zergling(4, 2), zergling(4, 3)
    slots = new_step(1.0, assignments, [a, b])
    for attacker in (a, b):
        pick_tiered(slots, BlockTracker(), attacker, [([target], True)], None, 1.0)
    new_step(1.2, assignments, [a, b])
    assignments.release(a)
    assignments.release(a)
    assert assignments.reserved[target.tag] == pytest.approx(melee_share(b, target))


def test_a_stale_attacker_releases_nothing():
    target = bay(5, 0)
    assignments = Assignments()
    stale, recent = zergling(4, 2), zergling(4, 3)
    assignments.assign(stale, target, 0.0)
    assignments.assign(recent, target, 0.8)
    assignments.start(1.0, {stale.tag, recent.tag})  # only `recent` picked within the last 0.5 s
    assignments.release(stale)
    assert assignments.reserved[target.tag] == pytest.approx(melee_share(recent, target))


def test_a_repick_replaces_the_claim():
    first, second = bay(5, 0), bay(10, 0)
    attacker, blocks = zergling(0, 0), BlockTracker()
    slots = new_step(1.0)
    tiers = [([first, second], False)]
    assert pick_tiered(slots, blocks, attacker, tiers, None, 1.0) == (first, False)
    block(blocks, attacker, first)  # what `fight()` finds out next, before picking again
    assert pick_tiered(slots, blocks, attacker, tiers, None, 1.0) == (second, False)
    assert {t: n for t, n in slots.taken.items() if n} == {second.tag: 1}


def test_taking_the_same_target_again_is_one_claim():
    target = bay(5, 0)
    attacker = zergling(0, 0)
    slots = new_step(1.0)
    pick_tiered(slots, BlockTracker(), attacker, [([target], True)], None, 1.0)
    slots.take(target, attacker)  # `fight()` recursing after planning a detour ("detour follow")
    assert slots.taken[target.tag] == 1
    assert slots.assignments.used[target.tag] == pytest.approx(melee_share(attacker, target))


def brute_far_holder(assignments: Assignments, attacker, target):
    """`Assignments.far_holder` as a scan over every assigned attacker (what the per-target index replaces)."""
    from bot.settings import SLOT_HOLD_GAP, SLOT_TAKE_OVER_MARGIN
    from bot.targeting import edge_gap

    own = edge_gap(attacker, target)
    far = [
        (g, a)
        for a, g in assignments.gap.items()
        if a != attacker.tag
        and g > SLOT_HOLD_GAP
        and g > own + SLOT_TAKE_OVER_MARGIN
        and assignments.holds(a, target.tag)
    ]
    return max(far)[1] if far else None


def test_far_holder_finds_what_a_full_scan_finds():
    import random

    rng = random.Random(7)
    targets = [zergling(rng.uniform(0, 20), rng.uniform(0, 20)) for _ in range(4)]
    attackers = [zergling(rng.uniform(0, 20), rng.uniform(0, 20)) for _ in range(40)]
    assignments = Assignments()
    for step in range(6):
        now = 1.0 + 0.3 * step
        alive = [a for a in attackers if rng.random() > 0.05]
        slots = new_step(now, assignments, alive)
        for a in rng.sample(alive, len(alive) // 2):
            target = rng.choice(targets)
            slots.assignments.release(a)
            if rng.random() < 0.2 and (far := assignments.far_holder(a, target)) is not None:
                assignments.drop(far, target.tag)
            assignments.assign(a, target, now)
        for a in alive:
            for t in targets:
                assert assignments.far_holder(a, t) == brute_far_holder(assignments, a, t)


def test_safe_attack_spots_are_the_same_from_the_cache():
    import numpy as np

    from bot.targeting import ReachMap

    grid = np.ones((30, 30), dtype=np.float32)
    building = bay(15.5, 15.5, is_memory=False)
    grid[14:17, 14:17] = np.inf
    ling = zergling(5.5, 5.5)
    reach = ReachMap(grid, [ling], [building], margin=20)
    reach.set_danger([(Point2((19, 15.5)), 3.0)], 1.0)
    area = reach.safe_area(ling.position)
    expected = [
        p
        for p in reach.attack_spots(building, ling.radius, ling.ground_range)
        if reach.safe_labels[reach.cell(p)] == area and not reach.in_danger(p)
    ]
    assert 0 < len(expected) < len(reach.attack_spots(building, ling.radius, ling.ground_range))
    for _ in range(2):  # built, then from the cache
        assert reach.safe_attack_spots(building, ling.radius, area, ling.ground_range) == expected
    reach.set_danger([], 2.0)  # a new stamp: worked out again
    assert len(reach.safe_attack_spots(building, ling.radius, area, ling.ground_range)) > len(expected)


def test_unchanged_danger_keeps_its_fields_and_changed_danger_rebuilds_them():
    import numpy as np

    from bot.targeting import ReachMap

    grid = np.ones((30, 30), dtype=np.float32)
    ling = zergling(5.5, 5.5)
    reach = ReachMap(grid, [ling], [bay(20.5, 20.5, is_memory=False)], margin=20)
    zones = [(Point2((15.5, 15.5)), 4.0)]
    reach.set_danger(list(zones), 1.0)
    goal = reach.cell(Point2((25.5, 5.5)))
    step = reach.safe_step(goal, ling.position)
    labels, field = reach.safe_labels, reach._safe_fields[goal]
    reach.set_danger(list(zones), 2.0)  # the same zones a step later
    assert reach.safe_labels is labels and reach._safe_fields[goal] is field
    assert reach.safe_step(goal, ling.position) == step
    reach.set_danger([(Point2((8.5, 8.5)), 4.0)], 3.0)  # someone moved
    fresh = ReachMap(grid, [ling], [bay(20.5, 20.5, is_memory=False)], margin=20)
    fresh.set_danger([(Point2((8.5, 8.5)), 4.0)], 3.0)
    assert goal not in reach._safe_fields
    assert (reach.danger == fresh.danger).all() and (reach.safe_labels == fresh.safe_labels).all()


def test_no_danger_on_the_first_stamp_still_labels_the_safe_ground():
    import numpy as np

    from bot.targeting import ReachMap

    ling = zergling(5.5, 5.5)
    reach = ReachMap(np.ones((30, 30), dtype=np.float32), [ling], [bay(20.5, 20.5, is_memory=False)], margin=20)
    reach.set_danger([], 1.0)
    assert reach.safe_area(ling.position) > 0


def test_both_graph_flavours_agree_without_enemy_units_in_the_way():
    import numpy as np

    from bot.targeting import ReachMap, build_graph

    grid = np.ones((20, 20), dtype=np.float32)
    grid[5:8, 5:8] = np.inf  # a building, and one touching it at a corner
    grid[8:11, 8:11] = np.inf
    reach = ReachMap(grid, [zergling(2.5, 2.5)], [bay(15.5, 15.5, is_memory=False)], margin=20)
    assert not reach.unit_blocked.any()  # buildings only: the diagonal rule has nothing to forbid
    assert (reach._build_graph(reach.walkable) != build_graph(reach.walkable)).nnz == 0


def test_reaches_from_one_place_agrees_with_reaches():
    import numpy as np

    from bot.targeting import RouteMap

    grid = np.ones((40, 40), dtype=np.float32)
    grid[20, :] = np.inf  # a wall across the map with one gap
    grid[20, 35] = 1.0
    routes = RouteMap(grid, [(Point2((20.5, 35.5)), 3.0)])  # danger sitting in the gap
    start = Point2((5.5, 5.5))
    for goal in [Point2((10.5, 30.5)), Point2((30.5, 5.5)), Point2((30.5, 38.5)), Point2((19.5, 20.5))]:
        assert routes.reaches_from(start, goal) == routes.reaches(start, goal)
    assert not routes.reaches_from(start, Point2((30.5, 5.5)))  # behind the wall, the gap in danger


def test_buildings_touching_at_a_corner_still_leave_a_gap():
    import numpy as np

    from bot.targeting import ReachMap

    grid = np.full((12, 12), np.inf, dtype=np.float32)
    grid[1:5, 4] = 1.0
    grid[5, 5:9] = 1.0  # (4, 5) and (5, 4) both buildings/terrain: the corner gap is walkable
    reach = ReachMap(grid, [zergling(1.5, 4.5)], [unit(5.5, 8.5, is_memory=False)], margin=20)
    assert reach.reached[5, 5] and reach.regions()[4, 4] == reach.regions()[5, 5]
