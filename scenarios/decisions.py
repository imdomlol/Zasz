"""The decision interface (`bot.decisions`): other decisions plug in, and games can be recorded."""

import tempfile
from pathlib import Path

from sc2.ids.unit_typeid import UnitTypeId

from bot.decisions import Build, Decision
from bot.observation import FEATURE_NAMES, Observation
from bot.recording import DecisionRecorder, read_records
from scenarios import Outcome, Scenario, ScenarioBot, failed, passed


class ExpandFirst:
    """A policy the scripted rules would never pick: take the natural before any spawning pool."""

    def decide(self, obs: Observation) -> Decision:
        return Decision(
            build=Build.EXPAND if obs.bases < 2 else Build.NONE,
            drone_target=obs.drone_capacity,
        )


class CustomDecisions(Scenario):
    name = "custom_decisions"
    description = "Other decisions plug in: an expand-first policy takes the natural before a pool; recorded"
    time_limit = 60

    def __init__(self) -> None:
        self.folder: tempfile.TemporaryDirectory | None = None  # made with the recorder, removed in teardown
        self.path = Path()

    def decisions(self) -> ExpandFirst:
        return ExpandFirst()

    def recorder(self) -> DecisionRecorder:
        self.folder = tempfile.TemporaryDirectory(prefix="zasz-scenario-")
        self.path = Path(self.folder.name) / "custom_decisions.jsonl"
        return DecisionRecorder(self.path, meta={"scenario": self.name})

    def teardown(self, bot: ScenarioBot) -> None:
        bot.recorder.close("scenario over")  # on_end's close() then does nothing
        if self.folder is not None:
            self.folder.cleanup()

    async def setup(self, bot: ScenarioBot) -> None:
        await bot.client.debug_minerals()

    def check(self, bot: ScenarioBot) -> Outcome | None:
        if bot.structures(UnitTypeId.SPAWNINGPOOL) or bot.already_pending(UnitTypeId.SPAWNINGPOOL):
            return failed("a spawning pool was started: the scripted rules are still in charge")
        if not bot.townhalls.not_ready:
            return None
        # The policy was followed; check the recording of it too
        bot.recorder.flush()
        recording = read_records(self.path)
        if not recording.steps:
            return failed("nothing recorded")
        if recording.header["features"] != FEATURE_NAMES:
            return failed("recorded feature names don't match")
        if not any(d.build == Build.EXPAND for _, _, d in recording.steps):
            return failed("the recorded decisions never asked to expand")
        _, first, first_decision = recording.steps[0]
        if (first.bases, first.drones, first_decision.build) != (1, 12, Build.EXPAND):
            return failed(f"first recorded step looks wrong: {first.bases} bases, {first.drones} drones")
        _, last, last_decision = recording.steps[-1]
        if (last.bases, last_decision.build) != (2, Build.NONE):
            return failed(f"last recorded step should be the switch back once the base is down: {last}")
        return passed(f"natural started at {bot.time:.0f}s without a pool; {len(recording.steps)} steps recorded")


SCENARIOS = [CustomDecisions]
