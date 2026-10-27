"""Time-ordered replay of clip and synthetic match records."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from agents.orchestrator import Orchestrator
from engine.state import MatchState, ingest


@dataclass
class ReplayDataset:
    name: str
    source: str
    events: list[dict[str, Any]] = field(default_factory=list)
    frames: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_files(
        cls,
        events_path: str | Path,
        frames_path: str | Path,
        *,
        name: str,
        source: str,
    ) -> "ReplayDataset":
        event_file = Path(events_path)
        frame_file = Path(frames_path)
        try:
            events = json.loads(event_file.read_text(encoding="utf-8"))
            frames = json.loads(frame_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Unable to load replay dataset {name}: {error}") from error
        if not isinstance(events, list) or not isinstance(frames, list):
            raise ValueError(f"Replay dataset {name} must contain JSON arrays")
        return cls(name=name, source=source, events=events, frames=frames)


TickCallback = Callable[[dict[str, Any]], None]


class ReplayEngine:
    def __init__(
        self,
        state: MatchState | None = None,
        orchestrator: Orchestrator | None = None,
        *,
        speed: float = 1.0,
        tick_interval: float = 2.5,
        on_tick: TickCallback | None = None,
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be greater than zero")
        if tick_interval <= 0:
            raise ValueError("tick_interval must be greater than zero")
        self.state = state or MatchState()
        self.orchestrator = orchestrator or Orchestrator()
        self.speed = speed
        self.tick_interval = tick_interval
        self.on_tick = on_tick
        self.tick_results: list[dict[str, Any]] = []

    def run(
        self,
        datasets: Iterable[ReplayDataset],
        *,
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> MatchState:
        """Ingest datasets in supplied order and periodically run all agents."""
        dataset_list = list(datasets)
        if not dataset_list:
            raise ValueError("at least one replay dataset is required")
        next_tick_minute = self.state.minute
        last_timestamp: float | None = None
        timeline_offset = self.state.minute * 60.0

        for dataset in dataset_list:
            records = self._ordered_records(dataset, timeline_offset)
            for timestamp, kind, value in records:
                if last_timestamp is not None and timestamp > last_timestamp:
                    time.sleep((timestamp - last_timestamp) / self.speed)
                last_timestamp = timestamp
                if kind == "event":
                    ingest(self.state, event=value)
                else:
                    ingest(self.state, frame=value)

                if self.state.minute >= next_tick_minute:
                    self._tick(
                        mode=mode,
                        language=language,
                        favourite_team=favourite_team,
                        favourite_player=favourite_player,
                    )
                    next_tick_minute = self.state.minute + self.tick_interval

            if records:
                timeline_offset = max(timestamp for timestamp, _, _ in records)

        if not self.tick_results or self.tick_results[-1]["minute"] < round(self.state.minute, 2):
            self._tick(
                mode=mode,
                language=language,
                favourite_team=favourite_team,
                favourite_player=favourite_player,
            )
        return self.state

    def _tick(
        self,
        *,
        mode: str,
        language: str,
        favourite_team: str | None,
        favourite_player: str | None,
    ) -> None:
        result = self.orchestrator.tick(
            self.state,
            mode=mode,
            language=language,
            favourite_team=favourite_team,
            favourite_player=favourite_player,
        )
        self.tick_results.append(result)
        if self.on_tick:
            self.on_tick(result)

    @staticmethod
    def _ordered_records(
        dataset: ReplayDataset, timeline_offset: float
    ) -> list[tuple[float, str, dict[str, Any]]]:
        records: list[tuple[float, int, str, dict[str, Any]]] = []
        for event in dataset.events:
            local_time = (
                float(event["t"])
                if event.get("t") is not None
                else float(event.get("minute", 0.0)) * 60.0
            )
            absolute_time = timeline_offset + local_time
            normalized = dict(event)
            normalized["minute"] = absolute_time / 60.0
            normalized["source"] = str(event.get("source", dataset.source))
            if event.get("t") is not None:
                normalized["t"] = absolute_time
            records.append((absolute_time, 0, "event", normalized))

        for frame in dataset.frames:
            local_time = (
                float(frame["t"])
                if frame.get("t") is not None
                else float(frame.get("minute", 0.0)) * 60.0
            )
            absolute_time = timeline_offset + local_time
            normalized = dict(frame)
            normalized["minute"] = absolute_time / 60.0
            normalized["source"] = str(frame.get("source", dataset.source))
            if frame.get("t") is not None:
                normalized["t"] = absolute_time
            records.append((absolute_time, 1, "frame", normalized))

        records.sort(key=lambda item: (item[0], item[1]))
        return [(timestamp, kind, value) for timestamp, _, kind, value in records]


def default_datasets(
    *,
    events_path: str | Path | None = None,
    frames_path: str | Path | None = None,
    clip: ReplayDataset | None = None,
) -> list[ReplayDataset]:
    """Load optional clip records first, followed by the generated match."""
    project_root = Path(__file__).resolve().parent.parent
    datasets: list[ReplayDataset] = []
    if clip is not None:
        datasets.append(clip)
    datasets.append(
        ReplayDataset.from_files(
            events_path or project_root / "outputs" / "synthetic_events.json",
            frames_path or project_root / "outputs" / "synthetic_frames.json",
            name="synthetic",
            source="synthetic",
        )
    )
    return datasets
