"""Local FastAPI service and live replay manager for Overseer."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from agents.fan_curator import FanCurator
from engine.replay import ReplayDataset, ReplayEngine, default_datasets
from engine.state import MatchState
from telemetry import initialize_telemetry


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


class ControlRequest(BaseModel):
    action: Literal["start", "stop"] | None = None
    speed: float | None = Field(default=None, gt=0, le=1000)
    mode: Literal["Broadcast", "Analyst", "Fan"] | None = None
    language: Literal["English", "Spanish", "Arabic", "French"] | None = None
    favourite_team: Literal["Rovers", "United"] | None = None
    favourite_player: str | None = Field(default=None, max_length=80)


class FanQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class ReplayManager:
    def __init__(self) -> None:
        self.state = MatchState()
        self.speed = 50.0
        self.mode = "Broadcast"
        self.language = "English"
        self.favourite_team = "Rovers"
        self.favourite_player: str | None = None
        self.status = "idle"
        self.error: str | None = None
        self._lock = threading.RLock()
        self._stop_event: threading.Event | None = None
        self._engine: ReplayEngine | None = None
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[str]] = set()
        self.fan_curator = FanCurator()
        self._last_video_frame_t = -1.0

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode,
                "language": self.language,
                "favourite_team": self.favourite_team,
                "favourite_player": self.favourite_player,
            }

    def compact_snapshot(self, tick: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            state = self.state
            return {
                "minute": round(state.minute, 2),
                "score": dict(state.score),
                "source": state.source,
                "status": self.status,
                "speed": self.speed,
                "mode": self.mode,
                "language": self.language,
                "favourite_team": self.favourite_team,
                "favourite_player": self.favourite_player,
                "event_count": len(state.events),
                "frame_count": len(state.frames),
                "events": [dict(event) for event in state.events[-80:]],
                "frame": dict(state.frames[-1]) if state.frames else None,
                "shape": dict(state.shape),
                "patterns": dict(state.patterns),
                "impact": list(state.impact[:10]),
                "momentum_series": list(state.momentum_series),
                "moments": list(state.moments[-30:]),
                "narration": dict(state.narration_log[-1]) if state.narration_log else None,
                "tick": tick,
                "error": self.error,
            }

    def publish_from_worker(self, tick: dict[str, Any]) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        snapshot = self.compact_snapshot(tick)
        self._loop.call_soon_threadsafe(self._fanout, snapshot)

    def _fanout(self, snapshot: dict[str, Any]) -> None:
        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def start(self) -> None:
        if self._task and not self._task.done():
            raise HTTPException(status_code=409, detail="Replay is already running")
        self.state = MatchState()
        self.error = None
        self.status = "starting"
        self._last_video_frame_t = -1.0
        self._loop = asyncio.get_running_loop()
        self._stop_event = threading.Event()
        self._engine = ReplayEngine(
            self.state,
            speed=self.speed,
            on_tick=self.publish_from_worker,
            stop_event=self._stop_event,
            config_provider=self.settings,
            on_record=self._on_replay_record,
        )
        self.status = "running"
        self._task = asyncio.create_task(self._run_worker())
        self._fanout(self.compact_snapshot())

    def _on_replay_record(self, kind: str, record: dict[str, Any]) -> None:
        if kind != "frame" or record.get("source") != "video":
            return
        timestamp = float(record.get("t", 0.0))
        if timestamp - self._last_video_frame_t < 0.5:
            return
        self._last_video_frame_t = timestamp
        self.publish_video_frame()

    def publish_video_frame(self) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._fanout, self.compact_snapshot())

    async def _run_worker(self) -> None:
        try:
            clip = None
            clip_events = os.getenv("CLIP_EVENTS_PATH")
            clip_frames = os.getenv("CLIP_FRAMES_PATH")
            clip_video = os.getenv("CLIP_VIDEO_PATH")
            if bool(clip_events) != bool(clip_frames):
                raise RuntimeError("CLIP_EVENTS_PATH and CLIP_FRAMES_PATH must be set together")
            if clip_events and clip_frames:
                clip = await asyncio.to_thread(
                    ReplayDataset.from_files,
                    clip_events,
                    clip_frames,
                    name="video-clip",
                    source="video",
                    video_path=clip_video,
                )
            datasets = await asyncio.to_thread(default_datasets, clip=clip)
            engine = self._engine
            if engine is None:
                raise RuntimeError("Replay engine was not initialized")
            await asyncio.to_thread(engine.run, datasets, **self.settings())
            with self._lock:
                self.status = "stopped" if self._stop_event and self._stop_event.is_set() else "complete"
        except Exception as error:
            with self._lock:
                self.error = f"{error.__class__.__name__}: {error}"
                self.status = "error"
        finally:
            self._fanout(self.compact_snapshot())

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._task and not self._task.done():
            await self._task
        elif self.status in ("idle", "running", "starting"):
            self.status = "stopped"
        self._fanout(self.compact_snapshot())

    async def update(self, data: ControlRequest) -> dict[str, Any]:
        if data.speed is not None:
            self.speed = data.speed
            if self._engine:
                self._engine.speed = data.speed
        if data.mode is not None:
            self.mode = data.mode
        if data.language is not None:
            self.language = data.language
        if data.favourite_team is not None:
            self.favourite_team = data.favourite_team
        if "favourite_player" in data.model_fields_set:
            self.favourite_player = (
                data.favourite_player.strip() if data.favourite_player else None
            )

        if data.action == "stop":
            await self.stop()
        elif data.action == "start":
            await self.start()
        else:
            self._fanout(self.compact_snapshot())
        return self.compact_snapshot()

    def full_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self.state.snapshot()
            snapshot.update(
                {
                    "status": self.status,
                    "speed": self.speed,
                    "mode": self.mode,
                    "language": self.language,
                    "favourite_team": self.favourite_team,
                    "favourite_player": self.favourite_player,
                    "error": self.error,
                    "frame": self.state.frames[-1] if self.state.frames else None,
                    "event_count": len(self.state.events),
                    "frame_count": len(self.state.frames),
                }
            )
            return snapshot


manager = ReplayManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    manager._loop = asyncio.get_running_loop()
    yield
    if manager._task and not manager._task.done():
        await manager.stop()


app = FastAPI(title="Overseer", version="0.1.0", lifespan=lifespan)
initialize_telemetry()
_cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/app.js", include_in_schema=False)
async def app_script() -> FileResponse:
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")


@app.get("/clip-video", include_in_schema=False)
async def clip_video() -> FileResponse:
    video_path = os.getenv("CLIP_VIDEO_PATH")
    if not video_path:
        raise HTTPException(status_code=404, detail="CLIP_VIDEO_PATH is not configured")
    path = Path(video_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Configured clip video does not exist: {path}")
    return FileResponse(path, media_type="video/mp4")


@app.get("/styles.css", include_in_schema=False)
async def styles() -> FileResponse:
    return FileResponse(WEB_DIR / "styles.css", media_type="text/css")


@app.get("/config.js", include_in_schema=False)
async def frontend_config() -> FileResponse:
    return FileResponse(WEB_DIR / "config.js", media_type="text/javascript")


@app.get("/state")
async def get_state() -> dict[str, Any]:
    return manager.full_snapshot()


@app.get("/frame")
async def get_frame(
    minute: float = Query(ge=0),
    t: float | None = Query(default=None, ge=0),
    source: Literal["video", "synthetic"] | None = None,
) -> dict[str, Any]:
    frames = manager.state.frames
    if not frames:
        raise HTTPException(status_code=404, detail="No replay frames are available yet")
    if source is not None:
        frames = [frame for frame in frames if frame.get("source") == source]
        if not frames:
            raise HTTPException(
                status_code=404,
                detail=f"No {source} replay frames are available",
            )
    if t is not None:
        target_time = t

        def frame_time(item: dict[str, Any]) -> float:
            return float(item.get("t", float(item.get("minute", 0.0)) * 60.0))
    else:
        target_time = minute * 60.0

        def frame_time(item: dict[str, Any]) -> float:
            return float(item.get("minute", 0.0)) * 60.0

    frame = min(
        frames,
        key=lambda item: abs(frame_time(item) - target_time),
    )
    return {"frame": frame}


@app.post("/control")
async def control(data: ControlRequest) -> dict[str, Any]:
    return await manager.update(data)


@app.post("/fan-query")
async def fan_query(data: FanQueryRequest) -> dict[str, Any]:
    query = data.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="query must not be blank")
    return manager.fan_curator.query(manager.state, query)


@app.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=3)
    manager._subscribers.add(queue)
    await queue.put(json.dumps(manager.compact_snapshot(), ensure_ascii=False))

    async def events():
        try:
            while not await request.is_disconnected():
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {message}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            manager._subscribers.discard(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
