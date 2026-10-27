"""Sequence-aware feed processor with in-memory match state and Cosmos snapshots."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from engine.schema import validate_event, validate_frame
from engine.state import MatchState, ingest
from feed.clients import FeedConsumer
from feed.models import FeedMessage
from feed.persistence import (
    CosmosPersistence,
    InMemoryNarrationCache,
    NarrationCache,
    SnapshotStore,
)


logger = logging.getLogger(__name__)


@dataclass
class _MatchRuntime:
    state: MatchState = field(default_factory=MatchState)
    next_seq: int = 0
    last_seq: int = -1
    pending: dict[int, FeedMessage] = field(default_factory=dict)
    gap_since: float | None = None


class FeedProcessor:
    def __init__(
        self,
        consumer: FeedConsumer,
        *,
        snapshot_store: SnapshotStore | None = None,
        narration_cache: NarrationCache | None = None,
        snapshot_interval_seconds: float | None = None,
        reorder_wait_seconds: float = 0.5,
        max_reorder_buffer: int = 8,
    ) -> None:
        if snapshot_interval_seconds is None:
            snapshot_interval_seconds = float(
                os.getenv("MATCH_SNAPSHOT_INTERVAL_SECONDS", "30")
            )
        if snapshot_interval_seconds <= 0:
            raise ValueError("snapshot_interval_seconds must be greater than zero")
        if reorder_wait_seconds <= 0:
            raise ValueError("reorder_wait_seconds must be greater than zero")
        if max_reorder_buffer < 1:
            raise ValueError("max_reorder_buffer must be at least one")
        persistence = (
            snapshot_store
            if isinstance(snapshot_store, CosmosPersistence)
            else None
        )
        if snapshot_store is None:
            persistence = CosmosPersistence.from_environment()
            snapshot_store = persistence
        self.consumer = consumer
        self.persistence = persistence
        self.snapshot_store = snapshot_store
        self.narration_cache = (
            narration_cache
            if narration_cache is not None
            else persistence.narration_cache
            if persistence is not None
            else InMemoryNarrationCache()
        )
        self.snapshot_interval_seconds = snapshot_interval_seconds
        self.reorder_wait_seconds = reorder_wait_seconds
        self.max_reorder_buffer = max_reorder_buffer
        self.states: dict[str, MatchState] = {}
        self.duplicate_count = 0
        self._runtimes: dict[str, _MatchRuntime] = {}
        self._last_snapshot_at = time.monotonic()

    async def process_message(self, message: FeedMessage) -> None:
        runtime = await self._runtime_for(message.match_id)
        if message.seq < runtime.next_seq or message.seq in runtime.pending:
            self.duplicate_count += 1
            return

        runtime.pending[message.seq] = message
        now = time.monotonic()
        if runtime.next_seq not in runtime.pending and runtime.gap_since is None:
            runtime.gap_since = now
        await self._drain(runtime, now=now)

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        while stop_event is None or not stop_event.is_set():
            now = time.monotonic()
            timeout = self._next_timeout(now)
            try:
                message = await asyncio.wait_for(
                    self.consumer.receive(), timeout=timeout
                )
            except asyncio.TimeoutError:
                now = time.monotonic()
                for runtime in self._runtimes.values():
                    await self._drain(runtime, now=now)
                await self._save_periodic_snapshot_if_due(now)
                continue
            await self.process_message(message)
            await self._save_periodic_snapshot_if_due(time.monotonic())

        now = time.monotonic()
        for runtime in self._runtimes.values():
            await self._drain(runtime, now=now, force_gap=True)
        await self._save_all_snapshots()

    async def close(self) -> None:
        try:
            await self.consumer.close()
        finally:
            if self.persistence is not None:
                await self.persistence.close()

    async def _save_periodic_snapshot_if_due(self, now: float) -> None:
        if now - self._last_snapshot_at < self.snapshot_interval_seconds:
            return
        await self._save_all_snapshots()
        self._last_snapshot_at = now

    async def _runtime_for(self, match_id: str) -> _MatchRuntime:
        runtime = self._runtimes.get(match_id)
        if runtime is not None:
            return runtime

        runtime = _MatchRuntime()
        if self.snapshot_store is not None:
            snapshot = await self.snapshot_store.load_snapshot(match_id)
            if snapshot is not None:
                state_data = snapshot["state"]
                runtime.state = MatchState(**state_data)
                runtime.last_seq = snapshot["last_seq"]
                runtime.next_seq = runtime.last_seq + 1
        self._runtimes[match_id] = runtime
        self.states[match_id] = runtime.state
        return runtime

    async def _drain(
        self,
        runtime: _MatchRuntime,
        *,
        now: float,
        force_gap: bool = False,
    ) -> None:
        while runtime.pending:
            message = runtime.pending.get(runtime.next_seq)
            if message is None:
                gap_expired = (
                    runtime.gap_since is not None
                    and now - runtime.gap_since >= self.reorder_wait_seconds
                )
                if (
                    not force_gap
                    and not gap_expired
                    and len(runtime.pending) < self.max_reorder_buffer
                ):
                    break
                logger.warning(
                    "Skipping missing feed sequence range [%d, %d) for buffered "
                    "messages on match %s",
                    runtime.next_seq,
                    min(runtime.pending),
                    next(iter(runtime.pending.values())).match_id,
                )
                runtime.next_seq = min(runtime.pending)
                runtime.gap_since = None
                continue

            del runtime.pending[runtime.next_seq]
            self._ingest_message(runtime.state, message)
            runtime.last_seq = message.seq
            runtime.next_seq = message.seq + 1
            if self._is_key_moment(message):
                await self._save_snapshot_for(message.match_id, runtime)

        if not runtime.pending:
            runtime.gap_since = None
        elif runtime.next_seq not in runtime.pending and runtime.gap_since is None:
            runtime.gap_since = now

    @staticmethod
    def _ingest_message(state: MatchState, message: FeedMessage) -> None:
        record = dict(message.record)
        record["source"] = message.source
        if message.kind == "event":
            record_match_id = record.get("match_id")
            if record_match_id != message.match_id:
                raise ValueError(
                    "Event match_id must match the feed envelope match_id"
                )
            validate_event(record)
            ingest(state, event=record)
        else:
            validate_frame(record)
            ingest(state, frame=record)

    @staticmethod
    def _is_key_moment(message: FeedMessage) -> bool:
        return (
            message.kind == "event"
            and message.record.get("type") == "shot"
            and message.record.get("outcome") == "goal"
        )

    async def _save_snapshot_for(
        self, match_id: str, runtime: _MatchRuntime
    ) -> None:
        if self.snapshot_store is None:
            return
        await self.snapshot_store.save_snapshot(
            match_id,
            {"last_seq": runtime.last_seq, "state": runtime.state.snapshot()},
        )

    async def _save_all_snapshots(self) -> None:
        for match_id, runtime in self._runtimes.items():
            await self._save_snapshot_for(match_id, runtime)

    def _next_timeout(self, now: float) -> float:
        deadlines = [
            self.snapshot_interval_seconds - (now - self._last_snapshot_at)
        ]
        deadlines.extend(
            runtime.gap_since + self.reorder_wait_seconds - now
            for runtime in self._runtimes.values()
            if runtime.gap_since is not None
        )
        return max(0.01, min(deadlines))
