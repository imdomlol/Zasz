"""Who can hit whom, and who wins: weapon reach, damage, and the combat sim's verdict corrected where
it's wrong (sieged tanks we're on top of, bunkers). Plain functions of the units, used by every army
decision (`bot.army`, `bot.micro`)."""

from ares import AresBot
from ares.consts import IGNORE_IN_COST_DICT, EngagementResult, UnitTreeQueryType
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit

from bot.army_notes import COUNTS
from bot.settings import (
    BUNKER_MARINES,
    FIGHT_AT,
    MARINE_DPS,
    MARINE_HP,
    MARINE_RANGE,
    MEMORY_THREAT_MARGIN,
    TANK_NEUTRALIZED_BY,
    THREAT_LOOKUP,
    VISIBLE_THREAT_MARGIN,
    ZERGLING_DPS_VS_MARINE,
)
from bot.squads import fight_verdict
from bot.targeting import ReachMap, attack_capacity

ARMY_TYPES = {UnitTypeId.ZERGLING}


SIEGE_MIN_RANGE = 2.0  # sieged tanks can't fire at targets this close


IGNORED_THREATS = {UnitTypeId.LARVA, UnitTypeId.EGG, UnitTypeId.OVERLORD, UnitTypeId.OVERSEER}


def front_first(units: list[Unit], enemies: list[Unit]) -> list[Unit]:
    """Units closest to the enemy first, so they claim the target slots they're already at."""
    if not enemies:
        return units
    return sorted(units, key=lambda u: min(u.position.distance_to(e.position) for e in enemies))


def can_reach_soon(enemy: Unit, units: list[Unit], extra: float = 0.0) -> bool:
    """Within the enemy's weapon range plus a margin of one of these units: a margin for visible
    enemies (they can step in), a smaller one for remembered ones (last seen in the fog). Enemies
    farther away don't turn a squad back, so going *around* an army at a safe distance works."""
    margin = MEMORY_THREAT_MARGIN if enemy.is_memory else VISIBLE_THREAT_MARGIN
    reach = weapon_range(enemy) + enemy.radius + margin + extra
    return any(u.position.distance_to(enemy.position) - u.radius <= reach for u in units)


def enemies_near(ai: AresBot, units: list[Unit]) -> list[Unit]:
    """Enemy ground units/structures within THREAT_LOOKUP of any unit in the squad (as far as anything
    can hit us from: `can_reach_soon` then picks the ones that actually can).

    Includes ares' memory of units last seen in the fog (e.g. a sieged tank revealed by firing).
    Memory units aren't in python-sc2's per-frame distance cache, so always measure distances
    to enemies with positions (`a.position.distance_to(b.position)`), never `unit.distance_to(unit)`.
    """
    found: dict[int, Unit] = {}
    for group in ai.mediator.get_units_in_range(
        start_points=[u.position for u in units], distances=THREAT_LOOKUP, query_tree=UnitTreeQueryType.EnemyGround
    ):
        for e in group:
            if e.type_id not in IGNORED_THREATS:
                found[e.tag] = e
    return list(found.values())


def fight_result(
    ai: AresBot, units: list[Unit], threats: list[Unit], others: list[Unit] | None = None
) -> tuple[EngagementResult, list[Unit]]:
    """The combat sim's verdict for a fight against `threats`, and the army units of ours it counted:
    `units` plus `others`, the rest of the fight's members (`Army.members`: squads helping or fighting
    the same enemies, strikes on them, queens there), not whoever happens to be nearby."""
    mine = {u.tag for u in units}
    own = [u for u in (others or []) if u.tag not in mine] + list(units)
    return combat_estimate(ai, own, threats), own


