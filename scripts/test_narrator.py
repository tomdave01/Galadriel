"""Exercise the Foundry Narrator in three presentation modes and languages."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.narrator import Narrator
from engine.state import MatchState


SAMPLE_FACT = {
    "id": "block_change",
    "team": "United",
    "from": "mid block",
    "to": "low block",
    "minute": 63,
    "why": "momentum swung +0.4 to Rovers after United's line dropped 11 units",
}


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    state = MatchState(minute=float(SAMPLE_FACT["minute"]))
    state.shape["teams"] = {"Rovers": {}, "United": {}}
    state.moments.append(SAMPLE_FACT)
    narrator = Narrator(timeout_seconds=30.0)

    for mode, language in (
        ("Broadcast", "English"),
        ("Analyst", "Spanish"),
        ("Fan", "Arabic"),
    ):
        result = await narrator.narrate(
            state,
            mode=mode,
            language=language,
            favourite_team="United",
        )
        print(f"{mode}/{language}: {result['text']}")


if __name__ == "__main__":
    asyncio.run(main())
