from sc2.ids.unit_typeid import UnitTypeId

from bot.actions.macro import spawn_composition
from bot.decisions import Decision


def test_spawn_composition_in_ares_format():
    d = Decision(army={UnitTypeId.ZERGLING: 0.7, UnitTypeId.ROACH: 0.3, UnitTypeId.HYDRALISK: 0.0})
    assert spawn_composition(d) == {
        UnitTypeId.ZERGLING: {"proportion": 0.7, "priority": 0},
        UnitTypeId.ROACH: {"proportion": 0.3, "priority": 1},
    }


def test_no_army_wanted():
    assert spawn_composition(Decision()) == {}
