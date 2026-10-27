from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential

from agents import FanCurator, Orchestrator
from agents.framework_agents import build_agent_suite
from agents.narrator import Narrator
from engine.replay import ReplayDataset, ReplayEngine
from engine.state import MatchState, ingest
from feed.persistence import InMemoryNarrationCache
from azure.identity import CredentialUnavailableError


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


class _FakeAgent:
    def __init__(self, text: str, *, delay: float = 0) -> None:
        self.text = text
        self.delay = delay
        self.calls = 0

    async def run(self, _prompt: str) -> SimpleNamespace:
        self.calls += 1
        assert _prompt
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(text=self.text)


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
    result = FanCurator().query(state, "Rovers Rico Vale shots on target")
    assert result["compiled_by"] == "code"
    assert result["count"] == 1
    assert result["clips"][0]["player"] == "Rico Vale"


def test_fan_curator_parses_team_player_and_event_filters():
    state = MatchState(events=[
        _event("Rovers", "Rico Vale", "on_target"),
        _event("Rovers", "Rico Vale", "off_target"),
        _event("United", "Ira West", "on_target"),
    ])
    result = FanCurator().query(state, "Rovers Rico Vale shots on target")
    assert result["filters"] == {
        "team": "Rovers",
        "player": "Rico Vale",
        "type": ["shot"],
        "outcome": ["on_target", "goal"],
    }
    assert result["count"] == 1


def test_fan_curator_rejects_unlisted_structured_filters():
    state = MatchState(events=[_event("Rovers", "Rico Vale", "goal")])
    curator = FanCurator(agent=_FakeAgent('{"team":"Invented FC","type":["shot"]}'))

    result = asyncio.run(curator.query_async(state, "show me tactical sequences"))

    assert result["filters"] == {}
    assert result["count"] == 0
    assert result["compiled_by"] == "code"
    assert result["fallback_reason"]


def test_fan_curator_accepts_schema_validated_filters():
    state = MatchState(events=[_event("Rovers", "Rico Vale", "goal")])
    curator = FanCurator(
        agent=_FakeAgent('{"team":"Rovers","type":["shot"],"outcome":["goal"]}')
    )

    result = asyncio.run(curator.query_async(state, "show me decisive moments"))

    assert result["compiled_by"] == "agent_framework"
    assert result["count"] == 1
    assert result["filters"]["team"] == "Rovers"


def test_narrator_uses_templated_fallback_after_two_timeouts():
    state = MatchState()
    agent = _FakeAgent("too late", delay=0.05)

    result = asyncio.run(
        Narrator(agent=agent, timeout_seconds=0.001).narrate(
            state,
            mode="Broadcast",
            language="English",
        )
    )

    assert agent.calls == 2
    assert result["fallback"] is True
    assert result["fact_id"]
    assert result["text"]


def test_narrator_content_safety_replaces_rejected_output(monkeypatch):
    monkeypatch.setenv("AZURE_CONTENT_SAFETY_ENABLED", "1")
    monkeypatch.setattr("agents.narrator.narrator_output_is_safe", lambda text: not text)

    result = asyncio.run(
        Narrator(agent=_FakeAgent("Rejected generated narration.")).narrate(
            MatchState(),
            language="English",
        )
    )

    assert result["fallback"] is True
    assert result["reason"] == "ContentSafetyRejected"
    assert "Rejected generated narration" not in result["text"]


def test_optional_translator_only_translates_template_fallback(monkeypatch):
    monkeypatch.setenv("AZURE_TRANSLATOR_ENABLED", "1")
    translated_input: list[str] = []

    def fake_translate(text: str, language: str, names: list[str]) -> str:
        translated_input.append(text)
        assert language == "Spanish"
        assert "Rico Vale" in names
        assert "Rovers" in names and "United" in names
        return f"{language}:{text}"

    monkeypatch.setattr("agents.narrator.translate_narration", fake_translate)
    state = MatchState(events=[_event("Rovers", "Rico Vale", "goal")])
    narrator = Narrator(
        agent=_FakeAgent("late response", delay=0.2),
        timeout_seconds=0.05,
    )

    result = asyncio.run(narrator.narrate(state, language="Spanish"))

    assert result["fallback"] is True
    assert result["text"].startswith("Spanish:")
    assert "Rovers" in translated_input[0]


def test_narrator_cache_separates_mode_language_and_favourite():
    state = MatchState()
    agent = _FakeAgent("Grounded generated narration.")
    cache = InMemoryNarrationCache()
    narrator = Narrator(agent=agent, cache=cache)

    async def run() -> list[dict]:
        first = await narrator.narrate(state, favourite_team="Rovers")
        repeated = await narrator.narrate(state, favourite_team="Rovers")
        analyst = await narrator.narrate(
            state,
            mode="Analyst",
            favourite_team="Rovers",
        )
        spanish = await narrator.narrate(
            state,
            language="Spanish",
            favourite_team="Rovers",
        )
        rival = await narrator.narrate(state, favourite_team="United")
        return [first, repeated, analyst, spanish, rival]

    results = asyncio.run(run())
    assert agent.calls == 4
    assert results[0]["text"] == results[1]["text"]
    assert results[0]["fact_id"] == results[1]["fact_id"]
    assert len({result["mode"] for result in (results[0], results[2])}) == 2
    assert results[3]["language"] == "Spanish"
    assert results[4]["favourite_team"] == "United"


