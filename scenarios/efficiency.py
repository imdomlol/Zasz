"""Damage efficiency (`bot/efficiency.py`): zerglings vs a cluster of structures in different layouts.

These measure rather than judge: a scenario passes when every structure is dead in time, and
its message (also appended to `benchmarks/efficiency.csv` with the commit) reports how much of
the zerglings' damage potential landed. Compare layouts with each other (ramp vs open field is the
ramp problem) and commits with each other.

Setup: enemy engineering bays are spawned (they can't lift off), then the enemy's command center
and SCVs are removed (no repair, no workers wandering in; the bays keep the game going), then our
zerglings appear and the bays are checked to be in place. Measuring starts when the zerglings
appear.
"""

import csv
from datetime import datetime
from pathlib import Path

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from scenarios import Outcome, Scenario, ScenarioBot, failed, passed

ROOT = Path(__file__).resolve().parent.parent
RESULTS_FILE = ROOT / "benchmarks" / "efficiency.csv"
STRUCTURE = UnitTypeId.ENGINEERINGBAY  # 3x3, can't lift off
# One more enemy building, so killing the layout doesn't end the game. A *flying* barracks:
# zerglings can't hit air, so it can never die early (a sensor tower got killed first when it
# happened to be the structure closest to the bot's rally point)
KEEP_ALIVE = UnitTypeId.BARRACKSFLYING
ZERGLINGS = 30
FIELDS = ["timestamp", "commit", "scenario", "kill_time", "dps", "efficiency", "blocked", "idle", "dealt", "taken"]


def current_commit() -> str:
    head = ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            return (ROOT / ".git" / ref[5:]).read_text().strip()[:7]
        return ref[:7]
    except OSError:
        return "none"


class StructureAssault(Scenario):
    """Base: spawn the layout, clear the enemy's workers and command center, then the zerglings."""

    time_limit = 90

    def __init__(self) -> None:
        self.phase = "spawn"
        self.spawned_at = 0.0
        self.fight_started = 0.0
        self.expected = 0

    def layout(self, bot: ScenarioBot) -> tuple[list[Point2], Point2]:
        """Structure positions and where the zerglings start."""
        raise NotImplementedError

    async def setup(self, bot: ScenarioBot) -> None:
        await bot.client.debug_show_map()
        await bot.client.debug_control_enemy()

    async def on_step(self, bot: ScenarioBot) -> None:
        # Spawn the layout first: removing the enemy's command center while it has no other
        # structure would end the game
        if self.phase == "spawn":
            positions, _ = self.layout(bot)
            self.expected = len(positions)
            for position in positions:
                await bot.spawn(STRUCTURE, position, enemy=True)
            await bot.spawn(KEEP_ALIVE, keep_alive_spot(bot, positions), enemy=True)
            self.spawned_at = bot.time
            self.phase = "clear"
        elif self.phase == "clear" and bot.time - self.spawned_at > 1:
            # Everything else the enemy has (command center, SCVs) goes: no repair, no workers
            keep = {STRUCTURE, KEEP_ALIVE}
            others = {u.tag for u in bot.enemy_units} | {s.tag for s in bot.enemy_structures if s.type_id not in keep}
            if others:
                await bot.client.debug_kill_unit(others)
            _, start = self.layout(bot)
            await bot.spawn(UnitTypeId.ZERGLING, start, amount=ZERGLINGS)
            self.phase = "zerglings"
        elif self.phase == "zerglings" and bot.units(UnitTypeId.ZERGLING):
            bot.damage.reset(bot.time)
            self.fight_started = bot.time
            self.phase = "fight"

    def check(self, bot: ScenarioBot) -> Outcome | None:
        if self.phase == "zerglings":
            built = bot.enemy_structures(STRUCTURE).amount
            if built != self.expected:
                return failed(f"setup: {built} of {self.expected} structures spawned")
            if not bot.enemy_structures(KEEP_ALIVE):
                return failed("setup: the keep-alive structure didn't spawn")
        if self.phase != "fight":
            return None
        if not bot.enemy_structures(STRUCTURE):
            return self.report(bot, passed, f"all {self.expected} structures dead in {self.elapsed(bot):.0f}s")
        if not bot.units(UnitTypeId.ZERGLING):
            return self.report(bot, failed, "all zerglings died")
        return None

    def on_timeout(self, bot: ScenarioBot) -> Outcome:
        left = bot.enemy_structures(STRUCTURE).amount
        return self.report(bot, failed, f"{left} of {self.expected} structures still alive")

    def elapsed(self, bot: ScenarioBot) -> float:
        return bot.time - self.fight_started

    def report(self, bot: ScenarioBot, verdict, text: str) -> Outcome:
        t = bot.damage.total
        side = "top" if bot.enemy_start_locations[0].y > bot.game_info.map_center.y else "bottom"
        text = f"[{side}] {text}"
        RESULTS_FILE.parent.mkdir(exist_ok=True)
        new_file = not RESULTS_FILE.exists()
        with RESULTS_FILE.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(
                {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "commit": current_commit(),
                    "scenario": self.name,
                    "kill_time": round(self.elapsed(bot), 1) if verdict is passed else "",
                    "dps": round(t.dealt_per_second, 1),
                    "efficiency": round(t.efficiency or 0, 3),
                    "blocked": round(t.share(t.blocked), 3),
                    "idle": round(t.share(t.idle), 3),
                    "dealt": round(t.damage_dealt),
                    "taken": round(t.damage_taken),
                }
            )
        return verdict(f"{text}; {t.summary()}")


