"""Publish processed clip data followed by a synthetic match to the feed."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from feed.clients import FeedProducer, create_feed_producer
from feed.models import FeedMessage, Source
from generator.match import MATCH_ID, generate_match


def _load_clip(
    events_path: str | None, frames_path: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    if bool(events_path) != bool(frames_path):
        raise ValueError(
            "SIMULATOR_CLIP_EVENTS_PATH and SIMULATOR_CLIP_FRAMES_PATH "
            "must be configured together"
        )
    if not events_path or not frames_path:
        return None
    try:
        events = json.loads(Path(events_path).read_text(encoding="utf-8"))
        frames = json.loads(Path(frames_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unable to load processed clip data: {error}") from error
    if not isinstance(events, list) or not isinstance(frames, list):
        raise ValueError("Processed clip events and frames must be JSON arrays")
    if not all(isinstance(item, dict) for item in [*events, *frames]):
        raise ValueError("Processed clip records must be JSON objects")
    return events, frames


def _records(
    events: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    fallback_match_id: str,
    match_id_override: str | None,
) -> list[tuple[float, str, str, dict[str, Any]]]:
    records: list[tuple[float, int, str, str, dict[str, Any]]] = []
    for kind, values, order in (
        ("event", events, 0),
        ("frame", frames, 1),
    ):
        for value in values:
            local_time = (
                float(value["t"])
                if value.get("t") is not None
                else float(value.get("minute", 0.0)) * 60.0
            )
            if local_time < 0:
                raise ValueError("Feed records must not have negative timestamps")
            if (
                kind == "event"
                and not value.get("match_id")
                and match_id_override is None
            ):
                raise ValueError("Processed event is missing its match_id")
            match_id = match_id_override or str(
                value.get("match_id") or fallback_match_id
            )
            records.append((local_time, order, kind, match_id, value))
    records.sort(key=lambda item: (item[0], item[1]))
    return [
        (timestamp, kind, match_id, record)
        for timestamp, _, kind, match_id, record in records
    ]


async def simulate(
    producer: FeedProducer,
    *,
    speed: float = 1.0,
    seed: int = 42,
    match_id: str | None = None,
    clip_events_path: str | None = None,
    clip_frames_path: str | None = None,
) -> int:
    if speed <= 0:
        raise ValueError("speed must be greater than zero")
    clip = _load_clip(clip_events_path, clip_frames_path)
    datasets: list[
        tuple[Source, list[dict[str, Any]], list[dict[str, Any]], str]
    ] = []
    if clip is not None:
        events, frames = clip
        clip_match_id = match_id or next(
            (
                str(event["match_id"])
                for event in events
                if event.get("match_id")
            ),
            "video-match",
        )
        datasets.append(("video", events, frames, clip_match_id))

    synthetic_events, synthetic_frames = generate_match(seed=seed)
    synthetic_match_id = match_id or MATCH_ID
    datasets.append(
        ("synthetic", synthetic_events, synthetic_frames, synthetic_match_id)
    )

    sequences: dict[str, int] = {}
    clock_offsets: dict[str, float] = {}
    last_clocks: dict[str, float] = {}
    published = 0
    for source, events, frames, fallback_match_id in datasets:
        previous_local_time: float | None = None
        dataset_clocks: dict[str, float] = {}
        records = _records(events, frames, fallback_match_id, match_id)
        for local_time, kind, record_match_id, record in records:
            if previous_local_time is not None:
                delay = max(0.0, local_time - previous_local_time) / speed
                if delay:
                    await asyncio.sleep(delay)
            previous_local_time = local_time
            sequence = sequences.get(record_match_id, 0)
            match_clock = max(
                clock_offsets.get(record_match_id, 0.0) + local_time,
                last_clocks.get(record_match_id, 0.0),
            )
            message = FeedMessage(
                match_id=record_match_id,
                seq=sequence,
                match_clock=match_clock,
                wall_time_utc=datetime.now(timezone.utc).isoformat(),
                source=source,
                payload={
                    "kind": kind,
                    "record": (
                        {**record, "match_id": record_match_id}
                        if kind == "event"
                        else record
                    ),
                },
            )
            await producer.publish(message)
            sequences[record_match_id] = sequence + 1
            last_clocks[record_match_id] = match_clock
            dataset_clocks[record_match_id] = match_clock
            published += 1
        for record_match_id, final_clock in dataset_clocks.items():
            clock_offsets[record_match_id] = final_clock + 0.2
    return published


async def _run(arguments: argparse.Namespace) -> None:
    producer = create_feed_producer()
    try:
        count = await simulate(
            producer,
            speed=arguments.speed,
            seed=arguments.seed,
            match_id=arguments.match_id,
            clip_events_path=(
                arguments.clip_events
                or os.getenv("SIMULATOR_CLIP_EVENTS_PATH")
            ),
            clip_frames_path=(
                arguments.clip_frames
                or os.getenv("SIMULATOR_CLIP_FRAMES_PATH")
            ),
        )
        print(f"Published {count} feed messages.")
    finally:
        await producer.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--speed",
        type=float,
        default=float(os.getenv("SIMULATOR_SPEED", "1")),
        help="playback speed multiplier (default: 1)",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--match-id",
        help="optional shared match ID for processed clip and synthetic records",
    )
    parser.add_argument("--clip-events", help="processed clip events JSON")
    parser.add_argument("--clip-frames", help="processed clip frames JSON")
    arguments = parser.parse_args(argv)
    asyncio.run(_run(arguments))


if __name__ == "__main__":
    main()
