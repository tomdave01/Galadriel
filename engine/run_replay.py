"""Replay a complete match at 50x and print agent findings."""

from __future__ import annotations

import argparse
from typing import Any

from engine.replay import ReplayDataset, ReplayEngine, default_datasets
from engine.state import MatchState


def _print_tick(result: dict[str, Any]) -> None:
    minute = result["minute"]
    shape_data = result["shape"].get("teams", {})
    print(f"\n[{minute:05.2f}'] ShapeAnalyst")
    for team in ("Rovers", "United"):
        team_data = shape_data.get(team, {})
        out_shape = team_data.get("out_of_possession", {})
        print(
            f"  {team}: {out_shape.get('formation', '?')} out of possession; "
            f"{team_data.get('block', {}).get('block', '?')}; "
            f"{team_data.get('marking', {}).get('style', '?')} marking"
        )

    print(f"[{minute:05.2f}'] PatternSpotter")
    findings = result["patterns"].get("findings", {})
    if not findings:
        print("  No patterns passed the configured thresholds.")
    for name, finding in findings.items():
        if "flank" in name:
            summary = f"{finding['top_share']:.0%} of pass/shot events use {finding['top_flank']}"
        elif "reliance" in name:
            summary = f"{finding['top_player']} accounts for {finding['share']:.0%} of involvement"
        elif "turnover" in name:
            summary = f"{finding['zone']} ({finding['share']:.0%} of turnovers)"
        else:
            trigger = finding["best_trigger"]
            summary = (
                f"{trigger} trigger, "
                f"{finding['triggers'][trigger]['rate']:.0%} press rate"
            )
        print(f"  {name}: {summary}")

    print(f"[{minute:05.2f}'] ImpactAnalyst")
    leaders = result["impact"][:3]
    print(
        "  Top impact: "
        + (
            ", ".join(f"{item['player']} {item['score']:.1f}" for item in leaders)
            if leaders else "not enough events yet"
        )
    )
    recent_momentum = result["momentum_series"][-1] if result["momentum_series"] else None
    if recent_momentum:
        print(f"  Latest momentum score (Rovers-positive): {recent_momentum['value']:+.2f}")

    print(f"[{minute:05.2f}'] MomentDetector")
    if result["moments"]:
        for moment in result["moments"]:
            print(f"  {moment['type']}: {moment['why']}")
    else:
        print("  No new goals, momentum swings, or shape changes.")

    narration = result["narration"]
    marker = " [MOCK]" if narration["mock"] else ""
    print(f"[{minute:05.2f}'] Narrator{marker}: {narration['text']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speed", type=float, default=50.0, help="replay speed multiplier (default: 50)")
    parser.add_argument("--events", help="synthetic events JSON path")
    parser.add_argument("--frames", help="synthetic frames JSON path")
    parser.add_argument("--clip-events", help="optional clip-first events JSON")
    parser.add_argument("--clip-frames", help="optional clip-first frames JSON")
    parser.add_argument("--mode", choices=("Broadcast", "Analyst", "Fan"), default="Broadcast")
    parser.add_argument(
        "--language",
        choices=("English", "Spanish", "Arabic", "French"),
        default="English",
    )
    parser.add_argument("--favourite-team", choices=("Rovers", "United"), default="Rovers")
    parser.add_argument("--favourite-player")
    arguments = parser.parse_args()

    if bool(arguments.clip_events) != bool(arguments.clip_frames):
        parser.error("--clip-events and --clip-frames must be provided together")

    clip = None
    if arguments.clip_events:
        clip = ReplayDataset.from_files(
            arguments.clip_events,
            arguments.clip_frames,
            name="video-clip",
            source="video",
        )
    datasets = default_datasets(
        events_path=arguments.events,
        frames_path=arguments.frames,
        clip=clip,
    )
    state = MatchState()
    engine = ReplayEngine(state, speed=arguments.speed, on_tick=_print_tick)
    print(f"Starting replay at {arguments.speed:g}x ({len(datasets)} dataset(s)).")
    engine.run(
        datasets,
        mode=arguments.mode,
        language=arguments.language,
        favourite_team=arguments.favourite_team,
        favourite_player=arguments.favourite_player,
    )
    print(
        f"\nReplay complete: {state.minute:.2f}' "
        f"Rovers {state.score.get('Rovers', 0)}-{state.score.get('United', 0)} United; "
        f"{len(state.events)} events, {len(state.frames)} frames."
    )


if __name__ == "__main__":
    main()
