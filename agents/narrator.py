"""Fact-grounded narration in the supported presentation modes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from feed.persistence import InMemoryNarrationCache, NarrationCache
from engine.state import MatchState
from llm.client import create_foundry_chat_client
from llm.safety import content_safety_enabled, narrator_output_is_safe
from llm.translator import translate_narration, translation_enabled
from telemetry import traced_agent_call


SUPPORTED_LANGUAGES = {"English", "Spanish", "Arabic", "French"}
SUPPORTED_MODES = {"Broadcast", "Analyst", "Fan"}
logger = logging.getLogger(__name__)
_INSTRUCTIONS = (
    "Write one short football narration in the requested language and mode. "
    "Use only facts in the supplied JSON facts object; do not infer or invent "
    "statistics, events, or identities. Never translate player or club names. "
    "Broadcast is a punchy fact. Analyst is metric-heavy and explains the "
    "measured evidence. Fan is short, emotional, and plain-language, from the "
    "favourite team's side when specified. Return plain text only."
)


class Narrator:
    def __init__(
        self,
        agent: Any | None = None,
        *,
        cache: NarrationCache | None = None,
        timeout_seconds: float = 4.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.agent = agent
        self.cache = cache or InMemoryNarrationCache()
        self.timeout_seconds = timeout_seconds

    def _get_agent(self) -> Any:
        if self.agent is None:
            return create_foundry_chat_client().as_agent(
                name="Narrator",
                instructions=_INSTRUCTIONS,
            )
        return self.agent

    async def narrate(
        self,
        state: MatchState,
        *,
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> dict[str, Any]:
        if mode not in SUPPORTED_MODES:
            raise ValueError(f"Unsupported narration mode: {mode}")
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError(f"Unsupported narration language: {language}")
        facts = self._facts(state, favourite_team, favourite_player)
        fact_id = hashlib.sha256(
            json.dumps(
                facts,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        favourite = json.dumps(
            [favourite_team, favourite_player],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fallback = self._template(facts, mode, language)
        prompt = json.dumps(
            {
                "mode": mode,
                "language": language,
                "favourite_team": favourite_team,
                "favourite_player": favourite_player,
                "facts": facts,
            },
            ensure_ascii=False,
        )
        with traced_agent_call("Narrator", state, fact_ids=(fact_id,)) as span:
            cached = await self.cache.get(fact_id, mode, language, favourite)
            if cached is not None:
                span.set_attribute("overseer.cache.hit", True)
                text = cached
                fallback_used = False
                reason = None
            else:
                span.set_attribute("overseer.cache.hit", False)
                text, fallback_used, reason = await self._generate(
                    prompt,
                    fallback,
                    fact_id=fact_id,
                )
                if (
                    fallback_used
                    and translation_enabled()
                    and language != "English"
                ):
                    names = {
                        str(event["player"])
                        for event in state.events
                        if event.get("player")
                    }
                    names.update(("Rovers", "United"))
                    try:
                        translated = await asyncio.wait_for(
                            asyncio.to_thread(
                                translate_narration,
                                self._template(facts, mode, "English"),
                                language,
                                sorted(names, key=len, reverse=True),
                            ),
                            timeout=self.timeout_seconds,
                        )
                        text = translated
                    except Exception as error:
                        logger.warning(
                            "Translator call failed; retaining native template (%s)",
                            error.__class__.__name__,
                            exc_info=True,
                        )
                        reason = f"TranslatorError: {error.__class__.__name__}"
                if content_safety_enabled():
                    try:
                        is_safe = await asyncio.to_thread(
                            narrator_output_is_safe,
                            text,
                        )
                    except Exception as error:
                        logger.warning(
                            "Content Safety check failed; using template fallback (%s)",
                            error.__class__.__name__,
                            exc_info=True,
                        )
                        is_safe = False
                        reason = f"ContentSafetyError: {error.__class__.__name__}"
                    if not is_safe:
                        text = fallback
                        fallback_used = True
                        reason = reason or "ContentSafetyRejected"
                if not fallback_used:
                    await self.cache.put(
                        fact_id,
                        mode,
                        language,
                        favourite,
                        text,
                    )
            span.set_attribute("overseer.fact.output_ids", [fact_id])

        entry = {
            "minute": round(state.minute, 2),
            "mode": mode,
            "language": language,
            "favourite_team": favourite_team,
            "favourite_player": favourite_player,
            "fact_id": fact_id,
            "text": text,
            "fallback": fallback_used,
            "reason": reason,
            "facts": facts,
        }
        state.narration_log.append(entry)
        return entry

    async def _generate(
        self,
        prompt: str,
        fallback: str,
        *,
        fact_id: str,
    ) -> tuple[str, bool, str | None]:
        last_error: Exception | None = None
        for attempt in range(2):
            agent: Any | None = None
            owns_agent = self.agent is None
            try:
                agent = self._get_agent()
                if agent is None:
                    raise RuntimeError("Foundry Narrator agent was not created")
                response = await asyncio.wait_for(
                    agent.run(prompt),
                    timeout=self.timeout_seconds,
                )
                text = response.text.strip()
                if not text:
                    raise ValueError("Foundry Narrator returned empty text")
                return text, False, None
            except Exception as error:
                last_error = error
                logger.warning(
                    "Narrator call %d/2 failed for fact %s (%s)",
                    attempt + 1,
                    fact_id,
                    error.__class__.__name__,
                    exc_info=True,
                )
            finally:
                if owns_agent and agent is not None:
                    await agent.close()
        if last_error is None:
            raise RuntimeError("Narrator retry loop ended without a result")
        return fallback, True, f"{last_error.__class__.__name__}: {last_error}"

    @staticmethod
    def _facts(
        state: MatchState,
        favourite_team: str | None,
        favourite_player: str | None,
    ) -> dict[str, Any]:
        recent_moments = [
            moment for moment in state.moments
            if float(moment.get("minute", 0.0)) >= state.minute - 10.0
        ][-5:]
        top_players = state.impact[:3]
        selected_player = next(
            (item for item in state.impact if item["player"] == favourite_player),
            None,
        )
        team = favourite_team if favourite_team in state.shape.get("teams", {}) else "Rovers"
        team_shape = state.shape.get("teams", {}).get(team, {})
        return {
            "minute": round(state.minute, 2),
            "score": dict(state.score),
            "team_focus": team,
            "favourite_player": favourite_player,
            "favourite_player_impact": selected_player,
            "shape": {
                "in_possession": team_shape.get("in_possession"),
                "out_of_possession": team_shape.get("out_of_possession"),
                "block": team_shape.get("block"),
                "marking": team_shape.get("marking"),
            },
            "patterns": state.patterns.get("findings", {}),
            "top_impact": top_players,
            "latest_moments": recent_moments,
            "momentum": state.momentum_series[-2:],
        }

    @staticmethod
    def _template(facts: dict[str, Any], mode: str, language: str) -> str:
        team = facts["team_focus"]
        minute = facts["minute"]
        score = facts["score"]
        moments = facts["latest_moments"]
        patterns = facts["patterns"]
        best_player = facts["favourite_player_impact"]
        best = best_player or (facts["top_impact"][0] if facts["top_impact"] else None)
        scoring_moment = next((moment for moment in reversed(moments) if moment["type"] == "goal"), None)
        notable_pattern = next(iter(patterns.values()), None)

        if language == "Spanish":
            score_text = f"Rovers {score.get('Rovers', 0)}-{score.get('United', 0)} United"
            if mode == "Fan":
                if scoring_moment:
                    return f"¡Gol de {scoring_moment['team']}! {score_text}."
                return f"¡Vamos, {team}! {score_text}; seguimos en la pelea."
            if notable_pattern and notable_pattern.get("top_flank"):
                return f"{team}: {notable_pattern['top_share']:.0%} de las acciones por la banda {notable_pattern['top_flank']}. {score_text}."
            return f"Minuto {minute:.0f}: {score_text}. {best['player'] if best else 'El partido'} lidera el impacto disponible."
        if language == "French":
            score_text = f"Rovers {score.get('Rovers', 0)}-{score.get('United', 0)} United"
            if mode == "Fan":
                if scoring_moment:
                    return f"BUT de {scoring_moment['team']} ! {score_text}."
                return f"Allez {team} ! {score_text}, rien n'est joué."
            if notable_pattern and notable_pattern.get("top_flank"):
                return f"{team} : {notable_pattern['top_share']:.0%} des actions côté {notable_pattern['top_flank']}. {score_text}."
            return f"{minute:.0f}e minute : {score_text}. {best['player'] if best else 'Le match'} mène l'impact mesuré."
        if language == "Arabic":
            score_text = f"Rovers {score.get('Rovers', 0)}-{score.get('United', 0)} United"
            if mode == "Fan":
                if scoring_moment:
                    return f"هدف لـ {scoring_moment['team']}! {score_text}."
                return f"هيا يا {team}! {score_text}، المباراة مستمرة."
            if notable_pattern and notable_pattern.get("top_flank"):
                return f"{team}: {notable_pattern['top_share']:.0%} من التحركات عبر الجهة {notable_pattern['top_flank']}. {score_text}."
            return f"الدقيقة {minute:.0f}: {score_text}. {best['player'] if best else 'المباراة'} يتصدر التأثير المسجل."

        score_text = f"Rovers {score.get('Rovers', 0)}-{score.get('United', 0)} United"
        if mode == "Fan":
            if scoring_moment:
                return f"Goal for {scoring_moment['team']}! {score_text} — come on, {team}!"
            if best_player:
                return f"Come on, {team}! {score_text}; {best_player['player']} leads the measured impact."
            return f"Come on, {team}! {score_text}; there's still a match to play."
        if mode == "Analyst":
            shape_data = facts["shape"].get("out_of_possession") or {}
            block_data = facts["shape"].get("block") or {}
            marking_data = facts["shape"].get("marking") or {}
            player_text = (
                f"{best['player']} leads the measured impact at {best['score']:.1f}/100. "
                if best else ""
            )
            return (
                f"{minute:.0f}' | {score_text}. {team} show a "
                f"{shape_data.get('formation', 'not available')} out-of-possession shape, "
                f"{block_data.get('block', 'block not available')}, and "
                f"{marking_data.get('style', 'marking not available')} marking. "
                f"{player_text}These are measured patterns, not causal claims."
            )
        if scoring_moment:
            return f"{scoring_moment['minute']:.0f}' — {scoring_moment['player']} scores for {scoring_moment['team']}. {score_text}."
        if notable_pattern and notable_pattern.get("top_flank"):
            return f"{team} send {notable_pattern['top_share']:.0%} of their pass and shot events down the {notable_pattern['top_flank']}; {score_text}."
        if best:
            return f"{minute:.0f}' | {score_text}. {best['player']} leads the measured impact."
        return f"{minute:.0f}' | {score_text}."
