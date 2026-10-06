"""Offline tests for `Army` state shared between squads: the map-wide route cache (`route_map`) and squad
scouting plans (`send_squad_scout`). The bot is a stand-in with only what the code reads.

Tests marked xfail pin down known bugs from CODE_REVIEW.md (strict: a fix makes them fail until the mark
is removed).
"""

from types import SimpleNamespace

import numpy as np
import pytest
from ares.consts import EngagementResult
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

import bot.army
from bot.army import Army
from bot.settings import PLAN_SECONDS
from fakes import marine, zergling

STRONG = 10  # zerglings the stand-in combat sim needs to beat the enemy group


def fake_ai(time=10.0, expansions=()):
    def can_win_fight(own_units, enemy_units, **_):
        return EngagementResult.VICTORY_DECISIVE if len(own_units) >= STRONG else EngagementResult.LOSS_DECISIVE

    mediator = SimpleNamespace(
        get_ground_grid=np.ones((40, 40), dtype=np.float32),
        can_win_fight=can_win_fight,
        get_enemy_expansions=[(Point2(p), 0.0) for p in expansions],
    )
    return SimpleNamespace(time=time, mediator=mediator, is_visible=lambda p: False, scouting=None)


def lings(n, x=5.5, y=5.5):
    return [zergling(x, y, real_speed=4.13, sight_range=8.0) for _ in range(n)]


@pytest.fixture
def enemy_group(monkeypatch):
    """One enemy marine at (20.5, 20.5), the only enemy group."""
    enemy = marine(20.5, 20.5, type_id=UnitTypeId.MARINE, can_attack_ground=True)
    group = SimpleNamespace(units=[enemy], center=enemy.position, spread=0.0, reach=6.0)
    monkeypatch.setattr(bot.army, "enemy_groups", lambda ai: [group])
    return enemy


def blocked_at(routes, p: Point2) -> bool:
    return not routes.open[int(p.x), int(p.y)]


# --- route cache (CODE_REVIEW.md 03) ---


def test_a_weak_squad_routes_around_a_group_it_cant_beat_and_a_strong_one_doesnt(enemy_group):
    ai = fake_ai()
    assert blocked_at(Army().route_map(ai, lings(2)), enemy_group.position)
    assert not blocked_at(Army().route_map(ai, lings(STRONG)), enemy_group.position)


def test_the_route_map_is_rebuilt_after_plan_seconds(enemy_group):
    army, ai = Army(), fake_ai(time=10.0)
    first = army.route_map(ai, lings(2))
    ai.time += PLAN_SECONDS / 2
    assert army.route_map(ai, lings(2)) is first
    ai.time += PLAN_SECONDS
    assert army.route_map(ai, lings(2)) is not first


def test_a_weak_squad_after_a_strong_one_still_avoids_the_group(enemy_group):
    army, ai = Army(), fake_ai()
    army.route_map(ai, lings(STRONG))
    assert blocked_at(army.route_map(ai, lings(2)), enemy_group.position)


def test_a_strong_squad_after_a_weak_one_isnt_kept_out(enemy_group):
    army, ai = Army(), fake_ai()
    army.route_map(ai, lings(2))
    assert not blocked_at(army.route_map(ai, lings(STRONG)), enemy_group.position)


def test_squads_that_lose_to_the_same_groups_share_a_route_map(enemy_group):
    army, ai = Army(), fake_ai()
    weak = army.route_map(ai, lings(2))
    assert army.route_map(ai, lings(3)) is weak
    assert army.route_map(ai, lings(STRONG)) is not weak


# --- squad scouts (CODE_REVIEW.md 02) ---

SPOT = (30.5, 5.5)  # the one enemy expansion to look at


