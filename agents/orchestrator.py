"""Coordinates deterministic analysis and Microsoft Agent Framework agents."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

from agents.analysis_tools import (
    ImpactAnalysisTool,
    MomentDetectionTool,
    PatternAnalysisTool,
    ShapeAnalysisTool,
)
from agents.fan_curator import FanCurator
from agents.narrator import Narrator
from agents.workflow import run_agent_workflow
from engine.state import MatchState
from feed.persistence import NarrationCache
from llm.client import create_foundry_chat_client
from telemetry import initialize_telemetry, traced_agent_call, traced_tool_call


def _fact_id(section: str, data: Any) -> str:
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"{section}:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


class Orchestrator:
    def __init__(
        self,
        narrator: Narrator | None = None,
        *,
        fan_curator: FanCurator | None = None,
        narration_cache: NarrationCache | None = None,
        framework_client: Any | None = None,
    ) -> None:
        initialize_telemetry()
        self.shape_tool = ShapeAnalysisTool()
        self.pattern_tool = PatternAnalysisTool()
        self.impact_tool = ImpactAnalysisTool()
        self.moment_tool = MomentDetectionTool()
        self.narrator = narrator or Narrator(cache=narration_cache)
        self.fan_curator = fan_curator or FanCurator()
        self.framework_client = framework_client

    def analyze_facts(self, state: MatchState) -> dict[str, Any]:
        """Run the deterministic tools without waiting for any model call."""
        input_ids = [
            _fact_id("input", record)
            for record in state.events[-50:] + state.frames[-50:]
        ]
        results: dict[str, Any] = {}
        analyses = (
            ("ShapeAnalyst", self.shape_tool.analyze, "shape"),
            ("PatternSpotter", self.pattern_tool.analyze, "patterns"),
            ("ImpactAnalyst", self.impact_tool.analyze, "impact"),
            ("MomentDetector", self.moment_tool.analyze, "moments"),
        )
        for agent_name, analyze, result_name in analyses:
            with traced_agent_call(agent_name, state, fact_ids=input_ids) as span:
                with traced_tool_call(
                    result_name,
                    fact_ids=input_ids,
                ) as tool_span:
                    result = analyze(state)
                    output_id = _fact_id(result_name, result)
                    tool_span.set_attribute("overseer.fact.output_ids", [output_id])
                    span.set_attribute("overseer.fact.output_ids", [output_id])
                    results[result_name] = result
        return {
            "minute": round(state.minute, 2),
            **results,
            "momentum_series": state.momentum_series,
        }

    async def iter_tick_events(
        self,
        state: MatchState,
        *,
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield deterministic facts before awaiting optional model narration."""
        facts = self.analyze_facts(state)
        yield {"type": "fact", "facts": facts}

        narration = await self.narrator.narrate(
            state,
            mode=mode,
            language=language,
            favourite_team=favourite_team,
            favourite_player=favourite_player,
        )
        yield {"type": "narration", "narration": narration}

    async def tick_async(
        self,
        state: MatchState,
        *,
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> dict[str, Any]:
        facts = self.analyze_facts(state)
        narration = await self.narrator.narrate(
            state,
            mode=mode,
            language=language,
            favourite_team=favourite_team,
            favourite_player=favourite_player,
        )
        return {
            **facts,
            "impact": state.impact,
            "momentum_series": state.momentum_series,
            "moments": state.moments,
            "narration": narration,
        }

    def tick(
        self,
        state: MatchState,
        *,
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> dict[str, Any]:
        """Synchronous adapter used by the existing replay runner."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.tick_async(
                    state,
                    mode=mode,
                    language=language,
                    favourite_team=favourite_team,
                    favourite_player=favourite_player,
                )
            )
        raise RuntimeError("Orchestrator.tick cannot run inside an event loop; await tick_async")

    async def run_framework_workflow(
        self,
        state: MatchState,
        prompt: str = "Analyze the current match using only the shared structured facts.",
    ) -> Any:
        """Run the Agent Framework sequential multi-agent workflow."""
        from agents.framework_agents import build_agent_suite

        client = self.framework_client or create_foundry_chat_client()
        suite = build_agent_suite(
            state=state,
            shape_analyst=self.shape_tool,
            pattern_spotter=self.pattern_tool,
            impact_analyst=self.impact_tool,
            moment_detector=self.moment_tool,
            narrator=self.narrator,
            fan_curator=self.fan_curator,
            client=client,
        )
        return await run_agent_workflow(suite, state, prompt)
