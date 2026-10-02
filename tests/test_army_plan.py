"""Offline tests for the army plan (`bot.army_plan`): enemy grouping, spots outside an enemy
group's reach, raid sizing by the combat sim, and stable task assignment.

Units and the bot are stand-ins with only what the code reads, so no game is needed.
"""

from types import SimpleNamespace

from ares.consts import EngagementResult
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bot.army_plan import ArmyPlan, EnemyGroup, Task, enemy_groups, needed
from bot.settings import RAID_MIN

_tags = iter(range(1, 10_000))


def unit(x, y, *, type_id=UnitTypeId.MARINE, ground_range=5.0, radius=0.375, memory=False, flying=False):
    return SimpleNamespace(
        tag=next(_tags),
        position=Point2((x, y)),
        type_id=type_id,
        ground_range=ground_range,
        radius=radius,
        is_memory=memory,
        is_flying=flying,
    )


def bot_with(enemy_army=(), win_with=None):
    """A stand-in bot: `win_with` = how many of our units the combat sim needs to win."""

    def can_win_fight(own_units, enemy_units, **_):
        if win_with is not None and len(own_units) >= win_with:
            return EngagementResult.VICTORY_DECISIVE
        return EngagementResult.LOSS_DECISIVE

    mediator = SimpleNamespace(get_cached_enemy_army=list(enemy_army), can_win_fight=can_win_fight)
    return SimpleNamespace(mediator=mediator, time=0.0)


def test_enemy_groups_cluster_by_distance_and_ignore_workers_and_air():
    near_a = [unit(10, 10), unit(12, 10)]
    far_b = [unit(60, 60, memory=True)]
    ignored = [unit(11, 11, type_id=UnitTypeId.SCV), unit(10, 12, flying=True)]
    groups = enemy_groups(bot_with([*near_a, *far_b, *ignored]))
    assert sorted(len(g.units) for g in groups) == [1, 2]
    a = next(g for g in groups if len(g.units) == 2)
    assert a.visible and a.center == Point2((11, 10)) and a.spread == 1.0
    b = next(g for g in groups if len(g.units) == 1)
    assert not b.visible  # remembered only


def test_spot_outside_group_reach_clears_every_unit():
    units = [unit(0, 0), unit(4, 0)]  # center (2, 0), spread 2
    group = EnemyGroup(units, Point2((2, 0)), reach=5.375, visible=True, spread=2.0)
    spot = group.outside(Point2((100, 0)), margin=1.5)
    for u in units:
        assert spot.distance_to(u.position) > u.ground_range + u.radius + 1.0


def test_needed_sizes_raids_in_steps_and_none_when_hopeless():
    lings = [unit(i, 0) for i in range(30)]
    enemies = [unit(50, 0)]
    assert needed(bot_with(win_with=RAID_MIN), lings, enemies, Point2((50, 0))) == RAID_MIN
    assert needed(bot_with(win_with=RAID_MIN + 1), lings, enemies, Point2((50, 0))) == 2 * RAID_MIN
    assert needed(bot_with(win_with=31), lings, enemies, Point2((50, 0))) is None


def test_take_keeps_units_on_the_same_task_across_replans():
    plan = ArmyPlan()
    lings = [unit(0, i) for i in range(4)]
    plan._free, plan._new = list(lings), []
    first = Task("scout", Point2((50, 50)))
    plan.take(first, 1)
    keeper = next(iter(first.tags))

    # Re-plan: same kind of task at nearly the same spot goes to the same zergling, even if
    # another one is now closer
    plan._previous = {keeper: first}
    plan._free, plan._new = list(lings), []
    again = Task("scout", Point2((52, 50)))
    plan.take(again, 1)
    assert again.tags == {keeper}


def test_take_prefers_the_closest_free_units():
    plan = ArmyPlan()
    near, far = unit(10, 0), unit(90, 0)
    plan._free, plan._new, plan._previous = [far, near], [], {}
    task = Task("fight", Point2((0, 0)))
    plan.take(task, 1)
    assert task.tags == {near.tag} and plan._free == [far]
