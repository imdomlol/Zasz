import pytest
from sc2.data import Race
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

from bot.observation import FEATURE_NAMES, Observation
from bot.spaces import OWN_STRUCTURE_TYPES, OWN_UNIT_TYPES, RACES, UPGRADES


def sample() -> Observation:
    return Observation(
        game_time=312.5,
        minerals=275,
        vespene=48,
        mineral_income=1450.0,
        supply_used=67,
        supply_cap=78,
        drones=41,
        drone_capacity=44,
        bases=3,
        bases_building=1,
        free_expansion=True,
        pool_placed=True,
        pool_ordered=True,
        pool_ready=True,
        own_units={UnitTypeId.ZERGLING: 18, UnitTypeId.QUEEN: 4},
        upgrades_done={UpgradeId.ZERGLINGMOVEMENTSPEED: 1},
        enemy_race={Race.Protoss: 1},
        enemy_army_supply=22.0,
        enemy_has_air=True,
    )


def test_feature_names_are_unique_and_cover_the_fixed_lists():
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))
    for name, keys in [
        ("own_units", OWN_UNIT_TYPES),
        ("own_structures", OWN_STRUCTURE_TYPES),
        ("upgrades_done", UPGRADES),
        ("enemy_race", RACES),
    ]:
        assert [n for n in FEATURE_NAMES if n.startswith(f"{name}.")] == [f"{name}.{k.name}" for k in keys]


def test_vector_matches_feature_names():
    obs = sample()
    assert list(obs.to_dict()) == FEATURE_NAMES
    vector = obs.to_vector()
    assert len(vector) == len(FEATURE_NAMES)
    assert all(isinstance(v, float) for v in vector)
    assert vector[FEATURE_NAMES.index("own_units.ZERGLING")] == 18.0
    assert vector[FEATURE_NAMES.index("enemy_race.Protoss")] == 1.0
    assert vector[FEATURE_NAMES.index("enemy_has_air")] == 1.0


def test_round_trip_through_dict():
    obs = sample()
    assert Observation.from_dict(obs.to_dict()) == obs
    assert Observation.from_dict(Observation().to_dict()) == Observation()


def test_counts_are_filled_with_zeros():
    obs = Observation(own_units={UnitTypeId.ZERGLING: 3})
    assert obs.own_units[UnitTypeId.DRONE] == 0
    assert list(obs.own_units) == OWN_UNIT_TYPES


def test_counts_outside_the_fixed_list_are_rejected():
    with pytest.raises(ValueError):
        Observation(own_units={UnitTypeId.ROACH: 3})


def test_older_recordings_missing_features_still_load():
    flat = sample().to_dict()
    del flat["enemy_has_air"], flat["own_units.QUEEN"]
    obs = Observation.from_dict(flat)
    assert obs.enemy_has_air is False
    assert obs.own_units[UnitTypeId.QUEEN] == 0
    assert obs.own_units[UnitTypeId.ZERGLING] == 18
