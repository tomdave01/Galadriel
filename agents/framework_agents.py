"""Microsoft Agent Framework agents and deterministic analysis tools."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from agent_framework import AgentContext, AgentMiddleware, tool
from agent_framework.orchestrations import SequentialBuilder

from agents.analysis_tools import (
    ImpactAnalysisTool,
    MomentDetectionTool,
    PatternAnalysisTool,
    ShapeAnalysisTool,
)
from agents.fan_curator import FanCurator
from agents.narrator import Narrator
from engine.state import MatchState
from telemetry import traced_agent_call, traced_tool_call


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


@dataclass
class AgentSuite:
    orchestrator: Any
    shape_analyst: Any
    pattern_spotter: Any
    impact_analyst: Any
    moment_detector: Any
    narrator: Any
    fan_curator: Any
    workflow: Any


class _AgentTracingMiddleware(AgentMiddleware):
    def __init__(
        self,
        agent_name: str,
        state: MatchState,
        fact_ids: list[str],
    ) -> None:
        self.agent_name = agent_name
        self._state = state
        self._fact_ids = fact_ids

    async def process(self, context: AgentContext, call_next: Any) -> None:
        with traced_agent_call(
            self.agent_name,
            self._state,
            fact_ids=self._fact_ids,
        ) as span:
            await call_next()
            response_text = getattr(context.result, "text", "")
            if isinstance(response_text, str) and response_text:
                output_id = _fact_id(self.agent_name, response_text)
                span.set_attribute("overseer.fact.output_ids", [output_id])


def build_agent_suite(
    *,
    state: MatchState,
    shape_analyst: ShapeAnalysisTool,
    pattern_spotter: PatternAnalysisTool,
    impact_analyst: ImpactAnalysisTool,
    moment_detector: MomentDetectionTool,
    narrator: Narrator,
    fan_curator: FanCurator,
    client: Any,
) -> AgentSuite:
    """Build role-scoped Agent Framework agents over the same match state."""
    input_ids = _input_fact_ids(state)

    def run_shape_analysis() -> str:
        with traced_tool_call("shape_analysis", fact_ids=input_ids) as span:
            result = shape_analyst.analyze(state)
            output_id = _fact_id("shape", result)
            span.set_attribute("overseer.fact.output_ids", [output_id])
            return json.dumps(
                {"fact_id": output_id, "shape": result},
                ensure_ascii=False,
            )

    def run_pattern_analysis() -> str:
        with traced_tool_call("pattern_analysis", fact_ids=input_ids) as span:
            result = pattern_spotter.analyze(state)
            output_id = _fact_id("patterns", result)
            span.set_attribute("overseer.fact.output_ids", [output_id])
            return json.dumps(
                {"fact_id": output_id, "patterns": result},
                ensure_ascii=False,
            )

    def run_impact_analysis() -> str:
        with traced_tool_call("impact_analysis", fact_ids=input_ids) as span:
            result = impact_analyst.analyze(state)
            output_id = _fact_id("impact", result)
            span.set_attribute("overseer.fact.output_ids", [output_id])
            return json.dumps(
                {"fact_id": output_id, "impact": result},
                ensure_ascii=False,
            )

    def run_moment_detection() -> str:
        with traced_tool_call("moment_detection", fact_ids=input_ids) as span:
            result = moment_detector.analyze(state)
            output_id = _fact_id("moments", result)
            span.set_attribute("overseer.fact.output_ids", [output_id])
            return json.dumps(
                {"fact_id": output_id, "moments": result},
                ensure_ascii=False,
            )

    def read_shared_match_facts() -> str:
        result = {
            "minute": round(state.minute, 2),
            "score": state.score,
            "shape": state.shape,
            "patterns": state.patterns,
            "impact": state.impact,
            "momentum": state.momentum_series,
            "moments": state.moments,
        }
        return json.dumps(result, ensure_ascii=False)

    async def write_narration(
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> str:
        result = await narrator.narrate(
            state,
            mode=mode,
            language=language,
            favourite_team=favourite_team,
            favourite_player=favourite_player,
        )
        return json.dumps(result, ensure_ascii=False)

    async def compile_fan_query(request: str) -> str:
        with traced_tool_call("compile_fan_query", fact_ids=input_ids) as span:
            result = await fan_curator.query_async(state, request)
            output_id = _fact_id("fan-query", result)
            span.set_attribute("overseer.fact.output_ids", [output_id])
            return json.dumps(result, ensure_ascii=False)

    shape_tool = tool(
        name="analyze_shape",
        description="Compute shape, block, and marking for both teams.",
    )(run_shape_analysis)
    pattern_tool = tool(
        name="spot_patterns",
        description="Compute significant flank, reliance, turnover, and pressing facts.",
    )(run_pattern_analysis)
    impact_tool = tool(
        name="analyze_impact",
        description="Compute deterministic player influence and momentum.",
    )(run_impact_analysis)
    moment_tool = tool(
        name="detect_moments",
        description="Detect deduplicated goals, momentum swings, and shape changes.",
    )(run_moment_detection)
    state_tool = tool(
        name="read_match_facts",
        description="Read the current structured facts from the shared match state.",
    )(read_shared_match_facts)
    narrator_tool = tool(
        name="write_grounded_narration",
        description="Narrate the supplied match state using only recorded facts.",
    )(write_narration)
    curator_tool = tool(
        name="compile_fan_query",
        description="Compile a fan request into validated event filters and clips.",
    )(compile_fan_query)

    orchestrator = client.as_agent(
        name="Orchestrator",
        instructions=(
            "Coordinate the match analysis. Read the shared facts and return a "
            "brief routing summary. Do not invent or modify facts."
        ),
        tools=[state_tool],
        middleware=[_AgentTracingMiddleware("Orchestrator", state, input_ids)],
    )
    shape_agent = client.as_agent(
        name="ShapeAnalyst",
        instructions=(
            "Report only shape, block, and marking facts. Call analyze_shape; "
            "do not infer beyond its deterministic result."
        ),
        tools=[shape_tool],
        middleware=[_AgentTracingMiddleware("ShapeAnalyst", state, input_ids)],
    )
    pattern_agent = client.as_agent(
        name="PatternSpotter",
        instructions=(
            "Report only significant deterministic patterns. Call spot_patterns "
            "and do not add findings."
        ),
        tools=[pattern_tool],
        middleware=[_AgentTracingMiddleware("PatternSpotter", state, input_ids)],
    )
    impact_agent = client.as_agent(
        name="ImpactAnalyst",
        instructions=(
            "Report only measured player influence and momentum. Call "
            "analyze_impact and do not infer causation."
        ),
        tools=[impact_tool],
        middleware=[_AgentTracingMiddleware("ImpactAnalyst", state, input_ids)],
    )
    moment_agent = client.as_agent(
        name="MomentDetector",
        instructions=(
            "Report only deduplicated match moments. Call detect_moments and do "
            "not invent events."
        ),
        tools=[moment_tool],
        middleware=[_AgentTracingMiddleware("MomentDetector", state, input_ids)],
    )
    narrator_agent = client.as_agent(
        name="Narrator",
        instructions=(
            "Use only facts returned by write_grounded_narration. Never translate "
            "player or club names or add unsupported claims."
        ),
        tools=[narrator_tool],
        middleware=[_AgentTracingMiddleware("Narrator", state, input_ids)],
    )
    curator_agent = client.as_agent(
        name="FanCurator",
        instructions=(
            "Use compile_fan_query to return only clips matching the request. "
            "Do not invent teams, players, events, or clips."
        ),
        tools=[curator_tool],
        middleware=[_AgentTracingMiddleware("FanCurator", state, input_ids)],
    )

    workflow = SequentialBuilder(
        name="overseer_match_analysis",
        participants=[
            orchestrator,
            shape_agent,
            pattern_agent,
            impact_agent,
            moment_agent,
            narrator_agent,
        ],
        chain_only_agent_responses=True,
    ).build()
    return AgentSuite(
        orchestrator=orchestrator,
        shape_analyst=shape_agent,
        pattern_spotter=pattern_agent,
        impact_analyst=impact_agent,
        moment_detector=moment_agent,
        narrator=narrator_agent,
        fan_curator=curator_agent,
        workflow=workflow,
    )
