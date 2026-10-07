"""Offline tests for building edge cases no drones left, and spores that can't be placed."""

import asyncio
from types import SimpleNamespace

from sc2.position import Point2

from bot.actions.building import affordable_on_arrival
from bot.actions.defense import build_spores
from bot.settings import SPORE_RETRY_SECONDS


class Group(list):
    @property
    def ready(self):
        return self

    @property
    def amount(self):
        return len(self)

    @property
    def center(self):
        return Point2((sum(u.position.x for u in self) / len(self), sum(u.position.y for u in self) / len(self)))

    def closer_than(self, distance, unit):
        return Group(u for u in self if u.position.distance_to(unit.position) < distance)


def thing(tag, x, y):
    return SimpleNamespace(tag=tag, position=Point2((x, y)))


def test_no_drones_and_short_of_minerals_is_not_affordable_and_doesnt_crash():
    ai = SimpleNamespace(minerals=100, workers=Group(), calculate_cost=lambda s: SimpleNamespace(minerals=300))
    assert affordable_on_arrival(ai, None, Point2((10, 10))) is False


def spore_ai(placeable):
    """Two hatcheries with minerals beside them; `placeable`: hatchery tags whose spore spot can be placed."""
    hatches = Group([thing(1, 20, 20), thing(2, 60, 60)])
    built, queries = [], []

    async def find_placement(structure, near, max_distance):
        queries.append(near)
        owner = min(hatches, key=lambda h: h.position.distance_to(near))
        return near if owner.tag in placeable else None

    return (
        SimpleNamespace(
            time=100.0,
            can_afford=lambda t: True,
            already_pending=lambda t: 0,
            structure_pending=lambda t: 0,
            structures=lambda t: Group(),
            townhalls=hatches,
            mineral_field=Group([thing(10, 26, 20), thing(11, 66, 60)]),
            find_placement=find_placement,
            mediator=SimpleNamespace(
                select_worker=lambda target_position: SimpleNamespace(tag=99),
                build_with_specific_worker=lambda worker, structure_type, pos: built.append(pos),
            ),
        ),
        built,
        queries,
    )


def test_a_base_whose_spore_cant_be_placed_doesnt_hold_up_the_next():
    ai, built, queries = spore_ai(placeable={2})
    asyncio.run(build_spores(ai, 1))
    assert len(built) == 1 and built[0].distance_to(Point2((60, 60))) < 5
    asyncio.run(build_spores(ai, 1))  # a step later: the first base isn't asked about again yet
    assert len(queries) == 3
    ai.time += SPORE_RETRY_SECONDS
    asyncio.run(build_spores(ai, 1))
    assert len(queries) == 5  # tried again after a while
