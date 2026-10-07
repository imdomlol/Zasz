"""Decisions: *what* to do next, as a function of what we can see.

    observe(ai) -> Observation -> Decisions.decide(obs) -> Decision -> actions carry it out

`ScriptedDecisions` is hand-written rules. A learned model implements the same `Decisions`
interface and is passed to `ZaszBot(decisions=...)`. The rules every implementation follows:

- `decide` only gets the `Observation` (never the bot), so it sees exactly what a model would
  see, can be tested offline and replayed from recordings (`bot.recording`).
- It returns a `Decision`: a fixed set of choices, with a fixed shape (`bot.spaces`), so a model
  has a fixed set of outputs. How each choice is carried out (worker choice, placement, timing,
  micro) stays scripted in `bot.actions` and `bot.army`.
- Any new *what* choice gets a field on `Decision` (and whatever it needs to see on
  `Observation`); don't make it inside the actions.
"""

from bot.decisions.scripted import ScriptedDecisions
from bot.decisions.types import DECISION_NAMES, Build, Decision, Decisions

__all__ = ["DECISION_NAMES", "Build", "Decision", "Decisions", "ScriptedDecisions"]
