"""Compare the Foundry ShapeAnalyst answer with the deterministic block result."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.framework_agents import build_agent_suite
from engine.patterns import block
from engine.state import MatchState
from generator.match import generate_match
from llm.foundry import ask


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    events, all_frames = generate_match(seed=42)
    frames = [frame for frame in all_frames if float(frame["minute"]) <= 10.0]
    match_events = [event for event in events if float(event["minute"]) <= 10.0]
    state = MatchState(
        minute=10.0,
        events=match_events,
        frames=frames,
    )
    suite = build_agent_suite(state=state)

    answer = await ask(
        suite.shape_analyst,
        "What block is United playing? Use team United.",
        timeout=30.0,
        fallback="ShapeAnalyst did not return an answer.",
    )
    deterministic_result = block(frames, "United")

    print(f"ShapeAnalyst: {answer}")
    print(f"patterns.block(United): {deterministic_result}")


if __name__ == "__main__":
    asyncio.run(main())
