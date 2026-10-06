"""Offline tests for squads (`bot.squads`): who's in which squad, where patrols go, which squads
help a fight, and the enemy helpers moved over from the old army plan.

Units and the bot are stand-ins with only what the code reads, so no game is needed.
"""

from types import SimpleNamespace

from ares.consts import EngagementResult
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bot.settings import JOIN_RADIUS, RAID_MIN, SQUAD_MAX, SQUAD_SIZE
from bot.squads import EnemyGroup, Patrol, SquadBook, choose_helpers, enemy_groups, enemy_sites, needed

_tags = iter(range(1, 100_000))


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


def ling(x, y):
    return unit(x, y, type_id=UnitTypeId.ZERGLING, ground_range=0.1)


def bot_with(enemy_army=(), win_with=None):
    """A stand-in bot: `win_with` = how many of our units the combat sim needs to win."""

    def can_win_fight(own_units, enemy_units, **_):
        if win_with is not None and len(own_units) >= win_with:
            return EngagementResult.VICTORY_DECISIVE
        return EngagementResult.LOSS_DECISIVE

    mediator = SimpleNamespace(get_cached_enemy_army=list(enemy_army), can_win_fight=can_win_fight)
    return SimpleNamespace(mediator=mediator, time=0.0)


# --- squads ---


def test_new_zerglings_fill_a_squad_then_start_the_next():
    book = SquadBook()
    lings = [ling(10, 10) for _ in range(SQUAD_SIZE + 3)]
    squads = book.update(lings, busy=set())
    assert sorted(len(s.tags) for s in squads) == [3, SQUAD_SIZE]


def test_far_away_zerglings_start_their_own_squad():
    book = SquadBook()
    near = [ling(10, 10) for _ in range(2)]
    far = [ling(10 + JOIN_RADIUS + 5, 10)]
    squads = book.update(near + far, busy=set())
    assert sorted(len(s.tags) for s in squads) == [1, 2]


def test_squads_keep_their_identity_and_drop_the_dead():
    book = SquadBook()
    lings = [ling(10, 10) for _ in range(4)]
    (squad,) = book.update(lings, busy=set())
    squad.job = "raid"
    (again,) = book.update(lings[1:], busy=set())
    assert again is squad and again.job == "raid" and len(again.tags) == 3


def test_small_squads_out_of_combat_merge_but_not_in_combat():
    book = SquadBook()
    a = [ling(10, 10) for _ in range(3)]
    b = [ling(10 + JOIN_RADIUS + 1, 10) for _ in range(3)]
    squads = book.update(a + b, busy=set())
    assert len(squads) == 2
    # They walk toward each other
    for u in b:
        u.position = Point2((14, 10))
    busy = {squads[0].id}
    assert len(book.update(a + b, busy=busy)) == 2  # one is fighting: leave it alone
    assert len(book.update(a + b, busy=set())) == 1


def test_a_big_squad_out_of_combat_splits():
    book = SquadBook()
    lings = [ling(10, 10) for _ in range(SQUAD_SIZE)]
    (squad,) = book.update(lings, busy=set())
    helpers = [ling(10 + i * 0.5, 10) for i in range(SQUAD_MAX + 1 - SQUAD_SIZE)]
    squad.tags |= {u.tag for u in helpers}  # helpers that joined it
    everyone = lings + helpers
    assert len(book.update(everyone, busy={squad.id})) == 1  # still fighting: keep it whole
    squads = book.update(everyone, busy=set())
    assert len(squads) == 2 and sum(len(s.tags) for s in squads) == len(everyone)


# --- patrol ---


def test_patrol_goes_to_the_stalest_spot_nearest_first_and_spreads_out():
    patrol = Patrol()
    near, far, seen = Point2((20, 0)), Point2((80, 0)), Point2((30, 0))
    patrol.spots = [near, far, seen]
    patrol.seen = {near: 0.0, far: 0.0, seen: 95.0}  # `seen` was in vision 5 s ago
    here = Point2((0, 0))
    assert patrol.pick(100.0, here, claimed=[], avoid=lambda p: False) == near
    assert patrol.pick(100.0, here, claimed=[near], avoid=lambda p: False) == far  # another squad has it
    assert patrol.pick(100.0, here, claimed=[], avoid=lambda p: p == near) == far  # dangerous


# --- helping ---


def squad_stub(n, name):
    return SimpleNamespace(name=name), [ling(0, 0) for _ in range(n)]


def wins_with_at_least(n):
    return lambda own, enemies: len(own) >= n


def test_no_help_when_the_fight_is_already_won():
    present = [ling(0, 0) for _ in range(10)]
    assert choose_helpers(present, [unit(5, 0)], [squad_stub(10, "a")], wins_with_at_least(10), room=20) == []


