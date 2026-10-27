"""Coordinates structured analysts and mode-specific narration."""

from __future__ import annotations

from typing import Any

from agents.analysts import ImpactAnalyst, MomentDetector, PatternSpotter, ShapeAnalyst
from agents.narrator import Narrator
from agents.workflow import WorkflowRequest, run_match_analysis
from engine.state import MatchState
from telemetry import initialize_telemetry


class Orchestrator:
    def __init__(self, narrator: Narrator | None = None) -> None:
        initialize_telemetry()
        self.shape_analyst = ShapeAnalyst()
        self.pattern_spotter = PatternSpotter()
        self.impact_analyst = ImpactAnalyst()
        self.moment_detector = MomentDetector()
        self.narrator = narrator or Narrator()

    def tick(
        self,
        state: MatchState,
        *,
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> dict[str, Any]:
        """Run the Microsoft Agent Framework workflow over shared match state."""
        return run_match_analysis(
            WorkflowRequest(
                state=state,
                shape_analyst=self.shape_analyst,
                pattern_spotter=self.pattern_spotter,
                impact_analyst=self.impact_analyst,
                moment_detector=self.moment_detector,
                narrator=self.narrator,
                mode=mode,
                language=language,
                favourite_team=favourite_team,
                favourite_player=favourite_player,
            )
        )
