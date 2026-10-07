"""What each zergling does in a fight (`fight`): which target, from where, or stepping away; and the
moves the army uses to get around (`back_off`, `spread_move`, `move`). Squads and strikes decide *whether*
to fight (`bot.army`); this decides how, one zergling at a time, sharing target room through `TargetSlots`."""

from dataclasses import dataclass

from ares import AresBot
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.individual import AMove, AttackTarget, PathUnitToTarget, ShootTargetInRange
from ares.behaviors.combat.individual.combat_individual_behavior import CombatIndividualBehavior
from ares.consts import TOWNHALL_TYPES, WORKER_TYPES
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.army_notes import BACK_OFF_CIRCLES, CHOKE_NOTES, CHOKE_OPENERS, COUNTS, TARGET_NOTES, dump_reach, name_of, note
from bot.combat import ARMY_TYPES, armed, dps_vs, production_cost, reach_of, weapon_range
from bot.settings import (
    BACK_OFF_MARGIN,
    BREAKTHROUGH_RANGE,
    CHOKE_SLOTS,
    CLEAR_THE_WAY_RANGE,
    FLANK_DISTANCE,
    LEVEL_TOLERANCE,
    PLUG_HARM_RANGE,
    SAFE_WORK_MARGIN,
    SPREAD_ROW,
    SPREAD_SPACING,
    TARGET_FILL,
    VISIBLE_THREAT_MARGIN,
)
from bot.squads import center_of
from bot.targeting import BlockTracker, Detours, TargetSlots, attack_capacity, pick_tiered, spot_key

# Long-range or splash units zerglings should dive first rather than trade with from range
DIVE_FIRST = {UnitTypeId.SIEGETANKSIEGED, UnitTypeId.SIEGETANK, UnitTypeId.LURKERMPBURROWED, UnitTypeId.COLOSSUS}


# Structures worth killing first once their defenders are dead: economy, then power/production
PRIORITY_STRUCTURES = TOWNHALL_TYPES | {UnitTypeId.PYLON, UnitTypeId.SPAWNINGPOOL}


@dataclass
class MoveTo(CombatIndividualBehavior):
    """Plain move to a point (a step along our own path)."""

    unit: Unit
    target: Point2

    def execute(self, ai: AresBot, config: dict, mediator) -> bool:
        self.unit.move(self.target)
        return True


