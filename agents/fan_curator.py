"""Natural-language-to-event filters and matching event clip references."""

from __future__ import annotations

import json
from typing import Any

from engine.state import MatchState
from llm.client import LLMClient


class FanCurator:
    def __init__(self, client: LLMClient | None = None) -> None:
        self.client = client or LLMClient()

    def query(self, state: MatchState, request: str) -> dict[str, Any]:
        """Compile a fan request and return matching event records as clips."""
        filters = self._parse_request(state, request)
        compilation_source = "code"
        if filters is None:
            filters = self._llm_filters(state, request)
            compilation_source = "llm"

        matches = [
            event for event in state.events
            if self._matches(event, filters)
        ]
        clips = [
            {
                "clip_id": event.get("clip_id"),
                "minute": event.get("minute"),
                "t": event.get("t"),
                "team": event.get("team"),
                "player": event.get("player"),
                "type": event.get("type"),
                "outcome": event.get("outcome"),
                "event": event,
            }
            for event in matches
        ]
        return {
            "request": request,
            "filters": filters,
            "clips": clips,
            "count": len(clips),
            "compiled_by": compilation_source,
        }

    def _parse_request(
        self, state: MatchState, request: str
    ) -> dict[str, Any] | None:
        text = request.casefold()
        filters: dict[str, Any] = {}
        teams = {str(event.get("team", "")) for event in state.events}
        team = next((name for name in teams if name and name.casefold() in text), None)
        if team:
            filters["team"] = team

        player_names = sorted(
            {str(event.get("player", "")) for event in state.events if event.get("player")},
            key=len,
            reverse=True,
        )
        player = next((name for name in player_names if name.casefold() in text), None)
        if player:
            filters["player"] = player

        if any(phrase in text for phrase in ("missed chance", "missed chances", "missed shot", "missed shots")):
            filters.update({"type": ["shot"], "outcome": ["off_target", "blocked"]})
        elif any(phrase in text for phrase in ("shots on target", "shot on target", "on-target shots")):
            filters.update({"type": ["shot"], "outcome": ["on_target", "goal"]})
        elif "goal" in text or "goals" in text:
            filters.update({"type": ["shot"], "outcome": ["goal"]})
        elif "shot" in text or "shots" in text:
            filters["type"] = ["shot"]
        elif "tackle" in text or "tackles" in text:
            filters["type"] = ["tackle"]
        elif "pass" in text or "passes" in text:
            filters["type"] = ["pass"]
            if any(word in text for word in ("lost", "failed", "incomplete")):
                filters["outcome"] = ["lost"]
        elif "pressure" in text or "pressures" in text:
            filters["type"] = ["pressure"]

        if filters:
            return filters
        return None

    def _llm_filters(self, state: MatchState, request: str) -> dict[str, Any]:
        valid_teams = sorted({str(event.get("team", "")) for event in state.events})
        valid_players = sorted({str(event.get("player", "")) for event in state.events})
        prompt = json.dumps(
            {
                "request": request,
                "allowed_teams": valid_teams,
                "allowed_players": valid_players,
                "allowed_types": ["pass", "shot", "tackle", "pressure", "foul"],
                "allowed_outcomes": [
                    "complete", "lost", "won", "blocked", "off_target",
                    "on_target", "goal", "",
                ],
            },
            ensure_ascii=False,
        )
        response = self.client.complete(
            prompt,
            system=(
                "Convert the request to a JSON object with optional team, player, "
                "type, and outcome filters. Each filter value must be selected from "
                "the provided allowed values. Use arrays for type and outcome. "
                "Return only JSON."
            ),
            fallback_text='{"filters":{}}',
            json_mode=True,
        )
        try:
            parsed = json.loads(response["text"])
        except json.JSONDecodeError:
            parsed = {}
        raw = parsed.get("filters", parsed) if isinstance(parsed, dict) else {}
        if not isinstance(raw, dict):
            return {}

        allowed = {
            "team": set(valid_teams),
            "player": set(valid_players),
            "type": {"pass", "shot", "tackle", "pressure", "foul"},
            "outcome": {
                "complete", "lost", "won", "blocked", "off_target",
                "on_target", "goal", "",
            },
        }
        filters: dict[str, Any] = {}
        for key, choices in allowed.items():
            value = raw.get(key)
            if isinstance(value, str) and value in choices:
                filters[key] = value
            elif isinstance(value, list):
                selected = [item for item in value if isinstance(item, str) and item in choices]
                if selected:
                    filters[key] = selected
        return filters

    @staticmethod
    def _matches(event: dict[str, Any], filters: dict[str, Any]) -> bool:
        for key, expected in filters.items():
            actual = event.get(key)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True
