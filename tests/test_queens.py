"""Offline tests for injector pairing (`bot.actions.queens.pair_injectors`)."""

from types import SimpleNamespace

from sc2.position import Point2

from bot.actions.queens import pair_injectors
from bot.actions.state import ActionState


class Hatcheries(list):
    @property
    def ready(self):
        return self


def queen(tag, x, y):
    position = Point2((x, y))
    return SimpleNamespace(tag=tag, position=position, distance_to=lambda u: position.distance_to(u.position))


def hatchery(tag, x, y):
    return SimpleNamespace(tag=tag, position=Point2((x, y)))


def test_two_queens_walking_past_each_other_keep_their_hatcheries():
    a, b = hatchery(1, 0, 0), hatchery(2, 20, 0)
    ai = SimpleNamespace(action_state=ActionState(), townhalls=Hatcheries([a, b]))
    q1, q2 = queen(11, 1, 0), queen(12, 19, 0)
    assert pair_injectors(ai, [q1, q2]) == {11: a, 12: b}
    # Now each is closer to the other's hatchery (walking back from defending): no swap
    q1, q2 = queen(11, 15, 0), queen(12, 5, 0)
    assert pair_injectors(ai, [q1, q2]) == {11: a, 12: b}


def test_a_dead_injector_is_replaced_and_a_new_base_gets_a_queen():
    a, b = hatchery(1, 0, 0), hatchery(2, 20, 0)
    ai = SimpleNamespace(action_state=ActionState(), townhalls=Hatcheries([a]))
    q1, q2, q3 = queen(11, 1, 0), queen(12, 3, 0), queen(13, 18, 0)
    assert pair_injectors(ai, [q1, q2, q3]) == {11: a}
    ai.townhalls.append(b)
    assert pair_injectors(ai, [q1, q2, q3]) == {11: a, 13: b}
    assert pair_injectors(ai, [q2, q3]) == {12: a, 13: b}  # 11 died: the closest free queen takes over
