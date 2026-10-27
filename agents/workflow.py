"""Microsoft Agent Framework workflow adapter for the existing Python agents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agent_framework import step, workflow

from agents.analysts import ImpactAnalyst, MomentDetector, PatternSpotter, ShapeAnalyst
from agents.narrator import Narrator
from engine.state import MatchState
from llm.translator import translate_narration, translation_enabled
from telemetry import traced_agent_call


@dataclass
class WorkflowRequest:
    state: MatchState
    shape_analyst: ShapeAnalyst
    pattern_spotter: PatternSpotter
    impact_analyst: ImpactAnalyst
    moment_detector: MomentDetector
    narrator: Narrator
    mode: str
    language: str
    favourite_team: str | None
    favourite_player: str | None


@step
async def analyze_shape(request: WorkflowRequest) -> dict[str, Any]:
    with traced_agent_call("ShapeAnalyst", request.state) as span:
        result = request.shape_analyst.analyze(request.state)
        span.set_attribute("overseer.output.teams", len(result.get("teams", {})))
        return result


@step
async def spot_patterns(request: WorkflowRequest) -> dict[str, Any]:
    with traced_agent_call("PatternSpotter", request.state) as span:
        result = request.pattern_spotter.analyze(request.state)
        span.set_attribute(
            "overseer.output.findings", len(result.get("findings", {}))
        )
        return result


@step
async def analyze_impact(request: WorkflowRequest) -> dict[str, Any]:
    with traced_agent_call("ImpactAnalyst", request.state) as span:
        result = request.impact_analyst.analyze(request.state)
        span.set_attribute("overseer.output.players", len(result.get("impact", [])))
        return result


@step
async def detect_moments(request: WorkflowRequest) -> list[dict[str, Any]]:
    with traced_agent_call("MomentDetector", request.state) as span:
        result = request.moment_detector.analyze(request.state)
        span.set_attribute("overseer.output.moments", len(result))
        return result


@step
async def narrate(request: WorkflowRequest) -> dict[str, Any]:
    with traced_agent_call("Narrator", request.state) as span:
        translate = translation_enabled() and request.language != "English"
        result = request.narrator.narrate(
            request.state,
            mode=request.mode,
            language="English" if translate else request.language,
            favourite_team=request.favourite_team,
            favourite_player=request.favourite_player,
        )
        if translate:
            names = [
                str(event.get("player", ""))
                for event in request.state.events
                if event.get("player")
            ]
            names.extend(("Rovers", "United"))
            result["text"] = translate_narration(
                result["text"], request.language, names
            )
            result["language"] = request.language
            request.state.narration_log[-1]["text"] = result["text"]
            request.state.narration_log[-1]["language"] = request.language
            span.set_attribute("overseer.translator.enabled", True)
        span.set_attribute("overseer.output.mock", bool(result.get("mock")))
        return result


@workflow(name="overseer_match_analysis")
async def match_analysis(request: WorkflowRequest) -> dict[str, Any]:
    """Run the unchanged specialist implementations in dependency order."""
    shape_result = await analyze_shape(request)
    patterns_result = await spot_patterns(request)
    impact_result = await analyze_impact(request)
    moments_result = await detect_moments(request)
    narration = await narrate(request)
    return {
        "minute": round(request.state.minute, 2),
        "shape": shape_result,
        "patterns": patterns_result,
        **impact_result,
        "moments": moments_result,
        "narration": narration,
    }


def run_match_analysis(request: WorkflowRequest) -> dict[str, Any]:
    """Execute the framework workflow from the synchronous replay interface."""
    async def execute() -> dict[str, Any]:
        result = await match_analysis.build().run(request)
        outputs = result.get_outputs()
        if not outputs or not isinstance(outputs[0], dict):
            raise RuntimeError(
                "Agent Framework workflow returned no analysis result"
            )
        return outputs[0]

    return asyncio.run(execute())