def combat_estimate(
    ai: AresBot, own: list[Unit], threats: list[Unit], reach: ReachMap | None = None
) -> EngagementResult:
    """The combat sim's verdict for `own` against `threats`, corrected where it's wrong: a sieged tank we're
    already on top of can't shoot us (`tank_neutralized`), and a bunker shoots (`through_bunkers`, its spots
    counted on `reach` if given). Every fight, strike, raid and help judgement goes through this."""
    if not own:
        return EngagementResult.LOSS_EMPHATIC
    threats = [t for t in threats if not tank_neutralized(t, own)]
    if not threats:
        return EngagementResult.VICTORY_EMPHATIC
    bunkers = [t for t in threats if t.type_id == UnitTypeId.BUNKER]
    melee = [u for u in own if u.type_id in ARMY_TYPES]
    if bunkers and melee:
        return through_bunkers(ai, own, melee, bunkers, [t for t in threats if t not in bunkers], reach)
    return fight_verdict(ai, own, threats)


def wins(ai: AresBot, own: list[Unit], enemies: list[Unit], at: EngagementResult = FIGHT_AT) -> bool:
    """Do `own` beat `enemies` (at least `at`), by `combat_estimate`."""
    return combat_estimate(ai, own, enemies) >= at


def through_bunkers(
    ai: AresBot,
    own: list[Unit],
    melee: list[Unit],
    bunkers: list[Unit],
    others: list[Unit],
    reach: ReachMap | None = None,
) -> EngagementResult:
    """The sim doesn't know a bunker shoots (its marines are inside), so judge it in two steps: the
    zerglings that fit around the bunkers (the cells next to them we reach, given `reach`) kill them
    while the marines inside fire; then the survivors fight the marines that come out of the dead
    bunkers (BUNKER_MARINES each: we can't see inside) and whatever else is there."""
    probe = melee[0]
    attackers = min(
        len(melee),
        sum(
            reach.attack_slots(b, probe.radius, attack_capacity(probe, b) or len(melee), probe.ground_range)
            if reach is not None
            else (attack_capacity(probe, b) or len(melee))
            for b in bunkers
        ),
    )
    our_dps = attackers * max(probe.calculate_dps_vs_target(bunkers[0]), 0.1)
    seconds = sum(b.health + b.shield for b in bunkers) / our_dps
    lost = sum(dps_vs(b, probe) for b in bunkers) * seconds / max(probe.health_max, 1)
    COUNTS[f"bunker fight (lose ~{lost:.0f})"] += 1
    keep = max(0, len(melee) - int(lost + 0.5))
    survivors = sorted(melee, key=lambda u: u.health, reverse=True)[:keep]
    if not survivors:
        return EngagementResult.LOSS_OVERWHELMING
    # The marines that come out of the dead bunkers
    keep, marines_left = duel(keep, max(probe.health_max, 1), ZERGLING_DPS_VS_MARINE, BUNKER_MARINES * len(bunkers))
    COUNTS[f"bunker fight: marines after (we keep {keep})"] += 1
    if keep == 0:
        return (
            EngagementResult.LOSS_DECISIVE
            if marines_left > BUNKER_MARINES * len(bunkers) / 2
            else EngagementResult.LOSS_MARGINAL
        )
    survivors = survivors[:keep]
    if others:
        rest = [u for u in own if u.type_id not in ARMY_TYPES] + survivors
        return fight_verdict(ai, rest, others)
    share = keep / len(melee)  # what's left of us once the bunkers are dead
    if share >= 0.75:
        return EngagementResult.VICTORY_DECISIVE
    if share >= 0.5:
        return EngagementResult.VICTORY_CLOSE
    return EngagementResult.VICTORY_MARGINAL if share >= 0.25 else EngagementResult.LOSS_MARGINAL


def duel(zerglings: int, zergling_hp: float, zergling_dps: float, marines: int) -> tuple[int, int]:
    """Zerglings against marines in the open, one target at a time on each side (every zergling hits one
    marine, every marine shoots one zergling): (zerglings left, marines left)."""
    z_hp, m_hp = [zergling_hp] * zerglings, [float(MARINE_HP)] * marines
    for _ in range(600):  # 0.1 s steps, 60 s at most
        if not z_hp or not m_hp:
            break
        m_hp[0] -= len(z_hp) * zergling_dps * 0.1
        z_hp[0] -= len(m_hp) * MARINE_DPS * 0.1
        if m_hp[0] <= 0:
            m_hp.pop(0)
        if z_hp and z_hp[0] <= 0:
            z_hp.pop(0)
    return len(z_hp), len(m_hp)