def fits(bot: ScenarioBot, center: Point2, size: int = 3) -> bool:
    """Every cell of a size x size footprint centred here is buildable."""
    half = size / 2
    return all(
        bot.in_placement_grid(Point2((center.x - half + 0.5 + dx, center.y - half + 0.5 + dy)))
        for dx in range(size)
        for dy in range(size)
    )


def keep_alive_spot(bot: ScenarioBot, layout: list[Point2]) -> Point2:
    """The map corner farthest from our base, so the bot (which attacks the known structure
    closest to its rally point) never heads for it before the layout is dead."""
    area = bot.game_info.playable_area
    corners = [
        Point2((area.x + 3, area.y + 3)),
        Point2((area.x + area.width - 3, area.y + 3)),
        Point2((area.x + 3, area.y + area.height - 3)),
        Point2((area.x + area.width - 3, area.y + area.height - 3)),
    ]
    return max(corners, key=lambda p: p.distance_to(bot.start_location))


def placed_layout(bot: ScenarioBot, offsets: list[Point2], origin: Point2) -> list[Point2]:
    """The layout at the closest spot to `origin` where every structure fits (debug spawns that
    don't fit get moved or dropped)."""
    for radius in range(0, 20):
        for dx in range(-radius, radius + 1):
            for dy in (-radius, radius) if abs(dx) != radius else range(-radius, radius + 1):
                shifted = Point2((origin.x + dx, origin.y + dy))
                positions = [shifted + o for o in offsets]
                if all(fits(bot, p) for p in positions):
                    return positions
    return [origin + o for o in offsets]


def toward_us(bot: ScenarioBot, origin: Point2) -> Point2:
    """Unit vector from `origin` toward our main."""
    return origin.towards(bot.start_location, 1) - origin


def across(direction: Point2) -> Point2:
    return Point2((-direction.y, direction.x))


class OpenField(StructureAssault):
    name = "efficiency_open_field"
    description = "30 zerglings vs 6 engineering bays spread out on flat ground (the upper bound)"

    def layout(self, bot: ScenarioBot) -> tuple[list[Point2], Point2]:
        center = bot.game_info.map_center
        back, side = toward_us(bot, center), across(toward_us(bot, center))
        # 2 rows of 3, 2 tiles apart: room to surround every one
        positions = placed_layout(bot, [side * dx - back * dy for dx in (-5, 0, 5) for dy in (0, 5)], center)
        return positions, positions[0] + side * 5 + back * 12