def fight(
    ai: AresBot,
    unit: Unit,
    threats: list[Unit],
    enemies: list[Unit],
    slots: TargetSlots,
    squad_position: Point2,
    blocks: BlockTracker,
    detours: Detours,
    diagnostics: bool = False,
) -> bool:
    """Target choice for one zergling, in priority tiers; it takes the best-tier target that still
    has room (`TargetSlots`: melee attackers per target are capped by the space around it):

    1. siege tanks and other DIVE_FIRST units close by, then any other close enemy that fights back
    2. workers
    3. key structures (town halls, pylons, pools), then any structure, once no fighters are close
       (so zerglings don't chew on a barracks while marines and tanks shoot them). Exception: when
       every close fighter is full or blocked, the structure physically in the way (within
       BREAKTHROUGH_RANGE) is next, so zerglings break through instead of dying in a lane

    When a target is full the next zergling takes the next one (a full pylon doesn't hold up the
    gateway next to it). Targets fill from the back (TARGET_FILL "back"): zerglings are handled
    front-first and each takes the *deepest* target with room, so the first ones in run past the
    front line (and up ramps) and later ones fill in toward the front, instead of everyone stopping
    at the first enemy and blocking the ones behind. A zergling only stops to hit its own target.

    A zergling that stops getting closer to an enemy unit (`BlockTracker`) first takes a detour to
    it around our own crowd (`Detours`, e.g. around the outside of a block of gateways instead of
    queueing at a 1-tile gap). With no reasonable detour (or for a structure) it takes another
    target it can reach, breaking through the structure in its way if that's all there is; a
    zergling breaking through switches back to the units as soon as one is reachable.
    """
    position = unit.position
    # Its own slot from last step is its to take again: release the reservation before any room check (the
    # "all work full: hold" check counted it as taken, so zerglings on the breach left it every other step)
    slots.assignments.release(unit)
    dive: list[Unit] = []
    in_the_way: list[Unit] = []
    covered: list[Unit] = []
    clear: list[Unit] = []
    key: list[Unit] = []
    fighters = [e for e in threats if e.type_id not in WORKER_TYPES]
    # Every fighter in this fight is a candidate, however far: whether to engage it (and with whom) was
    # decided for the group (the squad's sim verdict, a strike, a raid), not by each zergling. Plugs
    # are filtered out below; slots cap how many go for each
    near_fighters = list(fighters)
    workers = [e for e in threats if e.type_id in WORKER_TYPES]
    structures = [e for e in enemies if e.is_structure]
    # The structure worth breaking to get at enemy fighters behind it (`Army.breach_caps`): before
    # any other structure, with only as many attackers as can hit it at once
    breach = [s for s in structures if s.tag in slots.caps]
    if slots.reach is not None:
        # Only structures we can actually stand next to (not ones on top of a cliff from here)
        structures = [s for s in structures if slots.reach.reachable(s)]

        # Only enemy units we can surround (enough open cells reachable around them); fighting one
        # through a 1-tile gap is a 1-on-1 we lose. Exception: a ranged one right next to us, hit it
        # back (stepping away doesn't get us out of its range). A melee one plugging a gap (a zealot
        # between two gateways): step out of its reach instead, it gets surrounded once the
        # buildings next to it fall
        def touching(e: Unit) -> bool:
            return e.position.distance_to(position) - e.radius - unit.radius < 0.75

        plugs = slots.reach.plugs([e for e in fighters if not e.is_structure], fighters)
        # Danger: every cell an armed enemy can hit (its reach plus a margin; melee units included, so a
        # zealot in a gap makes the gateway faces next to it unsafe too)
        slots.reach.set_danger(
            [(e.position, reach_of(e, unit) + SAFE_WORK_MARGIN) for e in enemies if armed(e)], ai.time
        )

        def surroundable(e: Unit) -> bool:
            # Reachable, and not plugging a gap. A plug blocks our way to other enemy units (a zealot
            # between gateways or on a ramp, stalkers behind) and isn't hitting us from there (a melee
            # unit): fighting through it 1-on-1 loses. Anything else is fair game, slots capped by the
            # open cells around it (a clump of marines in the open, a marine shooting from a wall),
            # unless it's a choke fight not worth its cost (`choke_worth`)
            if slots.reach.open_sides(e, unit.radius) == 0:
                return False
            if e.tag in slots.assignments.deferred:
                return False  # a widening plan is opening more sides of it first (`Army.widen_plan`)
            if e.tag in plugs and weapon_range(e) < PLUG_HARM_RANGE:
                return False
            return choke_worth(ai, unit, e, slots, fighters, structures)

        pinned = [
            e
            for e in near_fighters
            if touching(e) and not surroundable(e) and not e.is_structure and e.ground_range <= 1
        ]
        if pinned:
            nearest = min(pinned, key=lambda e: e.position.distance_to(position))
            COUNTS["step away (gap)"] += 1
            why = "plug" if nearest.tag in plugs else "choke not worth it"
            if nearest.tag in CHOKE_OPENERS:
                why = f"open it up first: {CHOKE_OPENERS[nearest.tag]}"
            elif nearest.tag in CHOKE_NOTES:
                lost, worth, _ = CHOKE_NOTES[nearest.tag]
                why += f", lose ~{lost:.0f} vs worth {worth:.0f}"
            note([unit], f"step away from {name_of(nearest)} ({why})")
            unit.move(nearest.position.towards(position, nearest.radius + unit.radius + 2.0))
            return False
        near_fighters = [e for e in near_fighters if surroundable(e) or touching(e)]
        workers = [e for e in workers if slots.reach.reachable(e)]
    if near_fighters:
        dive = [e for e in near_fighters if e.type_id in DIVE_FIRST]
        # When every close fighter is full or blocked (e.g. zealots in 1-tile gaps between
        # gateways), break through the structure in the way rather than die in the lane
        in_the_way = safe_work(
            ai, slots, [s for s in structures if s.position.distance_to(position) - s.radius < BREAKTHROUGH_RANGE], unit
        )
        if not any(slots.has_room(unit, e) for e in near_fighters):
            # Every fighter full: the buildings only busy enemies can hit are free to work
            covered = covered_work(slots, structures, enemies, unit)
        tiers = [
            (dive, True),
            ([e for e in near_fighters if e not in dive], True),
            (workers, False),
            (breach, False),
            (in_the_way, False),
            (covered, False),
        ]
    elif any(e.type_id not in WORKER_TYPES for e in threats):
        # Fighters around but none we can reach: the breach target (inside their range, capped at its
        # slots), else the closest structures we can hit from outside their range
        # Any structure with a free safe cell we can walk to around the danger, however far: distance adds no
        # safety (a zergling parked down the ramp had "nothing to do" with every gateway 10+ tiles away).
        # Without a reach map (no safe cells), only the ones close by
        if slots.reach is not None:
            clear = safe_work(ai, slots, structures, unit)
        else:
            clear = safe_structures(
                ai, [s for s in structures if s.position.distance_to(position) - s.radius < CLEAR_THE_WAY_RANGE], unit
            )
        tiers = [(workers, False), (breach, False), (clear, False)]
    else:
        key = [s for s in structures if s.type_id in PRIORITY_STRUCTURES]
        tiers = [(fighters, True), (workers, True), (key, True), ([s for s in structures if s not in key], True)]
    tiers = [(t, back) for t, back in tiers if t]
    for rank, (tier, _) in enumerate(tiers, start=1):
        for e in tier:
            if e.tag not in TARGET_NOTES or TARGET_NOTES[e.tag][0] > rank:
                TARGET_NOTES[e.tag] = (rank, slots.capacity(unit, e))
    if not tiers and fighters and slots.reach is not None:
        # Diagnostic: why every enemy unit here was left out (memory = only remembered, not seen now)
        for e in fighters:
            mem = " (remembered)" if e.is_memory else ""
            if slots.reach.open_sides(e, unit.radius) == 0:
                COUNTS[f"nothing to do: {e.type_id.name.lower()} unreachable{mem}"] += 1
                dump_reach(ai, slots.reach, unit, fighters, enemies)
            elif e.tag in plugs and weapon_range(e) < PLUG_HARM_RANGE:
                COUNTS[f"nothing to do: {e.type_id.name.lower()} plug{mem}"] += 1
            else:
                COUNTS[f"nothing to do: {e.type_id.name.lower()} choke refused{mem}"] += 1
    if not tiers:
        # Nothing worth doing here: replace whatever order it still has (an attack picked earlier would
        # carry it on into their fire) with stepping out of their reach and waiting there
        note([unit], "nothing to do (no safe work, no breach): hold outside their reach")
        hold(ai, unit, threats)
        return False  # nothing to go after (e.g. only far-off remembered units, or nothing safe to break)
    if not near_fighters and fighters and not any(slots.has_room(unit, e) for t, _ in tiers for e in t):
        # Only structure work, and all of it full (the breach target saturated): wait just outside
        # their reach to take the place of a breach attacker that dies, instead of piling on
        COUNTS["hold for the breach"] += 1
        note([unit], "hold outside their reach (all work full)")
        back_off(ai, [unit], [e for e in threats if armed(e)], None)
        return True

    now = ai.time
    maneuver = CombatManeuver()
    # On a detour around our own crowd: keep following it to that target
    if (route_target := detours.route_target(unit, now)) is not None:
        goal = next((e for t, _ in tiers for e in t if e.tag == route_target), None)
        waypoint = detours.next_waypoint(unit) if goal is not None else None
        if goal is not None and waypoint is not None and not unit.target_in_range(goal, 0.25):
            COUNTS["detour follow"] += 1
            # Still going for it: it holds a slot there (its move order doesn't say so)
            slots.assignments.release(unit)
            slots.take(goal, unit)
            slots.assignments.assign(unit, goal, now)
            note([unit], f"detour to {name_of(goal)}")
            maneuver.add(ShootTargetInRange(unit=unit, targets=[goal]))
            maneuver.add(
                PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=waypoint, sense_danger=False)
            )
            ai.register_behavior(maneuver)
            return True
        detours.drop(unit)

    if TARGET_FILL == "off":
        goal = min((e for t, _ in tiers for e in t), key=lambda e: e.position.distance_to(position))
        flank = False
    else:
        depth_from = squad_position if TARGET_FILL == "back" else None
        goal, flank = pick_tiered(slots, blocks, unit, tiers, depth_from, now)
    if goal is None:
        note([unit], "no target picked: hold outside their reach")
        hold(ai, unit, threats)
        return False
    if goal.is_structure and goal.tag in slots.safe_cells and goal.tag not in slots.caps:
        # Safe work: stand on a safe cell next to it (walking there around their reach), hit it from there
        return work_from_cell(ai, unit, goal, slots, threats)
    if goal.is_structure and goal.tag in slots.caps and (done := work_breach_cell(ai, unit, goal, slots)) is not None:
        # The breach: walk to a cell next to it around the danger (only the last tiles are in their fire)
        return done
    if goal in covered:
        room = f"{slots.taken.get(goal.tag, 0)}/{len(slots.covered[goal.tag])}"
        if (done := work_spot(ai, unit, goal, slots, slots.covered[goal.tag], "covered work", room)) is not None:
            COUNTS["covered work"] += 1
            return done
    same_level = goal.is_flying or abs(ai.get_terrain_z_height(unit) - ai.get_terrain_z_height(goal)) < LEVEL_TOLERANCE
    if not flank and blocks.update(unit, goal, now, same_level):
        # Just got stuck on this one. An enemy unit: go around our own crowd to it if there's a
        # reasonable way; otherwise (or a structure) take another target we can reach
        crowd = [u for u in ai.units if u.tag != unit.tag and u.type_id in ARMY_TYPES and u.distance_to(unit) < 8]
        if not goal.is_structure and not goal.is_memory and detours.plan(ai, unit, goal, crowd, now):
            COUNTS["detour planned"] += 1
            return fight(ai, unit, threats, enemies, slots, squad_position, blocks, detours, diagnostics)
        COUNTS["blocked, repick"] += 1
        goal, flank = pick_tiered(slots, blocks, unit, tiers, None, now)
    if flank:
        category = "flank"
    elif goal.is_structure and near_fighters:
        category = "break through"
    elif goal.is_structure:
        category = "structure"
    else:
        category = "unit"
        if unit.target_in_range(goal, 0.25):
            COUNTS["unit in range"] += 1
    COUNTS[category] += 1
    # Only for the debug counts and the overlay's note: nothing decides on it
    unsafe = diagnostics and not ai.mediator.is_position_safe(grid=ai.mediator.get_ground_grid, position=position)
    if unsafe:
        COUNTS[f"UNSAFE {category}"] += 1
    if goal in dive:
        tier = "dive"
    elif goal in near_fighters:
        tier = "fighter"
    elif goal in workers:
        tier = "worker"
    elif goal in breach:
        tier = "breach"
    elif goal in in_the_way:
        tier = "break through (safe)"
    elif goal in covered:
        tier = "covered work"
    elif goal in clear:
        tier = "clear the way (safe)"
    elif goal in key:
        tier = "key structure"
    else:
        tier = "target"
    capacity = slots.capacity(unit, goal)
    room = f" {slots.taken.get(goal.tag, 0)}/{'-' if capacity is None else capacity}"
    note([unit], f"{category}: {name_of(goal)} [{tier}{room}]{' IN RANGE' if unsafe else ''}")

    if flank and not goal.is_memory:
        side = 1 if unit.tag % 2 else -1  # alternate sides so they spread around the jam
        away = goal.position.towards(unit.position, 1) - goal.position
        around = goal.position + Point2((-away.y, away.x)) * side * (goal.radius + FLANK_DISTANCE)
        maneuver.add(ShootTargetInRange(unit=unit, targets=[e for t, _ in tiers for e in t]))
        maneuver.add(PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=around, sense_danger=False))
        ai.register_behavior(maneuver)
        return True
    # Only stop for *our* target: stopping at whatever we brush past blocks everyone behind
    maneuver.add(ShootTargetInRange(unit=unit, targets=[goal]))
    # An enemy unit: walk around the other enemy units to it (e.g. around the outside of a gateway
    # grid instead of down a lane a zealot plugs), then attack it
    if (
        not goal.is_structure
        and not goal.is_memory
        and slots.reach is not None
        and not unit.target_in_range(goal, 0.5)
        and (step := slots.reach.approach_step(goal, position)) is not None
    ):
        COUNTS["approach around"] += 1
        maneuver.add(MoveTo(unit=unit, target=step))
        ai.register_behavior(maneuver)
        return True
    if chase := chase_behavior(unit, goal):
        maneuver.add(chase)
    ai.register_behavior(maneuver)
    return True


