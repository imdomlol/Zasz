"""Walled-in enemies: 4 marines in a pocket sealed by a ring of engineering bays, with different weak
points. The goal is to kill the marines as fast as possible while losing as few zerglings as
possible (the runner reports zerglings lost and time).

- `wall_techlab`: the ring's side facing us has a tech lab (2x2, 400 hp vs 850 for a bay: a supply
  depot's stats; debug commands won't spawn an enemy supply depot)
- `wall_bunker`: that slot is a bunker, with the 4 marines inside it
- `wall_bays`: engineering bays all round, nothing weaker to break

The ring, in tiles, with v pointing at our side (the weak side is the top row here):

    v=9..11  [bay][weak][bay][bay]    (the tech lab variant: [bay][lab 2x2][bay][bay], the row
    v=3..8   [bay]  pocket   [bay]     stops one tile short; still sealed)
    v=0..2   [bay][bay][bay][bay]
             u=0..11

The setup fails loudly unless every building landed where planned and the pocket is sealed (no
walkable way from the zerglings to the middle of the pocket).
"""

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from bot.targeting import ReachMap
from scenarios import Outcome, Scenario, ScenarioBot, failed, passed
from scenarios.efficiency import keep_alive_spot

BAY = UnitTypeId.ENGINEERINGBAY
# Debug commands won't spawn an enemy supply depot (not even on open ground), so the 2x2 weak point is
# a tech lab: also 400 hp, 1 armor, the same thing to a zergling
TECHLAB = UnitTypeId.TECHLAB
BUNKER = UnitTypeId.BUNKER
KEEP_ALIVE = UnitTypeId.BARRACKSFLYING  # zerglings can't hit air: the game doesn't end early
RING = [  # (u, v, size): bottom row, left and right columns, top row without its weak slot
    (0, 0, 3), (3, 0, 3), (6, 0, 3), (9, 0, 3),
    (0, 3, 3), (0, 6, 3), (9, 3, 3), (9, 6, 3),
    (0, 9, 3),
]  # fmt: skip
POCKET = Point2((6, 6))  # middle of the pocket (u, v)
LINGS = 24
APPROACH = 15  # zerglings start this far out from the weak side


