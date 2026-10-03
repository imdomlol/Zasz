"""Offline tests for how enemy threats are read (`bot.army`): bunkers shoot through their marines."""

from types import SimpleNamespace

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bot.army import armed, dps_vs, reach_of, weapon_range
from bot.settings import BUNKER_MARINES, MARINE_DPS, MARINE_RANGE


def enemy(type_id, *, can_attack_ground=False, ground_range=0.0, radius=1.5, dps=0.0):
    return SimpleNamespace(
        type_id=type_id,
        can_attack_ground=can_attack_ground,
        ground_range=ground_range,
        radius=radius,
        position=Point2((0, 0)),
        calculate_dps_vs_target=lambda target: dps,
    )


def test_a_bunker_is_armed_with_its_marines():
    bunker = enemy(UnitTypeId.BUNKER)  # no weapon of its own, as SC2 reports it
    ling = SimpleNamespace(radius=0.375)
    assert armed(bunker)
    assert weapon_range(bunker) == MARINE_RANGE + 1
    assert dps_vs(bunker, ling) == BUNKER_MARINES * MARINE_DPS
    assert reach_of(bunker, ling) == MARINE_RANGE + 1 + 1.5 + 0.375


def test_other_units_and_structures_are_read_as_they_are():
    bay = enemy(UnitTypeId.ENGINEERINGBAY)
    marine = enemy(UnitTypeId.MARINE, can_attack_ground=True, ground_range=5.0, radius=0.375, dps=9.8)
    assert not armed(bay) and weapon_range(bay) == 0
    assert armed(marine) and weapon_range(marine) == 5.0 and dps_vs(marine, None) == 9.8
