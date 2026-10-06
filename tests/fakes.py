"""Stand-ins for game units, shared by the offline tests: just the attributes the code reads.

Specialised fakes (a unit with a weapon cooldown, a bot with a combat sim) stay in their own test files.
"""

from types import SimpleNamespace

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

_tags = iter(range(1, 1_000_000))


def unit(x=0.0, y=0.0, *, radius=0.375, ground_range=0.1, air_range=0.0, flying=False, structure=False, **extra):
    return SimpleNamespace(
        tag=next(_tags),
        position=Point2((x, y)),
        radius=radius,
        ground_range=ground_range,
        air_range=air_range,
        is_flying=flying,
        is_structure=structure,
        order_target=None,
        **extra,
    )


def zergling(x=0.0, y=0.0, **extra):
    extra.setdefault("type_id", UnitTypeId.ZERGLING)
    return unit(x, y, radius=0.375, ground_range=0.1, **extra)


def mutalisk(x=0.0, y=0.0, **extra):
    return unit(x, y, radius=0.5, ground_range=3.0, air_range=3.0, flying=True, **extra)


def bay(x=0.0, y=0.0, **extra):
    return unit(x, y, radius=1.75, ground_range=0.0, structure=True, **extra)


def marine(x=0.0, y=0.0, **extra):
    extra.setdefault("type_id", UnitTypeId.MARINE)
    return unit(x, y, radius=0.375, ground_range=5.0, **extra)


def ultralisk(x=0.0, y=0.0, **extra):
    return unit(x, y, radius=1.0, ground_range=1.0, **extra)