def choke_worth(
    ai: AresBot, unit: Unit, target: Unit, slots: TargetSlots, fighters: list[Unit], structures: list[Unit]
) -> bool:
    """Whether to attack a melee unit that isn't in the open (a zealot in a lane or on a ramp).

    First: while a building touching it can be worked from safe cells, open it up instead. Breaking that
    building costs nothing and gives the unit more open sides, so the fight gets cheaper. Then, with room
    for at most CHOKE_SLOTS attackers, it's taken only if what we'd lose killing it that slowly is worth
    it: zerglings lost = the damage of everything covering its attack cells (itself included) over the
    time that many zerglings take to kill it, against its cost in zerglings. Worked out once per target
    per step (`CHOKE_NOTES`)."""
    if target.is_structure or weapon_range(target) >= PLUG_HARM_RANGE:
        # Only a unit that can't hit us from where it stands (a zealot) can be left alone: refusing a
        # stalker in a gap doesn't stop it shooting us
        return True
    # Units the game puts no price on (spawned ones like broodlings, burrowed forms, ...) aren't worth losing
    # zerglings to in a choke
    if (value := production_cost(ai, target.type_id)) is None:
        return False
    if target.tag in CHOKE_NOTES:
        return CHOKE_NOTES[target.tag][2]
    capacity = slots.capacity(unit, target)
    full = attack_capacity(unit, target)
    if (
        capacity is not None
        and full is not None
        and capacity < full
        and slots.reach is not None
        and slots.reach.danger_stamp is not None
    ):
        # Partly enclosed: a building next to it we can break without taking fire comes first
        opener = next(
            (
                s
                for s in structures
                if s.position.distance_to(target.position) - s.radius - target.radius < 1.5
                and slots.reach.safe_spot_count(s, unit.radius, unit.ground_range) > 0
            ),
            None,
        )
        if opener is not None:
            CHOKE_NOTES[target.tag] = (0.0, 0.0, False)
            CHOKE_OPENERS[target.tag] = name_of(opener)
            COUNTS["choke: open it up first"] += 1
            return False
    if capacity is None or capacity > CHOKE_SLOTS:
        return True
    seconds = (target.health + target.shield) / (capacity * max(unit.calculate_dps_vs_target(target), 0.1))
    cells = slots.reach.attack_points(target, unit.radius) if slots.reach is not None else []
    if cells:
        # Whoever can hit the cells our attackers would stand on (weapon reach to those cells)
        covering = [
            f
            for f in fighters
            if armed(f)
            and any(
                f.position.distance_to(c) <= weapon_range(f) + f.radius + unit.radius + VISIBLE_THREAT_MARGIN
                for c in cells
            )
        ]
    else:
        covering = [
            f
            for f in fighters
            if armed(f) and f.position.distance_to(target.position) - target.radius - f.radius < weapon_range(f) + 1.5
        ]
    if target not in covering and armed(target):
        covering.append(target)
    lost = sum(dps_vs(f, unit) for f in covering) * seconds / max(unit.health_max, 1)
    worth = value / 25  # a zergling costs 25
    taken = lost <= worth
    CHOKE_NOTES[target.tag] = (lost, worth, taken)
    COUNTS["choke: taken" if taken else "choke: not worth it"] += 1
    return taken