def scouting_army(monkeypatch):
    """An army with a squad held back by a partly seen position (so it wants to scout), no danger."""
    army = Army()
    army.turned_back_at = [(Point2((15, 15)), 5.0, 999.0)]
    army.avoid_reasons[Point2((15, 15))] = "scout needed"
    monkeypatch.setattr(army, "dangerous_for", lambda ai, units, spot: False)
    monkeypatch.setattr(army, "route_map", lambda ai, units: SimpleNamespace(distance=lambda a, b: a.distance_to(b)))
    return army


def squad(army, n=4):
    s = army.book.new()
    s.units = lings(n)
    s.tags = {u.tag for u in s.units}
    return s


def test_a_squad_waiting_on_an_overlord_scouts_an_expansion(monkeypatch):
    army, ai = scouting_army(monkeypatch), fake_ai(expansions=[SPOT])
    s = squad(army)
    army.send_squad_scout(ai, s)
    assert (s.job, s.target) == ("scout", Point2(SPOT))
    assert army.squad_scouts[s.id][0] == Point2(SPOT)


def test_a_spot_being_scouted_isnt_given_to_a_second_squad(monkeypatch):
    army, ai = scouting_army(monkeypatch), fake_ai(expansions=[SPOT])
    first, second = squad(army), squad(army)
    army.send_squad_scout(ai, first)
    army.send_squad_scout(ai, second)
    assert second.id not in army.squad_scouts and second.job != "scout"


@pytest.mark.xfail(strict=True, reason="CODE_REVIEW.md 02: a dead or merged squad's scout plan claims its spot forever")
def test_a_dead_scouts_spot_can_be_scouted_again(monkeypatch):
    army, ai = scouting_army(monkeypatch), fake_ai(expansions=[SPOT])
    dead, other = squad(army), squad(army)
    army.send_squad_scout(ai, dead)
    del army.book.squads[dead.id]  # all its zerglings died (or it was merged into another squad)
    army.send_squad_scout(ai, other)
    assert other.job == "scout"


# --- diagnostics (CODE_REVIEW.md 01, 23) ---


def test_normal_games_dont_keep_debug_counts_or_timings():
    from bot.army import COUNTS

    army = Army()
    army._control = lambda ai, attacking: COUNTS.update({"a decision": 1})
    try:
        army.control(None, False)
        assert army.step_ms == [] and not COUNTS
        army.debug = True
        army.control(None, False)
        assert len(army.step_ms) == 1 and COUNTS["a decision"] == 1
    finally:
        COUNTS.clear()


def test_a_new_game_starts_with_no_counts_left():
    from bot.army import COUNTS, REACH_DUMPED

    COUNTS["from an earlier game"] += 1
    REACH_DUMPED.add(3)
    Army()
    assert not COUNTS and not REACH_DUMPED


# --- avoid areas (CODE_REVIEW.md 18) ---


def test_avoid_areas_expire_and_take_their_reasons_with_them():
    army, ai = Army(), fake_ai(time=10.0)
    army.turned_back(ai, [marine(20.5, 20.5, type_id=UnitTypeId.MARINE)], "army can't beat")
    army.mark_avoid(ai, Point2((5, 30)), 4.0, "scout needed", seconds=100)
    assert army.blocked(ai, Point2((20.5, 21))) and army.blocked(ai, Point2((5, 31)))
    assert set(army.avoid_reasons.values()) == {"army can't beat", "scout needed"}
    ai.time += 60  # past AVOID_SECONDS, not past the scout's 100 s
    assert not army.blocked(ai, Point2((20.5, 21))) and army.blocked(ai, Point2((5, 31)))
    assert list(army.avoid_reasons.values()) == ["scout needed"]


def test_a_new_area_on_an_old_one_replaces_it_and_its_reason():
    army, ai = Army(), fake_ai(time=10.0)
    army.mark_avoid(ai, Point2((20, 20)), 6.0, "army can't beat")
    army.mark_avoid(ai, Point2((21, 20)), 6.0, "unseen fire")
    assert [(p, r) for p, r, _ in army.turned_back_at] == [(Point2((21, 20)), 6.0)]
    assert army.avoid_reasons == {Point2((21, 20)): "unseen fire"}
