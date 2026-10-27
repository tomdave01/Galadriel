from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app, manager
from engine.state import MatchState


def test_static_dashboard_and_state_route():
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Match pitch" in page.text
        assert client.get("/app.js").status_code == 200
        assert client.get("/styles.css").status_code == 200
        assert "OVERSEER_API_BASE_URL" in client.get("/config.js").text
        snapshot = client.get("/state")
        assert snapshot.status_code == 200
        assert snapshot.json()["status"] == "idle"
        assert "frames" in snapshot.json()
        missing_frame = client.get("/frame?minute=1")
        assert missing_frame.status_code == 404


def test_clip_video_supports_byte_range_requests(tmp_path, monkeypatch):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(bytes(range(64)))
    monkeypatch.setenv("CLIP_VIDEO_PATH", str(clip_path))
    with TestClient(app) as client:
        response = client.get("/clip-video", headers={"Range": "bytes=0-9"})
    assert response.status_code == 206
    assert len(response.content) == 10


def test_control_preferences_and_fan_query():
    original_state = manager.state
    original_settings = (
        manager.speed,
        manager.mode,
        manager.language,
        manager.favourite_team,
        manager.favourite_player,
    )
    manager.state = MatchState(
        events=[
            {
                "minute": 10.0,
                "team": "Rovers",
                "player": "Rico Vale",
                "type": "shot",
                "outcome": "on_target",
                "t": None,
                "clip_id": None,
            },
            {
                "minute": 11.0,
                "team": "United",
                "player": "Ira West",
                "type": "shot",
                "outcome": "off_target",
                "t": None,
                "clip_id": None,
            },
        ],
        frames=[
            {
                "t": 600.0,
                "minute": 10.0,
                "poss": "Rovers",
                "ball": {"x": 80.0, "y": 75.0},
                "players": [],
            }
        ],
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/control",
                json={
                    "speed": 25,
                    "mode": "Analyst",
                    "language": "French",
                    "favourite_team": "United",
                },
            )
            assert response.status_code == 200
            assert response.json()["speed"] == 25
            assert response.json()["mode"] == "Analyst"

            selected = client.post(
                "/control",
                json={"favourite_player": "Rico Vale"},
            )
            assert selected.json()["favourite_player"] == "Rico Vale"
            cleared = client.post(
                "/control",
                json={"favourite_player": None},
            )
            assert cleared.json()["favourite_player"] is None

            query = client.post(
                "/fan-query",
                json={"query": "Rico Vale shots on target"},
            )
            assert query.status_code == 200
            assert query.json()["count"] == 1
            assert query.json()["clips"][0]["player"] == "Rico Vale"
            frame = client.get("/frame?minute=9.9")
            assert frame.status_code == 200
            assert frame.json()["frame"]["minute"] == 10.0

            blank_query = client.post("/fan-query", json={"query": " "})
            assert blank_query.status_code == 422
    finally:
        manager.state = original_state
        (
            manager.speed,
            manager.mode,
            manager.language,
            manager.favourite_team,
            manager.favourite_player,
        ) = original_settings
