"""Run a deterministic ten-minute MOCK replay as an end-to-end smoke check."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.replay import ReplayDataset, ReplayEngine
from engine.state import MatchState
from generator.match import generate_match


def main() -> None:
    events, frames = generate_match(seed=42)
    first_ten_minutes = ReplayDataset(
        name="synthetic-first-ten-minutes",
        source="synthetic",
        events=[event for event in events if event["minute"] <= 10],
        frames=[frame for frame in frames if frame["minute"] <= 10],
    )
    os.environ["LLM_MOCK"] = "1"
    os.environ["LLM_BASE_URL"] = ""
    os.environ["LLM_API_KEY"] = ""
    os.environ["LLM_MODEL"] = ""
    os.environ["AZURE_TRANSLATOR_ENABLED"] = "0"
    with tempfile.TemporaryDirectory(prefix="overseer-smoke-cache-") as cache:
        os.environ["LLM_CACHE_DIR"] = cache
        state = MatchState()
        ReplayEngine(state, speed=1000).run([first_ten_minutes])

    assert state.minute == 10.0, f"Expected ten simulated minutes, got {state.minute}"
    assert state.shape, "ShapeAnalyst did not update MatchState"
    assert state.patterns.get("findings"), "PatternSpotter found no flagged patterns"
    assert state.impact, "ImpactAnalyst produced no player rankings"
    assert state.moments, "MomentDetector produced no moments"
    assert state.narration_log, "Narrator wrote no narration"
    assert all(item["mock"] for item in state.narration_log)
    print(
        "MOCK smoke passed: 10.0 simulated minutes, "
        f"{len(state.events)} events, {len(state.frames)} frames, "
        f"{len(state.moments)} moments, {len(state.narration_log)} narrations."
    )


if __name__ == "__main__":
    main()