class DeepBase(StructureAssault):
    name = "efficiency_deep_base"
    description = "30 zerglings vs 12 engineering bays packed 4 rows deep, 1-tile gaps (breadth-first spread)"

    def layout(self, bot: ScenarioBot) -> tuple[list[Point2], Point2]:
        center = bot.game_info.map_center
        back, side = toward_us(bot, center), across(toward_us(bot, center))
        positions = placed_layout(bot, [side * dx - back * dy for dx in (-4, 0, 4) for dy in (0, 4, 8, 12)], center)
        return positions, positions[0] + side * 4 + back * 12


class Ramp(StructureAssault):
    name = "efficiency_ramp"
    description = "30 zerglings below the enemy's main ramp vs 6 engineering bays at the top"

    def layout(self, bot: ScenarioBot) -> tuple[list[Point2], Point2]:
        ramp = bot.mediator.get_enemy_ramp
        top, bottom = ramp.top_center, ramp.bottom_center
        up = bottom.towards(top, 1) - bottom
        side = across(up)
        positions = [top + up * dy + side * dx for dx in (-4, 0, 4) for dy in (6, 10)]
        return positions, bottom - up * 8


class CrampedMain(StructureAssault):
    """A packed enemy main: a 5 wide x 3 deep grid of gateways with 1-tile gaps, zealots in the gaps
    of the back row and stalkers behind it, past the top of the enemy's main ramp. Zerglings have to
    squeeze through 1-tile lanes to reach the units; ones that stop at the first thing in reach
    jam the lanes, and ones that all pick the same few targets leave the rest untouched."""

    name = "efficiency_cramped_main"
    description = "48 zerglings up the ramp into a 5x3 gateway grid (1-tile gaps), zealots/stalkers at the back"
    time_limit = 180
    LINGS = 48
    ZEALOTS = 4
    COLUMNS = (-8, -4, 0, 4, 8)  # gateways are 3x3: 4 apart leaves a 1-tile gap
    ROWS = (6, 10, 14)  # depth past the ramp top

    def __init__(self) -> None:
        super().__init__()
        self.targets: set[int] = set()
        self.origin: Point2 | None = None

    def frame(self, bot: ScenarioBot) -> tuple[Point2, Point2, Point2, Point2]:
        """Ramp top, bottom, 'up' (into the main) and 'side' unit vectors, snapped to the map axes:
        a grid rotated with a diagonal ramp lands its 3x3 footprints on whole tiles unevenly and
        the 1-tile gaps close up into a solid wall."""
        ramp = bot.mediator.get_enemy_ramp
        top, bottom = ramp.top_center, ramp.bottom_center
        raw = bottom.towards(top, 1) - bottom
        up = Point2((1 if raw.x > 0 else -1, 0)) if abs(raw.x) >= abs(raw.y) else Point2((0, 1 if raw.y > 0 else -1))
        # 3x3 buildings sit on half-tile centers
        anchor = Point2((int(top.x) + 0.5, int(top.y) + 0.5))
        return anchor, bottom, up, across(up)

    def layout(self, bot: ScenarioBot) -> tuple[list[Point2], Point2]:
        top, bottom, up, side = self.frame(bot)
        offsets = [side * dx + up * dy for dy in self.ROWS for dx in self.COLUMNS]
        positions = placed_layout(bot, offsets, top)
        self.origin = positions[0] - offsets[0]
        return positions, bottom - up * 8

    def unit_spots(self, bot: ScenarioBot) -> tuple[list[Point2], list[Point2]]:
        """Zealots in the 1-tile gaps (back row first, then the middle row), stalkers behind the
        back row; only spots on walkable ground (a gap against a cliff edge would push the unit
        somewhere else entirely)."""
        _, _, up, side = self.frame(bot)
        back, middle = self.ROWS[-1], self.ROWS[-2]
        gaps = [self.origin + side * dx + up * back for dx in (-6, -2, 2, 6)]
        gaps += [self.origin + side * dx + up * middle for dx in (-2, 2, -6, 6)]  # center first
        zealots = [g for g in gaps if bot.in_pathing_grid(g)][: self.ZEALOTS]
        stalkers = []
        for dx in (-6, -2, 2, 6):
            for depth in (3, 2):
                spot = self.origin + side * dx + up * (back + depth)
                if bot.in_pathing_grid(spot):
                    stalkers.append(spot)
                    break
        return zealots, stalkers

    async def on_step(self, bot: ScenarioBot) -> None:
        if self.phase == "spawn":
            positions, _ = self.layout(bot)
            for position in positions:
                await bot.spawn(UnitTypeId.GATEWAY, position, enemy=True)
            zealots, stalkers = self.unit_spots(bot)
            for spot in zealots:
                await bot.spawn(UnitTypeId.ZEALOT, spot, enemy=True)
            for spot in stalkers:
                await bot.spawn(UnitTypeId.STALKER, spot, enemy=True)
            await bot.spawn(KEEP_ALIVE, keep_alive_spot(bot, positions), enemy=True)
            self.expected = len(positions)
            self.spawned_at = bot.time
            self.phase = "clear"
        elif self.phase == "clear" and bot.time - self.spawned_at > 1:
            keep = {UnitTypeId.GATEWAY, UnitTypeId.ZEALOT, UnitTypeId.STALKER, KEEP_ALIVE}
            others = {u.tag for u in bot.enemy_units if u.type_id not in keep}
            others |= {s.tag for s in bot.enemy_structures if s.type_id not in keep}
            if others:
                await bot.client.debug_kill_unit(others)
            _, start = self.layout(bot)
            await bot.spawn(UnitTypeId.ZERGLING, start, amount=self.LINGS)
            bot.army.debug = True  # log what the zerglings decide
            self.phase = "zerglings"
        elif self.phase == "zerglings" and bot.units(UnitTypeId.ZERGLING):
            self.targets = {u.tag for u in bot.enemy_units.of_type({UnitTypeId.ZEALOT, UnitTypeId.STALKER})}
            self.targets |= {s.tag for s in bot.enemy_structures(UnitTypeId.GATEWAY)}
            # Debug: what the zerglings can actually reach at the start (no gateway broken yet)
            from loguru import logger

            from bot.targeting import ReachMap

            lings = list(bot.units(UnitTypeId.ZERGLING))
            enemies = list(bot.enemy_units.of_type({UnitTypeId.ZEALOT, UnitTypeId.STALKER}))
            try:
                reach = ReachMap(bot.mediator.get_ground_grid, lings, enemies, bot.get_terrain_z_height, margin=30)
                report = ", ".join(f"{e.type_id.name}@{e.position.rounded}:{reach.open_sides(e)}" for e in enemies)
                logger.info(f"reach at start (open cells per unit): {report}")
            except TypeError:
                pass  # older bot code (comparisons) has a different ReachMap
            bot.damage.reset(bot.time)
            self.fight_started = bot.time
            self.phase = "fight"
        if self.phase == "fight":
            # Keep the units in their spots: they defend from there instead of walking out
            for unit in bot.enemy_units.of_type({UnitTypeId.ZEALOT, UnitTypeId.STALKER}):
                unit.hold_position()

    def alive(self, bot: ScenarioBot) -> int:
        known = {u.tag for u in bot.enemy_units} | {s.tag for s in bot.enemy_structures}
        return len(self.targets & known)

    def check(self, bot: ScenarioBot) -> Outcome | None:
        if self.phase == "zerglings":
            built = bot.enemy_structures(UnitTypeId.GATEWAY).amount
            units = bot.enemy_units.of_type({UnitTypeId.ZEALOT, UnitTypeId.STALKER}).amount
            if built != self.expected:
                return failed(f"setup: {built} of {self.expected} gateways spawned")
            if units < 6:
                return failed(f"setup: only {units} zealots/stalkers spawned")
            positions, _ = self.layout(bot)
            gateways = bot.enemy_structures(UnitTypeId.GATEWAY)
            moved = [p for p in positions if not gateways.closer_than(0.6, p)]
            if moved:
                return failed(f"setup: {len(moved)} gateways not where planned (gaps would be off)")
            zealot_spots, stalker_spots = self.unit_spots(bot)
            placed = bot.enemy_units.of_type({UnitTypeId.ZEALOT, UnitTypeId.STALKER})
            off = [p for p in zealot_spots + stalker_spots if not placed.closer_than(2.5, p)]
            if off:
                where = ", ".join(str(u.position.rounded) for u in placed)
                return failed(f"setup: {len(off)} units not where planned {[p.rounded for p in off]}; units at {where}")
        if self.phase != "fight" or bot.time - self.fight_started < 1:
            return None
        if not self.alive(bot):
            return self.report(bot, passed, f"all units and {self.expected} gateways dead in {self.elapsed(bot):.0f}s")
        if not bot.units(UnitTypeId.ZERGLING):
            return self.report(bot, failed, f"all zerglings died, {self.alive(bot)} targets left")
        return None

    def on_timeout(self, bot: ScenarioBot) -> Outcome:
        known = {u.tag: u for u in bot.enemy_units} | {s.tag: s for s in bot.enemy_structures}
        left = [known[t] for t in self.targets if t in known]
        what = ", ".join(f"{u.type_id.name}@{u.position.rounded}" for u in left)
        orders = {}
        for ling in bot.units(UnitTypeId.ZERGLING):
            target = known.get(ling.order_target) if isinstance(ling.order_target, int) else None
            key = f"{target.type_id.name}@{target.position.rounded}" if target else str(ling.order_target)
            orders[key] = orders.get(key, 0) + 1
        return self.report(
            bot, failed, f"{len(left)} of {len(self.targets)} targets still alive ({what}); zergling orders: {orders}"
        )


