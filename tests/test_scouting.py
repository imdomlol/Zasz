"""Offline tests for overlord scouting (`bot.actions.scouting.Scouting`): a scout whose job is dropped
heads home instead of flying on with its old order."""

from types import SimpleNamespace

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bot.actions.scouting import Scouting

HOME = Point2((10, 10))


class Units(list):
    def find_by_tag(self, tag):
        return next((u for u in self if u.tag == tag), None)


class Townhalls:
    def __bool__(self):
        return True

    @staticmethod
    def closest_distance_to(u):
        return u.position.distance_to(HOME)

    @staticmethod
    def closest_to(u):
        return SimpleNamespace(position=HOME)


def scout_ai(overlord, visible=False):
    behaviors, roles = [], {}
    mediator = SimpleNamespace(
        get_air_grid=None,
        is_position_safe=lambda grid, position: True,  # nothing on the air grid: hit by something unseen
        assign_role=lambda tag, role: roles.__setitem__(tag, role),
        get_enemy_expansions=[],
    )
    ai = SimpleNamespace(
        time=100.0,
        units=Units([overlord]),
        townhalls=Townhalls(),
        mediator=mediator,
        register_behavior=behaviors.append,
        is_visible=lambda p: visible,
    )
    return ai, behaviors, roles


def overlord():
    ol = SimpleNamespace(
        tag=1, type_id=UnitTypeId.OVERLORD, position=Point2((60, 60)), is_idle=False, health_percentage=0.8
    )
    ol.distance_to = lambda p: ol.position.distance_to(p)
    return ol


def heads_home(behaviors) -> bool:
    return any(getattr(b, "target", None) == HOME for b in behaviors)


def test_a_scout_shot_on_the_way_heads_home():
    ol = overlord()
    ai, behaviors, _ = scout_ai(ol)
    scouting = Scouting()
    scouting.requests[Point2((90, 90))] = 95.0
    scouting.sent[ol.tag] = Point2((90, 90))
    scouting.on_damage(ai, ol)
    scouting.control(ai)
    assert heads_home(behaviors)


def test_a_scout_whose_spot_was_seen_heads_home_and_isnt_a_scout_any_more():
    ol = overlord()
    ai, behaviors, roles = scout_ai(ol, visible=True)  # the requested spot is in vision now: done
    scouting = Scouting()
    scouting.requests[Point2((90, 90))] = 95.0
    scouting.sent[ol.tag] = Point2((90, 90))
    scouting.control(ai)
    assert heads_home(behaviors) and ol.tag not in scouting.sent and ol.tag in roles