def test_two_squads_that_only_matter_together_both_go():
    # 10 in a bad fight that 30 would win: each squad of 10 alone isn't enough, both are
    present = [ling(0, 0) for _ in range(10)]
    a, b, c = squad_stub(10, "a"), squad_stub(10, "b"), squad_stub(10, "c")
    chosen = choose_helpers(present, [unit(5, 0)], [a, b, c], wins_with_at_least(30), room=40)
    assert [s.name for s in chosen] == ["a", "b"]  # the closest two, not the third


def test_nobody_goes_when_no_set_of_squads_turns_the_fight():
    present = [ling(0, 0) for _ in range(10)]
    candidates = [squad_stub(10, "a"), squad_stub(10, "b")]
    assert choose_helpers(present, [unit(5, 0)], candidates, wins_with_at_least(50), room=60) is None


def test_helpers_beyond_the_room_around_the_targets_dont_count():
    # Enough zerglings in total, but only 5 more fit around the targets: piling on doesn't help
    present = [ling(0, 0) for _ in range(10)]
    candidates = [squad_stub(10, "a"), squad_stub(10, "b")]
    assert choose_helpers(present, [unit(5, 0)], candidates, wins_with_at_least(20), room=5) is None
    assert choose_helpers(present, [unit(5, 0)], candidates, wins_with_at_least(15), room=5) is not None


# --- the enemy (from the old army plan) ---


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


def test_sites_are_structure_clusters_with_their_defenders_plus_armies_in_the_field():
    base = [unit(10, 10, type_id=UnitTypeId.COMMANDCENTER), unit(14, 10, type_id=UnitTypeId.BARRACKS)]
    proxy = [unit(80, 80, type_id=UnitTypeId.BARRACKS)]
    defenders = [unit(12, 14)]
    field_army = [unit(50, 40), unit(51, 40)]
    groups = enemy_groups(bot_with([*defenders, *field_army]))
    sites = enemy_sites(None, base + proxy, groups)
    assert len(sites) == 3
    by_size = sorted(sites, key=lambda s: (len(s.structures), len(s.defenders)))
    assert by_size[0].structures == [] and len(by_size[0].defenders) == 2  # the army in the field
    assert len(by_size[1].structures) == 1 and by_size[1].defenders == []  # the proxy
    assert len(by_size[2].structures) == 2 and len(by_size[2].defenders) == 1  # the defended base


def test_a_plain_win_is_enough_when_no_set_makes_it_comfortable():
    present = [ling(0, 0) for _ in range(10)]
    candidates = [squad_stub(10, "a"), squad_stub(10, "b")]
    comfortable, plain = wins_with_at_least(100), wins_with_at_least(20)
    chosen = choose_helpers(present, [unit(5, 0)], candidates, comfortable, room=40, wins_at_all=plain)
    assert [s.name for s in chosen] == ["a"]


def counting_bot(loop=1):
    calls = []

    def can_win_fight(own_units, enemy_units, **_):
        calls.append((len(own_units), len(enemy_units)))
        return EngagementResult.VICTORY_CLOSE if len(own_units) >= 4 else EngagementResult.LOSS_MARGINAL

    bot = SimpleNamespace(
        state=SimpleNamespace(game_loop=loop),
        mediator=SimpleNamespace(can_win_fight=can_win_fight, get_cached_enemy_army=[]),
    )
    return bot, calls


def test_the_same_fight_is_simulated_once_a_step():
    from bot.settings import FIGHT_AT, HELP_WIN_AT
    from bot.squads import wins

    bot, calls = counting_bot()
    own, enemies = [ling(0, 0) for _ in range(6)], [unit(5, 5)]
    assert wins(bot, own, enemies, HELP_WIN_AT) == (EngagementResult.VICTORY_CLOSE >= HELP_WIN_AT)
    assert wins(bot, own, enemies, FIGHT_AT)
    assert len(calls) == 1
    needed(bot, own, enemies, Point2((0, 0)), HELP_WIN_AT) or needed(bot, own, enemies, Point2((0, 0)), FIGHT_AT)
    assert len(calls) == 1  # `assign_raids`' two passes over the same sizes: answered from the first
    bot.state.game_loop += 2  # a new step: health and positions have changed
    wins(bot, own, enemies)
    assert len(calls) == 2


def test_different_forces_are_simulated_separately():
    from bot.squads import fight_verdict

    bot, calls = counting_bot()
    own, enemies = [ling(0, 0) for _ in range(6)], [unit(5, 5)]
    assert fight_verdict(bot, own[:2], enemies) == EngagementResult.LOSS_MARGINAL
    assert fight_verdict(bot, own, enemies) == EngagementResult.VICTORY_CLOSE
    fight_verdict(bot, own[::-1], enemies)  # same units, another order: the sim isn't assumed order-free
    assert len(calls) == 3


def test_enemy_groups_are_built_once_a_step():
    bot, _ = counting_bot()
    bot.mediator.get_cached_enemy_army = [unit(5, 5)]
    first = enemy_groups(bot)
    assert enemy_groups(bot) is first
    bot.state.game_loop += 2
    assert enemy_groups(bot) is not first