def test_orchestrator_keeps_deterministic_analysis_and_template_fallback():
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

    assert output["narration"]["fallback"] is True
    assert "Rovers" in output["narration"]["text"]
    assert state.moments[0]["type"] == "goal"


def test_orchestrator_yields_facts_before_starting_narrator():
    state = MatchState()
    agent = _FakeAgent("Grounded generated narration.")
    orchestrator = Orchestrator(narrator=Narrator(agent=agent))

    async def collect_events() -> list[dict]:
        events = orchestrator.iter_tick_events(state)
        first = await anext(events)
        assert first["type"] == "fact"
        assert agent.calls == 0
        second = await anext(events)
        return [first, second]

    events = asyncio.run(collect_events())

    assert events[1]["type"] == "narration"
    assert agent.calls == 1


def test_replay_engine_emits_facts_before_narration():
    agent = _FakeAgent("Grounded generated narration.")
    orchestrator = Orchestrator(narrator=Narrator(agent=agent))
    callbacks: list[str] = []

    def on_fact(_facts: dict) -> None:
        assert isinstance(_facts, dict)
        assert agent.calls == 0
        callbacks.append("fact")

    def on_tick(_tick: dict) -> None:
        assert isinstance(_tick, dict)
        assert agent.calls == 1
        callbacks.append("narration")

    replay = ReplayEngine(
        orchestrator=orchestrator,
        speed=1_000_000,
        on_fact=on_fact,
        on_tick=on_tick,
    )
    replay.run([
        ReplayDataset(
            name="single",
            source="synthetic",
            frames=[{
                "t": 0.0,
                "minute": 0.0,
                "poss": "Rovers",
                "ball": {"x": 50.0, "y": 50.0},
                "players": [],
            }],
        )
    ])

    assert callbacks == ["fact", "narration"]


def test_agent_framework_suite_builds_sequential_workflow():
    state = MatchState()
    client = FoundryChatClient(
        project_endpoint="https://example.services.ai.azure.com/api/projects/test",
        model="model",
        credential=AzureCliCredential(),
    )
    orchestrator = Orchestrator()

    suite = build_agent_suite(
        state=state,
        shape_analyst=orchestrator.shape_tool,
        pattern_spotter=orchestrator.pattern_tool,
        impact_analyst=orchestrator.impact_tool,
        moment_detector=orchestrator.moment_tool,
        narrator=orchestrator.narrator,
        fan_curator=orchestrator.fan_curator,
        client=client,
    )

    assert suite.orchestrator.name == "Orchestrator"
    assert suite.shape_analyst.name == "ShapeAnalyst"
    assert suite.fan_curator.name == "FanCurator"
    assert suite.workflow is not None


def test_translator_uses_sdk_text_list_and_restores_names(monkeypatch):
    from types import SimpleNamespace

    import azure.ai.translation.text as translation_sdk
    import llm.translator as translator_module
    from llm.translator import translate_narration

    monkeypatch.setenv("AZURE_TEXT_TRANSLATION_ENDPOINT", "https://translator.test")
    monkeypatch.setenv("AZURE_TEXT_TRANSLATION_REGION", "eastus")
    captured: tuple[list[str], list[str], str] | None = None

    class FakeCredential:
        def close(self) -> None:
            pass

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

        def close(self) -> None:
            pass

    monkeypatch.setattr(translator_module, "DefaultAzureCredential", FakeCredential)
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


def test_foundry_uses_api_key_only_after_credential_failure(monkeypatch):
    import llm.client as client_module

    captured: dict[str, object] = {}

    class UnavailableCredential:
        def get_token(self, scope: str) -> None:
            assert scope
            raise CredentialUnavailableError("no Entra credential")

        def close(self) -> None:
            captured["credential_closed"] = True

    def fake_foundry_client(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return kwargs

    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://example.services.ai.azure.com/api/projects/test",
    )
    monkeypatch.setenv("FOUNDRY_MODEL", "test-deployment")
    monkeypatch.setenv("FOUNDRY_API_KEY", "unit-test-key")
    monkeypatch.setattr(client_module, "DefaultAzureCredential", UnavailableCredential)
    monkeypatch.setattr(client_module, "FoundryChatClient", fake_foundry_client)

    client_module.create_foundry_chat_client()

    assert captured["model"] == "test-deployment"
    assert captured["default_headers"] == {"api-key": "unit-test-key"}
    assert "credential" not in captured
    assert captured["credential_closed"] is True