def hold(ai: AresBot, unit: Unit, threats: list[Unit]) -> None:
    """Step out of the threats' reach and wait there (cancelling any older order)."""
    if threats:
        back_off(ai, [unit], threats, None)
    elif unit.orders:
        unit.stop()


def covered_work(slots: TargetSlots, structures: list[Unit], enemies: list[Unit], unit: Unit) -> list[Unit]:
    """Buildings that are "conditionally free" while every fighter is full: free spots next to them in
    reach only of enemies already busy with our attackers (`TargetSlots.busy`). Those enemies are shooting
    the zerglings on them and can't shoot both, so an extra zergling here costs nothing more: it hits the bays
    beside a full bunker, or the gateways next to zealots being fought, instead of queuing in their fire.
    Within CLEAR_THE_WAY_RANGE, closest first; the spots go in `TargetSlots.covered`. Which enemies reach
    each spot is worked out once per step (`ReachMap.step_cache`); per zergling only who's busy is checked."""
    reach = slots.reach
    if reach is None:
        return []
    found = []
    for s in structures:
        if armed(s) or s.tag in slots.caps or s.position.distance_to(unit.position) - s.radius > CLEAR_THE_WAY_RANGE:
            continue
        key = ("covering", s.tag, unit.radius, unit.ground_range)
        covering = reach.step_cache.get(key)
        if covering is None:
            armed_enemies = [e for e in enemies if armed(e) and e.type_id not in WORKER_TYPES]
            covering = [
                (p, [e.tag for e in armed_enemies if p.distance_to(e.position) <= reach_of(e, unit)])
                for p in reach.attack_spots(s, unit.radius, unit.ground_range)
            ]
            reach.step_cache[key] = covering
        spots = [p for p, tags in covering if spot_key(p) not in slots.cells_taken and all(slots.busy(t) for t in tags)]
        if spots:
            slots.covered[s.tag] = spots
            found.append(s)
    return sorted(found, key=lambda s: s.position.distance_to(unit.position))


