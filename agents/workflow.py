"""Agent Framework multi-agent workflow execution."""

from __future__ import annotations

from typing import Any

from agents.framework_agents import AgentSuite, _input_fact_ids
from engine.state import MatchState
from telemetry import traced_operation


async def run_agent_workflow(
    suite: AgentSuite,
    state: MatchState,
    prompt: str,
) -> Any:
    """Run the sequential Agent Framework workflow and return its terminal result."""
    with traced_operation(
        "overseer_match_analysis",
        operation_type="workflow",
        fact_ids=_input_fact_ids(state),
    ):
        return await suite.workflow.run(prompt)
