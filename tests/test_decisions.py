from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

from bot.decisions import DECISION_NAMES, Build, Decision, Decisions, ScriptedDecisions
from bot.observation import Observation
from bot.settings import (
    ATTACK_AT_ZERGLINGS,
    CREEP_QUEENS,
    EXPAND_WHEN_DRONES_WITHIN,
    MAX_DRONES,
    POOL_AT_DRONES,
    SCRIPTED_UPGRADES,
    SPORES_PER_BASE,
    STOP_ATTACK_AT_ZERGLINGS,
)
from bot.spaces import UPGRADES

# A mid-game state where nothing special is wanted: pool done, bases far from saturated
BASE = dict(pool_placed=True, pool_ordered=True, pool_ready=True, bases=2, drones=20, drone_capacity=38)


def decide(**changes) -> Decision:
    return ScriptedDecisions().decide(Observation(**(BASE | changes)))


def lings(n: int) -> dict:
    return {UnitTypeId.ZERGLING: n}


# --- what to save for ---


def test_no_pool_before_enough_drones():
    obs = dict(pool_placed=False, pool_ordered=False, pool_ready=False, bases=1, drone_capacity=16)
    assert decide(**obs, drones=POOL_AT_DRONES - 1).build == Build.NONE
    assert decide(**obs, drones=POOL_AT_DRONES).build == Build.POOL


def test_pool_only_once():
    assert decide(pool_placed=False, pool_ordered=True, pool_ready=False, drones=POOL_AT_DRONES).build != Build.POOL


def test_expand_when_drones_near_capacity():
    assert decide(drones=38 - EXPAND_WHEN_DRONES_WITHIN - 1, free_expansion=True).build == Build.NONE
    assert decide(drones=38 - EXPAND_WHEN_DRONES_WITHIN, free_expansion=True).build == Build.EXPAND


def test_no_expansion_without_a_free_base():
    assert decide(drones=38, free_expansion=False).build == Build.NONE


def test_one_expansion_at_a_time():
    assert decide(drones=38, free_expansion=True, bases_building=1).build == Build.NONE


def test_no_expansion_before_the_pool():
    obs = dict(pool_placed=False, pool_ordered=True, pool_ready=False, drones=38, free_expansion=True)
    assert decide(**obs).build == Build.NONE


def test_no_expansion_once_bases_hold_max_drones():
    assert decide(drones=MAX_DRONES, drone_capacity=MAX_DRONES, free_expansion=True).build == Build.NONE


def test_pool_before_expansion():
    obs = dict(pool_placed=False, pool_ordered=False, pool_ready=False, drones=20, drone_capacity=20)
    assert decide(**obs, free_expansion=True).build == Build.POOL


# --- spending targets ---


def test_drone_target_follows_capacity():
    assert decide(drone_capacity=44).drone_target == 44


def test_upgrades_wait_for_the_pool():
    assert decide(pool_placed=False, pool_ordered=False, pool_ready=False).upgrades == ()
    assert decide().upgrades == tuple(SCRIPTED_UPGRADES)


def test_queens_per_base():
    assert decide(bases=3).queens == 3 + CREEP_QUEENS


def test_spores_only_against_air():
    assert decide(enemy_has_air=False).spores_per_base == 0
    assert decide(enemy_has_air=True).spores_per_base == SPORES_PER_BASE


def test_army_is_zerglings():
    assert decide().army == {UnitTypeId.ZERGLING: 1.0}


# --- attack ---


def test_attack_hysteresis():
    rules = ScriptedDecisions()

    def attack(n: int, supply: int = 60) -> bool:
        return rules.decide(Observation(**BASE, own_units=lings(n), supply_used=supply)).attack

    assert not attack(ATTACK_AT_ZERGLINGS - 1)
    assert attack(ATTACK_AT_ZERGLINGS)
    assert attack(STOP_ATTACK_AT_ZERGLINGS)  # keeps attacking while above the stop threshold
    assert not attack(STOP_ATTACK_AT_ZERGLINGS - 1)
    assert not attack(STOP_ATTACK_AT_ZERGLINGS)  # and doesn't restart until the start threshold


def test_attack_when_maxed():
    assert decide(own_units=lings(0), supply_used=190).attack


# --- the Decision format ---


def test_decision_round_trip():
    d = decide(drones=38, free_expansion=True, enemy_has_air=True)
    assert Decision.from_dict(d.to_dict()) == d


def test_decision_vector():
    d = Decision(
        build=Build.EXPAND,
        drone_target=35,
        army={UnitTypeId.ZERGLING: 1.0},
        upgrades=(UpgradeId.ZERGMELEEWEAPONSLEVEL1, UpgradeId.ZERGLINGMOVEMENTSPEED),
        attack=True,
        queens=4,
        spores_per_base=1,
    )
    v = dict(zip(DECISION_NAMES, d.to_vector(), strict=True))
    assert len(DECISION_NAMES) == len(set(DECISION_NAMES))
    assert (v["build.NONE"], v["build.POOL"], v["build.EXPAND"]) == (0.0, 0.0, 1.0)
    assert v["drone_target"] == 35 / MAX_DRONES
    # first wish scores highest; unwanted upgrades are 0
    assert v["upgrades.ZERGMELEEWEAPONSLEVEL1"] > v["upgrades.ZERGLINGMOVEMENTSPEED"] > 0
    assert v["upgrades.ZERGGROUNDARMORSLEVEL2"] == 0.0
    assert (v["attack"], v["queens"], v["spores_per_base"]) == (1.0, 4.0, 1.0)


def test_scripted_upgrades_are_in_the_action_space():
    assert set(SCRIPTED_UPGRADES) <= set(UPGRADES)


def test_a_custom_policy_fits_the_interface():
    class AlwaysExpand:
        def decide(self, obs: Observation) -> Decision:
            return Decision(build=Build.EXPAND, drone_target=obs.drone_capacity)

    policy: Decisions = AlwaysExpand()
    assert policy.decide(Observation(drone_capacity=12)) == Decision(build=Build.EXPAND, drone_target=12)
