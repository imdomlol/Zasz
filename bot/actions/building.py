from ares import AresBot
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2


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
    distance = sum(a.distance_to(b) for a, b in zip([drone.position, *path], path, strict=False))
    travel_seconds = distance / drone.real_speed
    income_per_second = ai.state.score.collection_rate_minerals / 60
    return ai.minerals + income_per_second * travel_seconds >= cost


def next_expansion_location(ai: AresBot) -> Point2 | None:
    """The base `ExpansionController` will take next: the closest (by path) free, safe expansion.

    Mirrors its checks so we aim the just-in-time timing at the right spot, and so we know
    when there is nowhere left to expand.
    """
    grid = ai.mediator.get_ground_grid
    for location, _distance in ai.mediator.get_own_expansions:
        if (
            ai.mediator.is_position_safe(grid=grid, position=location)
            and not ai.location_is_blocked(ai.mediator, location)
            and ai.mediator.can_place_structure(position=location, structure_type=ai.base_townhall_type)
        ):
            return location
    return None