def safe_work(ai: AresBot, slots: TargetSlots, structures: list[Unit], unit: Unit) -> list[Unit]:
    """The structures this zergling can work without taking fire: ones with cells next to them outside
    every enemy's reach (`ReachMap.set_danger`), on the same patch of safe ground as the zergling (so
    it can walk there around the danger). Each takes as many zerglings as it has such cells
    (`TargetSlots.safe_cells`). Closest first. Without a reach map: `safe_structures`."""
    if slots.reach is None or slots.reach.danger_stamp is None:
        return safe_structures(ai, structures, unit)
    area = slots.reach.safe_area(unit.position)
    found = []
    for s in structures:
        cells = slots.reach.safe_attack_spots(s, unit.radius, area, unit.ground_range)
        if cells:
            slots.safe_cells[s.tag] = cells
            found.append(s)
    return sorted(found, key=lambda s: s.position.distance_to(unit.position))


def work_from_cell(ai: AresBot, unit: Unit, goal: Unit, slots: TargetSlots, threats: list[Unit]) -> bool:
    """Hit `goal` from a safe spot next to it (`ReachMap.attack_spots`), the closest free one; walk there
    around the danger. No free spot or no safe way: hold outside their reach."""
    reach = slots.reach
    free = [p for p in slots.safe_cells.get(goal.tag, []) if spot_key(p) not in slots.cells_taken]
    if not free:
        note([unit], f"hold: no free safe cell at {name_of(goal)}")
        hold(ai, unit, threats)
        return True
    spot = min(free, key=lambda p: p.distance_to(unit.position))
    slots.cells_taken.add(spot_key(spot))
    cell = reach.cell(spot)
    capacity = len(slots.safe_cells[goal.tag])
    room = f"{slots.taken.get(goal.tag, 0)}/{capacity}"
    if reach.safe_area(unit.position) and unit.target_in_range(goal, 0.05):
        note([unit], f"safe work: {name_of(goal)} [{room}] hitting it")
        if not (unit.is_attacking and unit.order_target == goal.tag):
            unit.attack(goal)
        return True
    step = reach.safe_step(cell, unit.position)
    if step is None:
        note([unit], f"hold: no safe way to {name_of(goal)}")
        hold(ai, unit, threats)
        return True
    note([unit], f"safe work: {name_of(goal)} [{room}] walking to its cell")
    unit.move(step if step.distance_to(unit.position) > 0.3 else spot)
    return True