class WalledMarines(Scenario):
    """Base: the ring with a weak slot (`WEAK`: a structure type and its pieces), 4 marines inside."""

    time_limit = 150
    WEAK: list[tuple[UnitTypeId, int, int, int]] = []  # (type, u, v, size) in the top row

    def __init__(self) -> None:
        self.phase = "spawn"
        self.spawned_at = 0.0
        self.fight_started = 0.0
        self.origin: Point2 | None = None
        self.axis: tuple[str, int] = ("y", 1)
        self.checked = False
        self.lab_tries = 0
        self.marine_tags: set[int] = set()  # the 4 marines (inside a bunker they drop out of view)

    # --- layout ---

    def pieces(self) -> list[tuple[UnitTypeId, int, int, int]]:
        return [(BAY, u, v, s) for u, v, s in RING] + self.WEAK

    def world(self, u: float, v: float, size: float = 0.0) -> Point2:
        """Local (u, v) of a piece's corner (+ size/2 to its center) to map coordinates; v points at
        our side along the map axis closest to that direction."""
        a, sign = self.axis
        cu, cv = u + size / 2, v + size / 2
        along = cv if sign > 0 else 12 - cv
        x, y = (cu, along) if a == "y" else (along, cu)
        return Point2((self.origin.x + x, self.origin.y + y))

    def plan(self, bot: ScenarioBot) -> None:
        center = bot.game_info.map_center
        d = bot.start_location - center
        self.axis = ("y", 1 if d.y > 0 else -1) if abs(d.y) >= abs(d.x) else ("x", 1 if d.x > 0 else -1)
        for radius in range(0, 45):
            for dx in range(-radius, radius + 1):
                for dy in (-radius, radius) if abs(dx) != radius else range(-radius, radius + 1):
                    self.origin = Point2((int(center.x) - 6 + dx, int(center.y) - 6 + dy))
                    cells = [self.world(u + 0.5, v + 0.5) for u in range(-1, 13) for v in range(-1, 13)]
                    start = self.zergling_start()
                    if all(bot.in_placement_grid(c) for c in cells) and bot.in_pathing_grid(start):
                        return
        raise RuntimeError("no open buildable ground for the wall near the map center")

    def zergling_start(self) -> Point2:
        return self.world(6, 12 + APPROACH)

    # --- running ---

    async def setup(self, bot: ScenarioBot) -> None:
        await bot.client.debug_show_map()
        await bot.client.debug_control_enemy()

    async def on_step(self, bot: ScenarioBot) -> None:
        if self.phase == "spawn":
            self.plan(bot)
            for type_id, u, v, size in self.pieces():
                if type_id != TECHLAB:  # the tech lab a step later (retried until it's there)
                    await bot.spawn(type_id, self.world(u, v, size), enemy=True)
            await bot.spawn(KEEP_ALIVE, keep_alive_spot(bot, [self.world(6, 6)]), enemy=True)
            await bot.spawn(UnitTypeId.MARINE, self.world(POCKET.x, POCKET.y), amount=4, enemy=True)
            self.spawned_at = bot.time
            self.phase = "clear"
        elif self.phase == "clear" and not self.labs_placed(bot):
            for type_id, u, v, size in self.pieces():
                if type_id == TECHLAB:
                    spot = self.world(u, v, size)
                    self.lab_tries += 1
                    await bot.spawn(TECHLAB, spot, enemy=True)
        elif self.phase == "clear" and bot.time - self.spawned_at > 1:
            keep = {BAY, TECHLAB, BUNKER, KEEP_ALIVE, UnitTypeId.MARINE}
            others = {u.tag for u in bot.enemy_units if u.type_id not in keep}
            others |= {s.tag for s in bot.enemy_structures if s.type_id not in keep}
            if others:
                await bot.client.debug_kill_unit(others)
            self.phase = "ready"
        elif self.phase == "ready" and self.ready(bot):
            await bot.spawn(UnitTypeId.ZERGLING, self.zergling_start(), amount=LINGS)
            self.phase = "zerglings"
        elif self.phase == "zerglings" and bot.units(UnitTypeId.ZERGLING):
            self.fight_started = bot.time
            self.phase = "fight"
        # The built-in AI walks units around: keep the marines in place
        if self.phase != "ready":
            for marine in bot.enemy_units(UnitTypeId.MARINE):
                marine.hold_position()

    def ready(self, bot: ScenarioBot) -> bool:
        return True

    def labs_placed(self, bot: ScenarioBot) -> bool:
        if self.lab_tries > 20:
            return True  # give up: the setup check reports it
        labs = bot.enemy_structures(TECHLAB)
        return all(labs.closer_than(0.6, self.world(u, v, s)) for t, u, v, s in self.pieces() if t == TECHLAB)

    def marines_left(self, bot: ScenarioBot) -> int:
        """Marines alive: the ones we see, plus (while a bunker stands) the ones out of view, inside it.
        An enemy bunker's cargo isn't visible to us, so this goes by the marines' tags."""
        if not self.marine_tags:
            self.marine_tags = {m.tag for m in bot.enemy_units(UnitTypeId.MARINE)}
        seen = {m.tag for m in bot.enemy_units(UnitTypeId.MARINE)}
        inside = len(self.marine_tags - seen) if bot.enemy_structures(BUNKER) else 0
        self.marine_tags = (self.marine_tags & seen) | (self.marine_tags - seen if inside else set())
        return len(seen) + inside

    def check(self, bot: ScenarioBot) -> Outcome | None:
        if self.phase != "fight":
            return None
        if not self.checked:
            self.checked = True
            if problem := self.setup_problem(bot):
                return failed(f"setup: {problem}")
        lings = bot.units(UnitTypeId.ZERGLING).amount
        if not self.marines_left(bot):
            return passed(
                f"all marines dead in {bot.time - self.fight_started:.0f}s, {LINGS - lings} of {LINGS} zerglings lost"
            )
        if not lings:
            return failed(f"all zerglings died, {self.marines_left(bot)} marines left")
        return None

    def on_timeout(self, bot: ScenarioBot) -> Outcome:
        if self.phase != "fight":
            return failed(f"setup: the fight never started (stuck in '{self.phase}')")
        lings = bot.units(UnitTypeId.ZERGLING).amount
        walls = bot.enemy_structures.of_type({BAY, TECHLAB, BUNKER}).amount
        return failed(
            f"{self.marines_left(bot)} marines alive after {self.time_limit:.0f}s, "
            f"{LINGS - lings} of {LINGS} zerglings lost, {walls} wall buildings left"
        )

    def setup_problem(self, bot: ScenarioBot) -> str | None:
        for type_id, u, v, size in self.pieces():
            spot = self.world(u, v, size)
            if not bot.enemy_structures(type_id).closer_than(0.6, spot):
                near = ", ".join(f"{e.type_id.name}@{e.position}" for e in bot.enemy_structures.closer_than(4, spot))
                kinds = sorted({e.type_id.name for e in bot.enemy_structures})
                return f"{type_id.name} not at {spot} (near it: {near or 'nothing'}; enemy structures: {kinds})"
        if self.marines_left(bot) != 4:
            return f"{self.marines_left(bot)} marines instead of 4"
        lings = list(bot.units(UnitTypeId.ZERGLING))
        reach = ReachMap(bot.mediator.get_ground_grid, lings, [], margin=40)
        pocket = self.world(POCKET.x, POCKET.y)
        cell = (int(pocket.x) - reach.x0, int(pocket.y) - reach.y0)
        if reach.reached[cell]:
            return "the pocket isn't sealed (the zerglings can walk in)"
        return None


