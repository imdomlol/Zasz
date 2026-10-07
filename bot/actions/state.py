"""What the scripted actions remember between steps, in one object the bot owns (`ZaszBot.action_state`)."""

from dataclasses import dataclass, field


@dataclass
class ActionState:
    injectors: dict[int, int] = field(default_factory=dict)  # hatchery tag -> its injecting queen's tag
    injector_of: dict[int, int] = field(default_factory=dict)  # last step's pairs (the switch log)
    spore_failed_at: dict[int, float] = field(default_factory=dict)  # hatchery tag -> when a spore failed there
