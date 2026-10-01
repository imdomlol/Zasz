from dataclasses import dataclass

from ares import AresBot
from ares.behaviors.macro import ExpansionController
from ares.managers.manager_mediator import ManagerMediator
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bot.actions.pathing import ExpansionAccess


def affordable_on_arrival(ai: AresBot, structure: UnitTypeId, target: Point2) -> bool:
    """Will we have the minerals for `structure` by the time the closest drone walks to `target`?

    Used to send builder drones just in time instead of idling at the build spot.
    """
    cost = ai.calculate_cost(structure).minerals
    if ai.minerals >= cost:
        return True
    drone = ai.workers.closest_to(target)
    path = ai.mediator.find_raw_path(
        start=drone.position, target=target, grid=ai.mediator.get_ground_grid, sensitivity=1
    )
    if path:
        distance = sum(a.distance_to(b) for a, b in zip([drone.position, *path], path, strict=False))
    else:
        # No ground path right now (e.g. the way is walled off); straight-line distance is close enough
        distance = drone.distance_to(target)
    travel_seconds = distance / drone.real_speed
    income_per_second = ai.state.score.collection_rate_minerals / 60
    return ai.minerals + income_per_second * travel_seconds >= cost


def next_expansion_location(ai: AresBot, access: ExpansionAccess) -> Point2 | None:
    """The base to take next: the closest (by path) free, safe expansion we can walk to.

    Same checks as ares' `ExpansionController`, plus reachability: a base whose way is blocked
    (by enemy structures, or rocks) is skipped for now, and `access.blocked` says why.
    """
    grid = ai.mediator.get_ground_grid
    for location, _distance in ai.mediator.get_own_expansions:
        if (
            ai.mediator.is_position_safe(grid=grid, position=location)
            and not ai.location_is_blocked(ai.mediator, location)
            and ai.mediator.can_place_structure(position=location, structure_type=ai.base_townhall_type)
            and access.is_reachable(ai, location)
        ):
            return location
    return None


@dataclass
class ExpandTo(ExpansionController):
    """ares' `ExpansionController`, but building at the location we chose (`next_expansion_location`)."""

    location: Point2 | None = None

    def _get_next_expansion_location(self, ai: AresBot, mediator: ManagerMediator) -> Point2 | None:
        return self.location
