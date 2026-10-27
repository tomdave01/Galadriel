"""Generate the synthetic match and print every analysis result."""

from __future__ import annotations

import json
import textwrap

from generator.match import generate_match
from engine.patterns import (
    block,
    flank_use,
    influence,
    marking,
    momentum,
    press_triggers,
    reliance,
    shape,
    turnover_zones,
)


def main() -> None:
    events, frames = generate_match()
    all_influence = influence(events)
    all_momentum = momentum(events)
    rows: list[tuple[str, str, object]] = []
    for team, opponent in (("Rovers", "United"), ("United", "Rovers")):
        rows.extend(
            (
                ("shape (in possession)", team, shape(frames, team, True)),
                ("shape (out of possession)", team, shape(frames, team, False)),
                ("block", team, block(frames, team)),
                ("flank use", team, flank_use(events, team)),
                ("reliance", team, reliance(events, team)),
                ("turnover zones", team, turnover_zones(events, team)),
                ("press triggers", team, press_triggers(frames, team, events)),
                ("marking", team, marking(frames, team, opponent)),
                (
                    "momentum",
                    team,
                    [
                        {
                            "minute": item["minute"],
                            "score": item[team.lower()],
                            "team_relative": round(
                                item["value"] if team == "Rovers" else -item["value"], 4
                            ),
                        }
                        for item in all_momentum
                    ],
                ),
                (
                    "influence",
                    team,
                    [item for item in all_influence if item["team"] == team],
                ),
            )
        )

    print(f"Synthetic match: {len(events)} events, {len(frames)} frames")
    print(f"{'FUNCTION':<28} {'TEAM':<8} RESULT")
    print("-" * 100)
    for function, team, result in rows:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        wrapped = textwrap.wrap(encoded, width=70, subsequent_indent=" " * 40) or [""]
        print(f"{function:<28} {team:<8} {wrapped[0]}")
        for line in wrapped[1:]:
            print(f"{'':<28} {'':<8} {line}")


if __name__ == "__main__":
    main()
