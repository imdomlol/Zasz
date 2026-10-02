from sc2.ids.unit_typeid import UnitTypeId

from bot.decisions import Build, Decision
from bot.observation import Observation
from bot.settings import (
    ARMY_COMPOSITION,
    ATTACK_AT_ZERGLINGS,
    CREEP_QUEENS,
    EXPAND_WHEN_DRONES_WITHIN,
    MAX_DRONES,
    POOL_AT_DRONES,
    SCRIPTED_UPGRADES,
    SPORES_PER_BASE,
    STOP_ATTACK_AT_ZERGLINGS,
)


class ScriptedDecisions:
    """Hand-written rules: 16 pool, ling speed, expand as drones saturate, mass zerglings, attack at 16."""

    def __init__(self) -> None:
        self.attacking = False

    def decide(self, obs: Observation) -> Decision:
        if self.pool_wanted(obs):
            build = Build.POOL
        elif self.expansion_wanted(obs) and obs.free_expansion:
            build = Build.EXPAND
        else:
            build = Build.NONE
        return Decision(
            build=build,
            drone_target=obs.drone_capacity,
            army=dict(ARMY_COMPOSITION),
            # Wait for the pool, otherwise ares' UpgradeController would build its own
            upgrades=tuple(SCRIPTED_UPGRADES) if obs.pool_placed else (),
            attack=self.should_attack(obs),
            queens=obs.bases + CREEP_QUEENS,  # one injector per hatchery, plus creep queens
            spores_per_base=SPORES_PER_BASE if obs.enemy_has_air else 0,
        )

    def should_attack(self, obs: Observation) -> bool:
        """Start at ATTACK_AT_ZERGLINGS, keep going until the army drops below
        STOP_ATTACK_AT_ZERGLINGS. The gap stops the army flip-flopping between attacking and
        pulling back each time a few zerglings die or hatch. An attack with no known enemy army left
        keeps going with whatever is left: undefended buildings don't need 8 zerglings."""
        lings = obs.own_units.get(UnitTypeId.ZERGLING, 0)
        if lings >= ATTACK_AT_ZERGLINGS or obs.supply_used >= 190:
            self.attacking = True
        elif lings < STOP_ATTACK_AT_ZERGLINGS and not (self.attacking and lings and obs.enemy_army_supply == 0):
            self.attacking = False
        return self.attacking

    # A drone still walking to the build spot counts as `pool_ordered`, so these stay True
    # (and keep saving minerals) until the building is placed

    def pool_wanted(self, obs: Observation) -> bool:
        return not obs.pool_ordered and obs.drones >= POOL_AT_DRONES

    def expansion_wanted(self, obs: Observation) -> bool:
        """Expand when our drones are about to fill every base we have.

        `drone_capacity` only counts mineral patches that still exist, so this also triggers
        when patches mine out and drones have nowhere left to mine.
        """
        # One hatchery at a time; the pool comes first
        if not obs.pool_placed or obs.bases_building:
            return False
        if obs.drone_capacity >= MAX_DRONES:
            return False  # we already have room for as many drones as we'll ever make
        return obs.drones >= obs.drone_capacity - EXPAND_WHEN_DRONES_WITHIN
