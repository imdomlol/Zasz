"""The decision types: `Decision` (the action space), `Build`, `DECISION_NAMES` and the `Decisions`
interface. See `bot.decisions` for the rules every implementation follows."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

from bot.observation import Observation
from bot.settings import MAX_DRONES
from bot.spaces import ARMY_UNITS, UPGRADES


class Build(Enum):
    """The one thing to save up for this step (everything else waits while we do)."""

    NONE = "none"  # no saving: spend on drones and army
    POOL = "pool"  # the spawning pool
    EXPAND = "expand"  # the next base


@dataclass(frozen=True)
class Decision:
    build: Build = Build.NONE
    drone_target: int = 0  # make drones up to this many
    # Proportions over `ARMY_UNITS` (non-negative). Read-only: a policy that reuses and changes its
    # dict can't change a decision already made (or recorded)
    army: Mapping[UnitTypeId, float] = field(default_factory=dict)
    upgrades: tuple[UpgradeId, ...] = ()  # wish list from `UPGRADES`, highest priority first
    attack: bool = False  # attack, or stay home and defend
    queens: int = 0  # total queens wanted (needs a spawning pool)
    spores_per_base: int = 0  # spore crawlers in each mineral line

    def __post_init__(self) -> None:
        # Only what the fixed lists can express, so `to_dict`, `to_vector` and the actions agree on
        # what a decision contains
        if unknown := [unit for unit in self.army if unit not in ARMY_UNITS]:
            raise ValueError(f"army: {unknown} not in ARMY_UNITS (bot.spaces)")
        if bad := {unit: share for unit, share in self.army.items() if not (math.isfinite(share) and share >= 0)}:
            raise ValueError(f"army: shares must be finite and non-negative, got {bad}")
        if unknown := [u for u in self.upgrades if u not in UPGRADES]:
            raise ValueError(f"upgrades: {unknown} not in UPGRADES (bot.spaces)")
        if len(set(self.upgrades)) != len(self.upgrades):
            raise ValueError(f"upgrades: listed twice in {self.upgrades}")
        object.__setattr__(self, "army", MappingProxyType(dict(self.army)))
        object.__setattr__(self, "upgrades", tuple(self.upgrades))

    def to_dict(self) -> dict:
        """JSON-friendly and lossless (see `from_dict`)."""
        return {
            "build": self.build.value,
            "drone_target": self.drone_target,
            "army": {unit.name: share for unit, share in self.army.items()},
            "upgrades": [u.name for u in self.upgrades],
            "attack": self.attack,
            "queens": self.queens,
            "spores_per_base": self.spores_per_base,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Decision:
        """Inverse of `to_dict`. A missing field takes its default (a recording from before the field
        existed); an unknown one is an error (a renamed field would otherwise be dropped silently)."""
        if unknown := set(d) - {f.name for f in fields(cls)}:
            raise ValueError(f"unknown decision fields {sorted(unknown)}")
        default = cls()
        return cls(
            build=Build(d.get("build", default.build.value)),
            drone_target=d.get("drone_target", default.drone_target),
            army={UnitTypeId[name]: share for name, share in d.get("army", {}).items()},
            upgrades=tuple(UpgradeId[name] for name in d.get("upgrades", [])),
            attack=d.get("attack", default.attack),
            queens=d.get("queens", default.queens),
            spores_per_base=d.get("spores_per_base", default.spores_per_base),
        )

    def to_vector(self) -> list[float]:
        """Model output/label, in `DECISION_NAMES` order. Upgrades are encoded by priority:
        1.0 for the first wish, falling towards 0 down the list, 0 if not wanted."""
        build = [float(self.build == b) for b in Build]
        army = [float(self.army.get(unit, 0.0)) for unit in ARMY_UNITS]
        n = len(UPGRADES)
        rank = {u: i for i, u in enumerate(self.upgrades)}
        upgrades = [(n - rank[u]) / n if u in rank else 0.0 for u in UPGRADES]
        return [
            *build,
            self.drone_target / MAX_DRONES,
            *army,
            *upgrades,
            float(self.attack),
            float(self.queens),
            float(self.spores_per_base),
        ]


DECISION_NAMES: list[str] = [
    *(f"build.{b.name}" for b in Build),
    "drone_target",
    *(f"army.{u.name}" for u in ARMY_UNITS),
    *(f"upgrades.{u.name}" for u in UPGRADES),
    "attack",
    "queens",
    "spores_per_base",
]


class Decisions(Protocol):
    def decide(self, obs: Observation) -> Decision:
        """What to do this step. Called every step; may keep its own state between calls."""
        ...