def work_breach_cell(ai: AresBot, unit: Unit, goal: Unit, slots: TargetSlots) -> bool | None:
    """Hit the breach target from a spot next to it (`ReachMap.attack_spots`), see `work_spot`. None when
    that can't be worked out (no reach map or danger, no free spot, no way): plain attack instead."""
    reach = slots.reach
    if reach is None or reach.danger_stamp is None:
        COUNTS["breach walk: no reach map"] += 1
        return None
    room = f"{slots.taken.get(goal.tag, 0)}/{slots.caps.get(goal.tag, '-')}"
    return work_spot(ai, unit, goal, slots, reach.attack_spots(goal, unit.radius, unit.ground_range), "breach", room)


def work_spot(
    ai: AresBot, unit: Unit, goal: Unit, slots: TargetSlots, spots: list[Point2], what: str, room: str
) -> bool | None:
    """Hit `goal` from one of `spots` next to it: the one it's on if free, else a free one (out of danger
    first, then closest), walked to around the danger except for the last tiles. None when there's no free
    spot or no way there (the caller falls back to a plain attack)."""
    reach = slots.reach
    spots = [p for p in spots if spot_key(p) not in slots.cells_taken]
    if not spots:
        COUNTS[f"{what} walk: no free spot"] += 1
        return None
    nearest = min(spots, key=lambda p: p.distance_to(unit.position))
    spot = (
        nearest
        if nearest.distance_to(unit.position) < unit.radius
        else min(spots, key=lambda p: (reach.in_danger(p), p.distance_to(unit.position)))
    )
    if unit.target_in_range(goal, 0.05):
        slots.cells_taken.add(spot_key(nearest))
        note([unit], f"{what}: {name_of(goal)} [{room}] hitting it")
        if not (unit.is_attacking and unit.order_target == goal.tag):
            unit.attack(goal)
        return True
    step = reach.breach_step(reach.cell(spot), unit.position)
    if step is None:
        COUNTS[f"{what} walk: no way around danger"] += 1
        return None
    slots.cells_taken.add(spot_key(spot))
    note([unit], f"{what}: {name_of(goal)} [{room}] walking to its spot")
    unit.move(step if step.distance_to(unit.position) > 0.3 else spot)
    return True