class WallTechLab(WalledMarines):
    name = "wall_techlab"
    description = "4 marines sealed in by engineering bays, with a tech lab (2x2, 400 hp) as the weak point"
    WEAK = [(TECHLAB, 3, 9, 2), (BAY, 5, 9, 3), (BAY, 8, 9, 3)]


class WallBunker(WalledMarines):
    name = "wall_bunker"
    description = "An engineering bay wall with a bunker as the weak point, the 4 marines inside it"
    WEAK = [(BUNKER, 3, 9, 3), (BAY, 6, 9, 3), (BAY, 9, 9, 3)]

    def ready(self, bot: ScenarioBot) -> bool:
        # Load the marines first; the fight starts once they're all inside
        bunkers = bot.enemy_structures(BUNKER)
        if not bunkers:
            return False
        # All 4 seen at some point (tags recorded in on_step), now out of view with the bunker
        # standing: inside it
        return len(self.marine_tags) == 4 and not bot.enemy_units(UnitTypeId.MARINE)

    async def on_step(self, bot: ScenarioBot) -> None:
        await super().on_step(bot)
        # The built-in AI unloads its bunker: while it stands, any marine we see goes back in (this
        # comes after the base class's hold-position orders, so it's the one that counts)
        marines = bot.enemy_units(UnitTypeId.MARINE)
        if self.phase in ("clear", "ready"):
            self.marine_tags |= {m.tag for m in marines}  # before they disappear into the bunker
        if self.phase != "spawn" and (bunkers := bot.enemy_structures(BUNKER)):
            for marine in marines:
                marine.smart(bunkers.first)


class WallBays(WalledMarines):
    name = "wall_bays"
    description = "4 marines sealed in by engineering bays all round (nothing weaker to break)"
    WEAK = [(BAY, 3, 9, 3), (BAY, 6, 9, 3), (BAY, 9, 9, 3)]


SCENARIOS = [WallTechLab(), WallBunker(), WallBays()]
