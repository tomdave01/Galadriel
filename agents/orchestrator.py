"""Coordinates structured analysts and mode-specific narration."""

from __future__ import annotations

from typing import Any

from agents.analysts import ImpactAnalyst, MomentDetector, PatternSpotter, ShapeAnalyst
from agents.narrator import Narrator
from engine.state import MatchState


class Orchestrator:
    def __init__(self, narrator: Narrator | None = None) -> None:
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
        """Run all specialists in dependency order, then narrate their facts."""
        shape_result = self.shape_analyst.analyze(state)
        patterns_result = self.pattern_spotter.analyze(state)
        impact_result = self.impact_analyst.analyze(state)
        moments_result = self.moment_detector.analyze(state)
        narration = self.narrator.narrate(
            state,
            mode=mode,
            language=language,
            favourite_team=favourite_team,
            favourite_player=favourite_player,
        )
        return {
            "minute": round(state.minute, 2),
            "shape": shape_result,
            "patterns": patterns_result,
            **impact_result,
            "moments": moments_result,
            "narration": narration,
        }