class CrampedMainMoreZealots(CrampedMain):
    """The packed main with two more zealots, in the two center gaps of the middle row."""

    name = "efficiency_cramped_more_zealots"
    description = "efficiency_cramped_main with 6 zealots (2 more in the middle row's center gaps)"
    ZEALOTS = 6

    async def on_step(self, bot: ScenarioBot) -> None:
        # The combat sim calls this fight a loss (it can't see that the gateways let zerglings take
        # the enemy apart piece by piece), so on its own the army never attacks. This tests the
        # fighting, not that decision: commit to it
        bot.army.committed = True
        await super().on_step(bot)


class OpenArmy(StructureAssault):
    """The gateway grid's army with no gateways: the same 48 zerglings against the same zealots and
    stalkers, in the same formation (zealots in a row 4 apart, stalkers 3 behind them) and on hold
    position as there, but on flat open ground. Compare zerglings lost with efficiency_cramped_main
    to see what the buildings cost."""

    name = "efficiency_open_army"
    description = "efficiency_cramped_main's army with no gateways: 48 zerglings vs 4 zealots, 4 stalkers in the open"
    time_limit = 120
    LINGS = CrampedMain.LINGS
    ZEALOTS = CrampedMain.ZEALOTS
    COLUMNS = (-6, -2, 2, 6)  # the grid's gaps
    STALKER_DEPTH = 3  # behind the zealots, as behind the back row there
    APPROACH = 20  # zerglings start this far in front of the zealots

    def __init__(self) -> None:
        super().__init__()
        self.targets: set[int] = set()

    def spots(self, bot: ScenarioBot) -> tuple[list[Point2], list[Point2], Point2]:
        """Zealots, stalkers, and where the zerglings start: around the map center, every spot
        on walkable ground."""
        center = bot.game_info.map_center
        raw = toward_us(bot, center)
        # Snapped to the map axes: a diagonal row lands units on uneven cells
        back = Point2((1 if raw.x > 0 else -1, 0)) if abs(raw.x) >= abs(raw.y) else Point2((0, 1 if raw.y > 0 else -1))
        side = across(back)

        # Rocks and other neutral units aren't in the static pathing grid, but debug spawns get
        # pushed off them
        neutral = [*bot.destructables, *bot.mineral_field, *bot.vespene_geyser]

        def open_ground(p: Point2) -> bool:
            return all(
                bot.in_pathing_grid(Point2((p.x + dx, p.y + dy))) for dx in (-1.5, 0, 1.5) for dy in (-1.5, 0, 1.5)
            ) and all(n.position.distance_to(p) > n.radius + 2 for n in neutral)

        for radius in range(0, 15):
            for dx in range(-radius, radius + 1):
                for dy in (-radius, radius) if abs(dx) != radius else range(-radius, radius + 1):
                    origin = Point2((int(center.x + dx) + 0.5, int(center.y + dy) + 0.5))
                    zealots = [origin + side * c for c in self.COLUMNS][: self.ZEALOTS]
                    stalkers = [origin + side * c - back * self.STALKER_DEPTH for c in self.COLUMNS]
                    start = origin + back * self.APPROACH
                    if all(open_ground(p) for p in [*zealots, *stalkers, start]):
                        return zealots, stalkers, start
        raise RuntimeError("no open ground for the army around the map center")

    async def on_step(self, bot: ScenarioBot) -> None:
        if self.phase == "spawn":
            zealots, stalkers, _ = self.spots(bot)
            for spot in zealots:
                await bot.spawn(UnitTypeId.ZEALOT, spot, enemy=True)
            for spot in stalkers:
                await bot.spawn(UnitTypeId.STALKER, spot, enemy=True)
            await bot.spawn(KEEP_ALIVE, keep_alive_spot(bot, [*zealots, *stalkers]), enemy=True)
            self.spawned_at = bot.time
            self.phase = "clear"
        elif self.phase == "clear" and bot.time - self.spawned_at > 1:
            keep = {UnitTypeId.ZEALOT, UnitTypeId.STALKER, KEEP_ALIVE}
            others = {u.tag for u in bot.enemy_units if u.type_id not in keep}
            others |= {s.tag for s in bot.enemy_structures if s.type_id not in keep}
            if others:
                await bot.client.debug_kill_unit(others)
            _, _, start = self.spots(bot)
            await bot.spawn(UnitTypeId.ZERGLING, start, amount=self.LINGS)
            bot.army.debug = True
            self.phase = "zerglings"
        elif self.phase == "zerglings" and bot.units(UnitTypeId.ZERGLING):
            self.targets = {u.tag for u in bot.enemy_units.of_type({UnitTypeId.ZEALOT, UnitTypeId.STALKER})}
            bot.damage.reset(bot.time)
            self.fight_started = bot.time
            self.phase = "fight"
        # On hold from the start (the built-in AI walks new units off otherwise) and through the
        # fight, as in the gateway grid: the only difference is the buildings
        for unit in bot.enemy_units.of_type({UnitTypeId.ZEALOT, UnitTypeId.STALKER}):
            if not unit.is_idle or unit.orders:
                unit.hold_position()

    def alive(self, bot: ScenarioBot) -> int:
        return len(self.targets & {u.tag for u in bot.enemy_units})

    def check(self, bot: ScenarioBot) -> Outcome | None:
        if self.phase == "zerglings":
            zealots, stalkers, _ = self.spots(bot)
            placed = bot.enemy_units.of_type({UnitTypeId.ZEALOT, UnitTypeId.STALKER})
            # Debug spawns at one end of the row come out a couple of tiles off; fine in the open
            off = [p for p in zealots + stalkers if not placed.closer_than(3.5, p)]
            if off:
                where = ", ".join(f"{u.type_id.name}@{u.position.rounded}" for u in placed)
                near = [n for n in bot.all_units if n.owner_id not in (bot.player_id, bot.enemy_id)]
                near = [n for n in near if min(n.position.distance_to(q) for q in off) < 6]
                where += "; neutral nearby: " + ", ".join(f"{n.type_id.name}@{n.position.rounded}" for n in near)
                return failed(f"setup: {len(off)} units not where planned {[p.rounded for p in off]}; units at {where}")
        if self.phase != "fight" or bot.time - self.fight_started < 1:
            return None
        lings = bot.units(UnitTypeId.ZERGLING).amount
        if not self.alive(bot):
            return self.report(
                bot, passed, f"all {len(self.targets)} units dead in {self.elapsed(bot):.0f}s, {lings} zerglings left"
            )
        if not lings:
            return self.report(bot, failed, f"all zerglings died, {self.alive(bot)} units left")
        return None

    def on_timeout(self, bot: ScenarioBot) -> Outcome:
        lings = bot.units(UnitTypeId.ZERGLING).amount
        return self.report(
            bot, failed, f"{self.alive(bot)} of {len(self.targets)} units still alive, {lings} zerglings left"
        )


SCENARIOS = [OpenField(), DeepBase(), Ramp(), CrampedMain(), CrampedMainMoreZealots(), OpenArmy()]
