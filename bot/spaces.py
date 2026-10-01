"""The fixed lists that give observations and decisions a fixed shape.

A model is trained on vectors whose positions mean the same thing every time, so these lists
only ever grow at the end (and a model trained before a change has to be retrained after it).
Unit types, upgrades and races that aren't listed here simply aren't seen or chosen.
"""

from sc2.data import Race
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

# Our units the observation counts
OWN_UNIT_TYPES = [
    UnitTypeId.DRONE,
    UnitTypeId.OVERLORD,
    UnitTypeId.QUEEN,
    UnitTypeId.ZERGLING,
]

# Our structures the observation counts (including ones still being built)
OWN_STRUCTURE_TYPES = [
    UnitTypeId.HATCHERY,
    UnitTypeId.LAIR,
    UnitTypeId.HIVE,
    UnitTypeId.SPAWNINGPOOL,
    UnitTypeId.EXTRACTOR,
    UnitTypeId.EVOLUTIONCHAMBER,
    UnitTypeId.SPORECRAWLER,
    UnitTypeId.SPINECRAWLER,
]

# Army units a decision can ask for (as proportions of the army)
ARMY_UNITS = [
    UnitTypeId.ZERGLING,
]

# Upgrades a decision can ask for (as an ordered wish list), and that the observation tracks
UPGRADES = [
    UpgradeId.ZERGLINGMOVEMENTSPEED,
    UpgradeId.ZERGMELEEWEAPONSLEVEL1,
    UpgradeId.ZERGGROUNDARMORSLEVEL1,
    UpgradeId.OVERLORDSPEED,
    UpgradeId.ZERGMELEEWEAPONSLEVEL2,
    UpgradeId.ZERGGROUNDARMORSLEVEL2,
]

RACES = [Race.Terran, Race.Zerg, Race.Protoss, Race.Random]
