"""Offline tests for expansion reachability (`bot.actions.pathing.ExpansionAccess`): the bot and map are
stand-ins that count how often the pathing grids are built."""

from types import SimpleNamespace

import numpy as np
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bot.actions.pathing import ExpansionAccess


class Group(list):
    """Just enough of `Units`: `not_flying`, `tags`, `filter`, `subgroup`."""

    @property
    def not_flying(self):
        return self

    @property
    def tags(self):
        return {u.tag for u in self}

    def filter(self, keep):
        return Group(u for u in self if keep(u))

    def subgroup(self, units):
        return Group(units)


def structure(tag, type_id=UnitTypeId.HATCHERY, x=10.0, y=10.0):
    return SimpleNamespace(tag=tag, type_id=type_id, position=Point2((x, y)), radius=1.0)


def fake_ai(reachable=True):
    built = []

    def get_pyastar_grid(include_destructables=True):
        built.append("fresh" if include_destructables else "without rocks")
        return np.ones((20, 20), dtype=np.float32)

    def get_base_pathing_grid(include_destructables=False):
        built.append("terrain")
        return np.ones((20, 20))

    map_data = SimpleNamespace(
        get_pyastar_grid=get_pyastar_grid,
        pather=SimpleNamespace(get_base_pathing_grid=get_base_pathing_grid),
        pathfind=lambda start, goal, grid: [Point2(goal)] if reachable else None,
    )
    ai = SimpleNamespace(
        time=100.0,
        time_formatted="01:40",
        state=SimpleNamespace(game_loop=1),
        mediator=SimpleNamespace(get_map_data_object=map_data),
        townhalls=Group(),
        start_location=Point2((5, 5)),
        structures=Group([structure(1)]),
        enemy_structures=Group(),
        destructables=Group(),
    )
    return ai, built


BASES = [Point2((15, 15)), Point2((15, 5)), Point2((5, 15))]


def test_the_grids_are_built_once_a_step_for_every_base():
    ai, built = fake_ai(reachable=False)
    access = ExpansionAccess()
    for base in BASES:
        assert not access.is_reachable(ai, base)
        assert access.blocked[base].kind == "unreachable"
    assert sorted(built) == ["fresh", "terrain", "without rocks"]


def test_a_new_step_builds_fresh_grids():
    ai, built = fake_ai()
    access = ExpansionAccess()
    access.is_reachable(ai, BASES[0])
    ai.state.game_loop += 2
    ai.structures.append(structure(2, x=12))  # a new building: answers are checked again
    access.is_reachable(ai, BASES[0])
    assert built == ["fresh", "fresh"]


def test_creep_tumors_dont_clear_the_answers():
    ai, built = fake_ai()
    access = ExpansionAccess()
    assert access.is_reachable(ai, BASES[0])
    for i, kind in enumerate([UnitTypeId.CREEPTUMOR, UnitTypeId.CREEPTUMORBURROWED, UnitTypeId.CREEPTUMORQUEEN]):
        ai.state.game_loop += 2
        ai.structures.append(structure(10 + i, kind))
        assert access.is_reachable(ai, BASES[0])
    assert built == ["fresh"]