def safe_structures(ai: AresBot, structures: list[Unit], unit: Unit) -> list[Unit]:
    """Only the structures we can hit from outside known enemy weapon range, closest first. Inside
    enemy range the one structure worth working is the breach target (`Army.breach_caps`); anything
    else there is zerglings dying for nothing."""
    grid = ai.mediator.get_ground_grid
    safe = [
        s
        for s in structures
        if ai.mediator.is_position_safe(
            grid=grid, position=s.position.towards(unit.position, s.radius + unit.radius + 0.2)
        )
    ]
    return sorted(safe, key=lambda s: s.position.distance_to(unit.position))


def chase_behavior(unit: Unit, goal: Unit) -> AttackTarget | AMove | None:
    """Go attack `goal`. Visible units get a direct attack order (only re-issued if the zergling
    isn't already on it). Remembered units in the fog can't be targeted directly, so attack-move
    to their last known spot; `success_at_distance=0` because AMove's default does nothing within
    7 of the target, which left zerglings stuck on their previous target."""
    if goal.is_memory:
        return AMove(unit=unit, target=goal.position, success_at_distance=0)
    if unit.order_target == goal.tag:
        return None
    return AttackTarget(unit=unit, target=goal)


def back_off(
    ai: AresBot, units: list[Unit], threats: list[Unit], hold_at: Point2 | None, retreat: bool = False
) -> None:
    """Get out of the threats' actual weapon range (not ares' danger map, which pads every range by
    4 and sent zerglings all the way home), then stop: at `hold_at` if given (a rally), else right
    there. Units already out of reach wait."""
    if not threats:
        for unit in units:
            if unit.orders:
                unit.stop()
        return
    shooters = [t for t in threats if armed(t)]
    center = center_of(threats)
    probe = units[0]
    if retreat:  # (overlay: only retreats draw the line, and only for enemies close to the units)
        BACK_OFF_CIRCLES.extend(
            (t.position, reach_of(t, probe) + BACK_OFF_MARGIN)
            for t in shooters
            if any(u.position.distance_to(t.position) < reach_of(t, probe) + BACK_OFF_MARGIN + 3 for u in units)
        )
    for unit in units:
        exposed = [t for t in shooters if unit.position.distance_to(t.position) < reach_of(t, unit) + BACK_OFF_MARGIN]
        if exposed:
            # Straight away from the closest one that can hit it, to just past its reach
            t = min(exposed, key=lambda e: unit.position.distance_to(e.position) - reach_of(e, unit))
            away = t.position.towards(unit.position, reach_of(t, unit) + BACK_OFF_MARGIN + 1)
            if unit.position.distance_to(center) < 0.5:
                away = center.towards(rally_point(ai), reach_of(t, unit) + BACK_OFF_MARGIN + 1)
            unit.move(away)
        elif hold_at is not None and unit.distance_to(hold_at) > 3:
            move(ai, unit, hold_at)
        elif unit.orders:
            unit.stop()


