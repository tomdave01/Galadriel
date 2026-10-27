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
    assert state.events[1]["t"] is None
    assert state.frames[-1]["t"] == 0.0
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


def test_optional_translator_runs_inside_workflow_and_preserves_state(
    monkeypatch,
):
    import agents.workflow as workflow_module

    monkeypatch.setenv("LLM_MOCK", "1")
    monkeypatch.setenv("AZURE_TRANSLATOR_ENABLED", "1")

    def fake_translate(text: str, language: str, names: list[str]) -> str:
        assert names[-2:] == ["Rovers", "United"]
        return f"{language}:{text}"

    monkeypatch.setattr(
        workflow_module,
        "translate_narration",
        fake_translate,
    )
    state = MatchState()
    result = Orchestrator().tick(state, language="Spanish")
    assert result["narration"]["language"] == "Spanish"
    assert result["narration"]["text"].startswith("Spanish:")
    assert state.narration_log[-1]["text"] == result["narration"]["text"]


def test_translator_uses_sdk_text_list_and_restores_names(monkeypatch):
    from types import SimpleNamespace

    import azure.ai.translation.text as translation_sdk
    from llm.translator import translate_narration

    monkeypatch.setenv("AZURE_TEXT_TRANSLATION_ENDPOINT", "https://translator.test")
    monkeypatch.setenv("AZURE_TEXT_TRANSLATION_APIKEY", "test-key")
    monkeypatch.setenv("AZURE_TEXT_TRANSLATION_REGION", "eastus")
    captured: tuple[list[str], list[str], str] | None = None

    class FakeTranslator:
        def __init__(self, **_: object) -> None:
            pass

        def translate(
            self,
            *,
            body: list[str],
            to_language: list[str],
            from_language: str,
        ) -> list[object]:
            nonlocal captured
            captured = (body, to_language, from_language)
            text = body[0]
            translated = text.replace("scores for", "anota para")
            return [
                SimpleNamespace(
                    translations=[SimpleNamespace(text=translated)]
                )
            ]

    monkeypatch.setattr(translation_sdk, "TextTranslationClient", FakeTranslator)
    result = translate_narration(
        "Rico Vale scores for Rovers", "Spanish", ["Rico Vale", "Rovers"]
    )
    assert captured == (
        ["OVERSEERNAME0TOKEN scores for OVERSEERNAME1TOKEN"],
        ["es"],
        "en",
    )
    assert result == "Rico Vale anota para Rovers"
