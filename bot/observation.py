"""What decisions get to see: one `observe(ai)` shared by the scripted rules, a learned model and
the decision log.

An `Observation` is a flat set of numbers with a fixed shape (see `bot.spaces`), so it can be
logged, replayed and turned into a model input (`to_vector`) without touching the game. Decisions
take an `Observation`, never the bot, which keeps them testable offline and swappable.

To give decisions more information, add a field here (with a default) and fill it in `observe`.
New features go at the end, so recorded data stays readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING

from ares.consts import TOWNHALL_TYPES
from sc2.data import Race
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

from bot.spaces import OWN_STRUCTURE_TYPES, OWN_UNIT_TYPES, RACES, UPGRADES

if TYPE_CHECKING:
    from bot.zasz import ZaszBot


def _counts(keys: list) -> dict:
    """A field holding one count per entry of a fixed list (flattened as `name.KEY`)."""
    return field(default_factory=dict, metadata={"keys": keys})


@dataclass(frozen=True)
class Observation:
    # Time and economy
    game_time: float = 0.0  # seconds
    minerals: int = 0
    vespene: int = 0
    mineral_income: float = 0.0  # per minute (the game's own collection rate)
    vespene_income: float = 0.0
    supply_used: int = 0
    supply_cap: int = 0
    larva: int = 0
    drones: int = 0  # including drones in eggs
    drone_capacity: int = 0  # drones our bases can use: 2 per mineral patch + gas (`drone_target`)

    # Bases and tech
    bases: int = 0  # hatcheries/lairs/hives, including ones being built
    bases_building: int = 0
    free_expansion: bool = False  # a free, safe expansion we can walk to exists
    pool_placed: bool = False  # spawning pool on the map (building or done)
    pool_ordered: bool = False  # placed, or a drone is on its way to place it
    pool_ready: bool = False

    # Our forces, upgrades and structures
    own_units: dict[UnitTypeId, int] = _counts(OWN_UNIT_TYPES)
    own_structures: dict[UnitTypeId, int] = _counts(OWN_STRUCTURE_TYPES)
    upgrades_done: dict[UpgradeId, int] = _counts(UPGRADES)  # 1 = researched

    # What we know about the enemy (seen now or remembered)
    enemy_race: dict[Race, int] = _counts(RACES)  # one-hot
    enemy_army_supply: float = 0.0
    enemy_workers: int = 0
    enemy_bases: int = 0
    enemy_structures: int = 0
    enemy_has_air: bool = False  # air units or air tech seen

    def __post_init__(self) -> None:
        # Count fields always hold every key of their fixed list (missing ones are 0), so there's
        # one representation, e.g. `Observation(own_units={ZERGLING: 20})` in tests
        for f in fields(self):
            if keys := f.metadata.get("keys"):
                given = getattr(self, f.name)
                unknown = set(given) - set(keys)
                if unknown:
                    raise ValueError(f"{f.name}: {unknown} not in the fixed list (bot.spaces)")
                object.__setattr__(self, f.name, {key: given.get(key, 0) for key in keys})

    def to_dict(self) -> dict[str, float]:
        """Flat `{feature name: number}`, always with the same names in the same order."""
        flat: dict[str, float] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if keys := f.metadata.get("keys"):
                for key in keys:
                    flat[f"{f.name}.{key.name}"] = value.get(key, 0)
            else:
                flat[f.name] = float(value) if isinstance(value, bool) else value
        return flat

    def to_vector(self) -> list[float]:
        """Model input: `to_dict()` values in `FEATURE_NAMES` order."""
        return [float(v) for v in self.to_dict().values()]

    @classmethod
    def from_dict(cls, flat: dict[str, float]) -> Observation:
        """Inverse of `to_dict`. Missing features take their default (older recordings)."""
        kwargs = {}
        for f in fields(cls):
            if keys := f.metadata.get("keys"):
                kwargs[f.name] = {key: int(flat.get(f"{f.name}.{key.name}", 0)) for key in keys}
            elif f.name in flat:
                kwargs[f.name] = type(f.default)(flat[f.name])  # bool / int / float, like the default
        return cls(**kwargs)


FEATURE_NAMES: list[str] = list(Observation().to_dict())


def observe(ai: ZaszBot) -> Observation:
    """Everything decisions get to see this step."""
    from bot.actions.building import next_expansion_location
    from bot.actions.defense import enemy_has_air
    from bot.actions.drones import drone_target

    pool = ai.structures(UnitTypeId.SPAWNINGPOOL)
    enemy_army = ai.mediator.get_cached_enemy_army
    return Observation(
        game_time=ai.time,
        minerals=ai.minerals,
        vespene=ai.vespene,
        mineral_income=ai.state.score.collection_rate_minerals,
        vespene_income=ai.state.score.collection_rate_vespene,
        supply_used=int(ai.supply_used),
        supply_cap=int(ai.supply_cap),
        larva=ai.larva.amount,
        drones=int(ai.supply_workers + ai.already_pending(UnitTypeId.DRONE)),
        drone_capacity=drone_target(ai),
        bases=ai.townhalls.amount,
        bases_building=ai.townhalls.not_ready.amount,
        free_expansion=next_expansion_location(ai, ai.expansions) is not None,
        pool_placed=bool(pool),
        pool_ordered=bool(pool) or bool(ai.already_pending(UnitTypeId.SPAWNINGPOOL)),
        pool_ready=bool(pool.ready),
        own_units={t: ai.units(t).amount for t in OWN_UNIT_TYPES},
        own_structures={t: ai.structures(t).amount for t in OWN_STRUCTURE_TYPES},
        upgrades_done={u: int(u in ai.state.upgrades) for u in UPGRADES},
        enemy_race={r: int(ai.enemy_race == r) for r in RACES},
        enemy_army_supply=float(sum(ai.calculate_supply_cost(u.type_id) for u in enemy_army)),
        enemy_workers=ai.mediator.get_cached_enemy_workers.amount,
        enemy_bases=ai.enemy_structures.of_type(TOWNHALL_TYPES).amount,
        enemy_structures=ai.enemy_structures.amount,
        enemy_has_air=enemy_has_air(ai),
    )
