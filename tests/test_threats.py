"""Offline tests for how enemy threats are read (`bot.army`): bunkers shoot through their marines."""

from types import SimpleNamespace

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bot.army import back_off, choke_worth
from bot.combat import armed, dps_vs, reach_of, weapon_range
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


def test_duel_zerglings_against_the_marines_out_of_a_bunker():
    from bot.combat import duel

    # 4 marines (45 hp, ~9.8 dps) out of a dead bunker: 8 zerglings win with some left, 2 lose
    left, marines = duel(8, 35, 10.0, 4)
    assert marines == 0 and 1 <= left < 8
    left, marines = duel(2, 35, 10.0, 4)
    assert left == 0 and marines > 0


def test_choke_worth_skips_free_broodlings_without_looking_up_a_cost():
    """Broodlings have no creation ability, so python-sc2 cannot calculate their cost."""
    ling = SimpleNamespace(
        radius=0.375,
        health_max=35,
        calculate_dps_vs_target=lambda target: 5.0,
    )
    broodling = SimpleNamespace(
        type_id=UnitTypeId.BROODLING,
        is_structure=False,
        health=30,
        shield=0,
        can_attack_ground=False,
        ground_range=0.1,
        radius=0.375,
        position=Point2((0, 0)),
        calculate_dps_vs_target=lambda target: 0.0,
    )
    slots = SimpleNamespace(capacity=lambda unit, target: 1, reach=None)
    ai = SimpleNamespace(calculate_cost=lambda unit_type: (_ for _ in ()).throw(AssertionError("cost lookup")))

    assert not choke_worth(ai, ling, broodling, slots, [broodling], [])


def test_back_off_stops_cleanly_when_no_armed_threats_remain():
    """A full target can leave no shooter to back away from; that must not calculate an empty center."""
    stopped = []
    ling = SimpleNamespace(orders=[object()], stop=lambda: stopped.append(True))

    back_off(SimpleNamespace(), [ling], [], None)

    assert stopped == [True]


def test_a_unit_type_the_game_puts_no_price_on_costs_nothing_instead_of_crashing():
    from bot.combat import production_cost

    def calculate_cost(type_id):
        if type_id == UnitTypeId.ZERGLINGBURROWED:  # no creation ability: python-sc2 raises
            raise AttributeError("'NoneType' object has no attribute 'exact_id'")
        return SimpleNamespace(minerals=50, vespene=25)

    ai = SimpleNamespace(calculate_cost=calculate_cost)
    assert production_cost(ai, UnitTypeId.ZERGLINGBURROWED) is None
    assert production_cost(ai, UnitTypeId.BROODLING) is None  # in ares' list: not even asked
    assert production_cost(ai, UnitTypeId.ROACH) == 75
