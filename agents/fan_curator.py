"""Structured fan-query compilation backed by the deterministic event parser."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from engine.state import MatchState
from llm.client import create_foundry_chat_client
from telemetry import traced_agent_call


logger = logging.getLogger(__name__)
AllowedEventType = Literal["pass", "shot", "tackle", "pressure", "foul"]
AllowedOutcome = Literal[
    "complete", "lost", "won", "blocked", "off_target",
    "on_target", "goal", "",
]


class FanQueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team: str | None = None
    player: str | None = None
    type: list[AllowedEventType] | None = None
    outcome: list[AllowedOutcome] | None = None


class FanCurator:
    def __init__(
        self,
        agent: Any | None = None,
        *,
        timeout_seconds: float = 4.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.agent = agent
        self.timeout_seconds = timeout_seconds

    def _get_agent(self) -> tuple[Any, bool]:
        if self.agent is not None:
            return self.agent, False
        agent = create_foundry_chat_client().as_agent(
            name="FanCurator",
            instructions=(
                "Convert a fan's request into filters using only the supplied "
                "team, player, event-type, and outcome values. Do not invent "
                "filters. Return only the requested structured JSON."
            ),
            default_options={"response_format": FanQueryFilters},
        )
        return agent, True

    def query(self, state: MatchState, request: str) -> dict[str, Any]:
        """Synchronous adapter for scripts and tests; use query_async in APIs."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.query_async(state, request))
        raise RuntimeError("FanCurator.query cannot run inside an event loop; await query_async")

    async def query_async(self, state: MatchState, request: str) -> dict[str, Any]:
        """Compile a fan request and return matching event records as clips."""
        filters = self._parse_request(state, request)
        compilation_source = "code"
        fallback_reason: str | None = None
        if filters is None:
            try:
                filters = await self._structured_filters(state, request)
                compilation_source = "agent_framework"
            except Exception as error:
                logger.warning(
                    "FanCurator structured query failed; using code parser (%s)",
                    error.__class__.__name__,
                    exc_info=True,
                )
                filters = self._parse_request(state, request) or {}
                fallback_reason = f"{error.__class__.__name__}: {error}"

        matches = [
            event for event in state.events
            if filters and self._matches(event, filters)
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
            "fallback_reason": fallback_reason,
        }

    async def _structured_filters(
        self,
        state: MatchState,
        request: str,
    ) -> dict[str, Any]:
        valid_teams = sorted({str(event.get("team", "")) for event in state.events})
        valid_players = sorted(
            {str(event.get("player", "")) for event in state.events if event.get("player")}
        )
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
        context_id = hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()
        last_error: Exception | None = None
        agent, owns_agent = self._get_agent()
        try:
            for attempt in range(2):
                try:
                    with traced_agent_call(
                        "FanCurator",
                        state,
                        fact_ids=(context_id,),
                    ) as span:
                        response = await asyncio.wait_for(
                            agent.run(prompt),
                            timeout=self.timeout_seconds,
                        )
                        parsed = FanQueryFilters.model_validate_json(response.text)
                        data = parsed.model_dump(exclude_none=True)
                        if data.get("team") not in (None, *valid_teams):
                            raise ValueError("FanCurator selected an unknown team")
                        if data.get("player") not in (None, *valid_players):
                            raise ValueError("FanCurator selected an unknown player")
                        output_id = hashlib.sha256(
                            json.dumps(
                                data,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                        span.set_attribute(
                            "overseer.fact.output_ids",
                            [f"fan-query:{output_id}"],
                        )
                        return data
                except Exception as error:
                    last_error = error
                    logger.warning(
                        "FanCurator call %d/2 failed (%s)",
                        attempt + 1,
                        error.__class__.__name__,
                        exc_info=True,
                    )
        finally:
            if owns_agent:
                await agent.close()
        if last_error is None:
            raise RuntimeError("FanCurator retry loop ended without a result")
        raise ValueError(
            f"structured FanCurator response unavailable: {last_error}"
        ) from last_error

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

        return filters or None

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
