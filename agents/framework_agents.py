"""Foundry-backed analyst agents with typed tools over one match state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from agent_framework import Agent, tool

from agents.analysis_tools import (
    ImpactAnalysisTool,
    MomentDetectionTool,
    PatternAnalysisTool,
    ShapeAnalysisTool,
)
from engine.state import MatchState
from llm.foundry import make_agent
from telemetry import traced_tool_call


TeamName = Literal["Rovers", "United"]


def _fact_id(section: str, data: Any) -> str:
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"{section}:{digest}"


def _input_fact_ids(state: MatchState) -> list[str]:
    records = state.events[-50:] + state.frames[-50:]
    return [_fact_id("input", record) for record in records]


@dataclass(frozen=True)
class AnalystTools:
    shape: Any
    patterns: Any
    impact: Any
    moments: Any


@dataclass(frozen=True)
class AgentSuite:
    shape_analyst: Agent
    pattern_spotter: Agent
    impact_analyst: Agent
    moment_detector: Agent
    tools: AnalystTools


def build_analysis_tools(
    *,
    state: MatchState,
    shape_analyst: ShapeAnalysisTool | None = None,
    pattern_spotter: PatternAnalysisTool | None = None,
    impact_analyst: ImpactAnalysisTool | None = None,
    moment_detector: MomentDetectionTool | None = None,
) -> AnalystTools:
    """Create typed, state-bound tools; each analysis writes its own state section."""
    shape_analyzer = shape_analyst or ShapeAnalysisTool()
    pattern_analyzer = pattern_spotter or PatternAnalysisTool()
    impact_analyzer = impact_analyst or ImpactAnalysisTool()
    moment_analyzer = moment_detector or MomentDetectionTool()
    input_ids = _input_fact_ids(state)

    @tool
    def get_team_shape(team: TeamName) -> str:
        """Return the selected team's formation, block, and marking facts."""
        with traced_tool_call("shape_analysis", fact_ids=input_ids) as span:
            all_shapes = (
                state.shape
                if state.shape.get("teams")
                else shape_analyzer.analyze(state)
            )
            result = all_shapes["teams"][team]
            output_id = _fact_id("shape", result)
            span.set_attribute("overseer.fact.output_ids", [output_id])
            return json.dumps(
                {"fact_id": output_id, "team": team, "shape": result},
                ensure_ascii=False,
            )

    @tool
    def get_team_patterns(team: TeamName) -> str:
        """Return only significant, deterministic patterns for one team."""
        with traced_tool_call("pattern_analysis", fact_ids=input_ids) as span:
            all_patterns = pattern_analyzer.analyze(state)
            prefix = f"{team.lower()}_"
            findings = {
                name: result
                for name, result in all_patterns["findings"].items()
                if name.startswith(prefix)
            }
            output_id = _fact_id("patterns", findings)
            span.set_attribute("overseer.fact.output_ids", [output_id])
            return json.dumps(
                {"fact_id": output_id, "team": team, "findings": findings},
                ensure_ascii=False,
            )

    @tool
    def get_player_impact(player: str | None = None) -> str:
        """Return measured player impact and recent momentum values."""
        with traced_tool_call("impact_analysis", fact_ids=input_ids) as span:
            impact = impact_analyzer.analyze(state)
            rankings = impact["impact"]
            if player is not None:
                rankings = [
                    item for item in rankings
                    if item.get("player", "").casefold() == player.casefold()
                ]
            result = {
                "impact": rankings,
                "momentum_series": impact["momentum_series"],
            }
            output_id = _fact_id("impact", result)
            span.set_attribute("overseer.fact.output_ids", [output_id])
            return json.dumps(
                {"fact_id": output_id, **result},
                ensure_ascii=False,
            )

    @tool
    def detect_recent_moments(since_minute: float = 0.0) -> str:
        """Return recorded goals, momentum swings, and shape changes."""
        with traced_tool_call("moment_detection", fact_ids=input_ids) as span:
            moment_analyzer.analyze(state)
            recent = [
                moment
                for moment in state.moments
                if float(moment.get("minute", 0.0)) >= since_minute
            ]
            output_id = _fact_id("moments", recent)
            span.set_attribute("overseer.fact.output_ids", [output_id])
            return json.dumps(
                {"fact_id": output_id, "moments": recent},
                ensure_ascii=False,
            )

    return AnalystTools(
        shape=get_team_shape,
        patterns=get_team_patterns,
        impact=get_player_impact,
        moments=detect_recent_moments,
    )


def build_agent_suite(
    *,
    state: MatchState,
    shape_analyst: ShapeAnalysisTool | None = None,
    pattern_spotter: PatternAnalysisTool | None = None,
    impact_analyst: ImpactAnalysisTool | None = None,
    moment_detector: MomentDetectionTool | None = None,
) -> AgentSuite:
    """Build the four tool-grounded analyst agents for a shared match state."""
    tools = build_analysis_tools(
        state=state,
        shape_analyst=shape_analyst,
        pattern_spotter=pattern_spotter,
        impact_analyst=impact_analyst,
        moment_detector=moment_detector,
    )
    instruction = (
        "Always call your available tool before answering. Use ONLY tool "
        "results. Never invent numbers, players, events, or conclusions."
    )
    return AgentSuite(
        shape_analyst=make_agent(
            "ShapeAnalyst",
            f"{instruction} Report only the selected team's shape, block, and marking.",
            tools=[tools.shape],
        ),
        pattern_spotter=make_agent(
            "PatternSpotter",
            f"{instruction} Report only significant patterns for the requested team.",
            tools=[tools.patterns],
        ),
        impact_analyst=make_agent(
            "ImpactAnalyst",
            f"{instruction} Report only measured player impact and momentum.",
            tools=[tools.impact],
        ),
        moment_detector=make_agent(
            "MomentDetector",
            f"{instruction} Report only moments returned by the moment tool.",
            tools=[tools.moments],
        ),
        tools=tools,
    )