def strike_result(ai: AresBot, own: list[Unit], group: list[Unit], reach: ReachMap | None = None) -> EngagementResult:
    """The verdict for `own` against `group` (`combat_estimate`)."""
    return combat_estimate(ai, own, group, reach)


def tank_neutralized(enemy: Unit, own: list[Unit]) -> bool:
    """A sieged tank with several of our zerglings inside its minimum range.

    ares' combat sim ignores minimum range, so it rates such a tank at full damage even though it
    can't shoot those zerglings and will die before it can unsiege. We leave it out of the sim
    (it's still first in the dive priority, so it keeps getting attacked).

    NOTE: a judgement call that might need reverting if it causes big mistakes. The tank can
    still shoot zerglings that aren't on top of it, and leaving it out also drops its health
    from the sim, so predictions get a little optimistic. Requiring TANK_NEUTRALIZED_BY
    zerglings in its minimum range limits this to tanks we're clearly already on top of.
    """
    if enemy.type_id != UnitTypeId.SIEGETANKSIEGED:
        return False
    reach = SIEGE_MIN_RANGE + enemy.radius
    inside = sum(
        1 for u in own if u.type_id in ARMY_TYPES and u.position.distance_to(enemy.position) < reach + u.radius
    )
    return inside >= TANK_NEUTRALIZED_BY


def production_cost(ai: AresBot, type_id: UnitTypeId) -> int | None:
    """Minerals + gas to make a unit of this type; None for types the game gives no production cost.
    ares lists spawned/free units (broodlings, locusts, ...), but other types (e.g. burrowed forms) have no
    creation ability either, and python-sc2's `calculate_cost` raises for them instead of returning zero:
    a benchmark game against Zerg crashed on it."""
    if type_id in IGNORE_IN_COST_DICT:
        return None
    try:
        cost = ai.calculate_cost(type_id)
    except (AttributeError, KeyError):
        return None
    return cost.minerals + cost.vespene


def armed(enemy: Unit) -> bool:
    """Can it shoot ground units: anything with a ground weapon, and a bunker (no weapon of its own,
    but the marines inside shoot from it)."""
    return enemy.can_attack_ground or enemy.type_id == UnitTypeId.BUNKER


def weapon_range(enemy: Unit) -> float:
    """Ground weapon range; a bunker's is its marines' plus one."""
    if enemy.type_id == UnitTypeId.BUNKER:
        return MARINE_RANGE + 1
    return enemy.ground_range


def dps_vs(enemy: Unit, target: Unit) -> float:
    """Damage per second `enemy` deals to `target`. An enemy bunker's cargo isn't visible to us, so
    it's assumed full: BUNKER_MARINES marines."""
    if enemy.type_id == UnitTypeId.BUNKER:
        return BUNKER_MARINES * MARINE_DPS
    return enemy.calculate_dps_vs_target(target)


def hit_fits(enemy: Unit, unit: Unit, amount: float) -> bool:
    """Could one attack (or one of its hits, e.g. a zealot's two blades) from `enemy` deal `amount`?"""
    try:
        damage = enemy.calculate_damage_vs_target(unit)[0]
    except Exception:  # noqa: BLE001 - no weapon data: can't tell, don't count it
        return False
    return damage > 0 and (abs(amount - damage) <= 2 or abs(amount - damage / 2) <= 2)


def center_of_points(points: list[Point2]) -> Point2:
    return Point2((sum(p.x for p in points) / len(points), sum(p.y for p in points) / len(points)))


def reach_of(threat: Unit, unit: Unit) -> float:
    """How close `unit` can be to `threat` before it can be hit (edge to edge plus weapon range)."""
    return weapon_range(threat) + threat.radius + unit.radius
