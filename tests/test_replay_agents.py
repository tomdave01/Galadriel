from __future__ import annotations

from agents import FanCurator, Orchestrator
from engine.replay import ReplayDataset, ReplayEngine
from engine.state import MatchState, ingest
from llm.client import LLMClient


def _event(
    team: str,
    player: str,
    outcome: str,
    *,
    minute: float = 0.0,
    source: str = "synthetic",
    clip_id: str | None = None,
) -> dict:
    return {
        "match_id": "test",
        "clip_id": clip_id,
        "source": source,
        "minute": minute,
        "t": None,
        "type": "shot",
        "team": team,
        "player": player,
        "outcome": outcome,
        "x": 80.0,
        "y": 75.0,
        "end_x": None,
        "end_y": None,
    }


def test_ingest_tracks_goals_minute_and_source():
    state = MatchState()
    ingest(state, event=_event("Rovers", "Rico Vale", "goal", minute=2, source="video"))
    assert state.score == {"Rovers": 1, "United": 0}
    assert state.minute == 2
    assert state.source == "video"


def test_replay_processes_clip_before_synthetic_and_offsets_timeline():
    clip_goal = _event("United", "Ira West", "goal", source="video", clip_id="c1")
    clip_goal["t"] = 4.0
    clip = ReplayDataset(
        name="clip",
        source="video",
        events=[clip_goal],
        frames=[{"t": 0.0, "minute": 0.0, "poss": "United", "ball": {"x": 50, "y": 50}, "players": []}],
    )
    synthetic = ReplayDataset(
        name="generated",
        source="synthetic",
        events=[_event("Rovers", "Rico Vale", "goal")],
        frames=[{"t": 0.0, "minute": 0.0, "poss": "Rovers", "ball": {"x": 50, "y": 50}, "players": []}],
    )
    state = MatchState()
    engine = ReplayEngine(state, speed=1_000_000, tick_interval=2.5)
    engine.run([clip, synthetic])
    assert [event["source"] for event in state.events] == ["video", "synthetic"]
    assert state.events[1]["minute"] == state.events[0]["minute"]
    assert state.events[0]["t"] == 4.0
    assert state.score == {"Rovers": 1, "United": 1}
    assert len(state.frames) == 2


def test_fan_curator_filters_shots_on_target():
    state = MatchState(events=[
        _event("Rovers", "Rico Vale", "on_target"),
        _event("Rovers", "Leo Vale", "off_target"),
        _event("United", "Ira West", "on_target"),
    ])
    result = FanCurator().query(state, "Rico Vale shots on target")
    assert result["compiled_by"] == "code"
    assert result["count"] == 1
    assert result["clips"][0]["player"] == "Rico Vale"


def test_llm_mock_and_orchestrator_narration_are_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MOCK", "1")
    client = LLMClient(cache_dir=tmp_path)
    result = client.complete("facts", fallback_text="Rovers lead.")
    assert result["mock"] is True
    assert result["text"] == "Rovers lead."
    assert "enabled" in result["reason"]

    state = MatchState()
    state.events = [_event("Rovers", "Rico Vale", "goal", minute=1)]
    state.frames = [
        {
            "t": 60.0,
            "minute": 1.0,
            "poss": "Rovers",
            "ball": {"x": 70.0, "y": 50.0},
            "players": [],
        }
    ]
    output = Orchestrator().tick(state, mode="Broadcast", language="English")
    assert output["narration"]["mock"] is True
    assert "Rovers" in output["narration"]["text"]
    assert state.moments[0]["type"] == "goal"