def spread_move(ai: AresBot, units: list[Unit], center: Point2, target: Point2) -> None:
    """Move toward `target` in an open formation: rows of up to SPREAD_ROW across the direction of
    travel, SPREAD_SPACING apart, anchored on the next point of the path (around known enemy fire),
    each unit taking the slot on its own side so they don't cross over."""
    anchor = ai.mediator.find_path_next_point(
        start=center, target=target, grid=ai.mediator.get_ground_grid, sensitivity=8, sense_danger=False
    )
    heading = anchor - center if anchor.distance_to(center) > 1 else target - center
    length = max((heading.x**2 + heading.y**2) ** 0.5, 0.01)
    forward = Point2((heading.x / length, heading.y / length))
    side = Point2((-forward.y, forward.x))
    slots = []
    for i in range(len(units)):
        row, col = divmod(i, SPREAD_ROW)
        in_row = min(SPREAD_ROW, len(units) - row * SPREAD_ROW)
        lateral = (col - (in_row - 1) / 2) * SPREAD_SPACING
        slots.append(anchor + side * lateral - forward * (row * SPREAD_SPACING))

    def across(p: Point2) -> float:
        return (p.x - center.x) * side.x + (p.y - center.y) * side.y

    for unit, slot in zip(sorted(units, key=lambda u: across(u.position)), sorted(slots, key=across), strict=True):
        unit.move(slot if ai.in_pathing_grid(slot) else anchor)


def move(ai: AresBot, unit: Unit, target: Point2) -> None:
    """Move along ares' path, avoiding known enemy fire."""
    ai.register_behavior(PathUnitToTarget(unit=unit, grid=ai.mediator.get_ground_grid, target=target))


def rally_point(ai: AresBot) -> Point2:
    """In front of our base closest to the enemy."""
    base = ai.townhalls.closest_to(ai.enemy_start_locations[0])
    return base.position.towards(ai.game_info.map_center, 6)


def all_in(ai: AresBot) -> None:
    """No hatchery left: throw everything at the enemy."""
    for unit in ai.units.exclude_type({UnitTypeId.EGG, UnitTypeId.LARVA}):
        unit.attack(ai.enemy_start_locations[0])
