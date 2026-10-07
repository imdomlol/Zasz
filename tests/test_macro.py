import pytest
from sc2.ids.unit_typeid import UnitTypeId

import bot.actions.macro
import bot.decisions
from bot.actions.macro import spawn_composition
from bot.decisions import Decision

MORE_UNITS = [UnitTypeId.ZERGLING, UnitTypeId.ROACH, UnitTypeId.HYDRALISK]


@pytest.fixture
def more_army_units(monkeypatch):
    """As if `ARMY_UNITS` listed roaches and hydralisks too (today it's zerglings only)."""
    monkeypatch.setattr(bot.decisions, "ARMY_UNITS", MORE_UNITS)
    monkeypatch.setattr(bot.actions.macro, "ARMY_UNITS", MORE_UNITS)


def test_spawn_composition_in_ares_format(more_army_units):
    d = Decision(army={UnitTypeId.ZERGLING: 0.7, UnitTypeId.ROACH: 0.3, UnitTypeId.HYDRALISK: 0.0})
    assert spawn_composition(d) == {
        UnitTypeId.ZERGLING: {"proportion": 0.7, "priority": 0},
        UnitTypeId.ROACH: {"proportion": 0.3, "priority": 1},
    }


def test_priority_follows_the_fixed_list_not_dict_order(more_army_units):
    d = Decision(army={UnitTypeId.ROACH: 0.3, UnitTypeId.ZERGLING: 0.7})
    assert spawn_composition(d) == {
        UnitTypeId.ZERGLING: {"proportion": 0.7, "priority": 0},
        UnitTypeId.ROACH: {"proportion": 0.3, "priority": 1},
    }


def test_no_army_wanted():
    assert spawn_composition(Decision()) == {}
