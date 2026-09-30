"""Decisions: what to do next (build order, when to expand, when to attack).

`ScriptedDecisions` is hand-written rules. A learned model can live next to it by
implementing the same `Decisions` interface, then be swapped in `ZaszBot.__init__`.
"""

from typing import Protocol

from ares import AresBot
from ares.behaviors.macro import MacroPlan

from bot.decisions.scripted import ScriptedDecisions


class Decisions(Protocol):
    def macro_plan(self, ai: AresBot) -> MacroPlan:
        """What to spend on this step, highest priority first."""
        ...

    def should_attack(self, ai: AresBot) -> bool: ...


__all__ = ["Decisions", "ScriptedDecisions"]
