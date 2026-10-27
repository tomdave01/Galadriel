from __future__ import annotations

import json

import cv2
import numpy as np

from engine.schema import validate_event, validate_frame
from vision.events_from_tracks import (
    _make_events,
    _make_frames,
    _interpolate_ball,
    _player_and_ball_positions,
    process_clip,
)
from vision.homography import load_config, perspective_matrix, transform_point
from vision.team_split import split_teams


def _tracks_fixture():
    frames = []
    ball_positions = [20, 35, 80, 90, 85, 80]
    for index, ball_x in enumerate(ball_positions):
        detections = [
            {
                "track_id": 0,
                "class_id": 0,
                "label": "person",
                "bbox": [18, 45, 22, 55],
                "center": [20, 60],
                "torso_color": [10, 20, 220],
            },
            {
                "track_id": 1,
                "class_id": 0,
                "label": "person",
                "bbox": [35, 45, 39, 55],
                "center": [35, 60],
                "torso_color": [10, 30, 210],
            },
            {
                "track_id": 2,
                "class_id": 0,
                "label": "person",
                "bbox": [80, 45, 84, 55],
                "center": [80, 60],
                "torso_color": [220, 30, 10],
            },
            {
                "track_id": 3,
                "class_id": 0,
                "label": "person",
                "bbox": [90, 45, 94, 55],
                "center": [90, 60],
                "torso_color": [210, 40, 20],
            },
            {
                "track_id": 99,
                "class_id": 32,
                "label": "ball",
                "bbox": [ball_x - 1, 49, ball_x + 1, 51],
                "center": [ball_x, 50],
            },
        ]
        frames.append({"frame_index": index, "t": index / 5, "detections": detections})
    return {"fps": 5.0, "width": 160, "height": 90, "duration": 1.2, "frames": frames}


def _write_video(path, frame_count=6):
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        5.0,
        (160, 90),
    )
    assert writer.isOpened()
    for _ in range(frame_count):
        writer.write(np.zeros((90, 160, 3), dtype=np.uint8))
    writer.release()


def test_homography_maps_calibration_corners(tmp_path):
    config_path = tmp_path / "clip.json"
    config_path.write_text(
        json.dumps(
            {
                "clip_id": "clip1",
                "frame_width": 100,
                "frame_height": 100,
                "reference_points": {
                    "image": [[0, 0], [99, 0], [99, 99], [0, 99]],
                    "pitch": [[0, 0], [100, 0], [100, 100], [0, 100]],
                },
            }
        ),
        encoding="utf-8",
    )
    matrix = perspective_matrix(load_config(config_path))
    assert transform_point([0, 0], matrix) == (0.0, 0.0)
    assert transform_point([99, 99], matrix) == (100.0, 100.0)


def test_team_split_assigns_two_clusters_and_outlier_candidate():
    tracks = _tracks_fixture()
    result = split_teams(
        tracks,
        team_clusters={"0": "Rovers", "1": "United"},
    )
    assert len(result["team_split"]["players"]) == 4
    assignments = {
        detection["track_id"]: detection["team"]
        for detection in result["frames"][0]["detections"]
        if detection["label"] == "person"
    }
    assert assignments[0] in {"Rovers", "United"}
    assert assignments[2] != assignments[0]


def test_team_split_handles_two_distinct_colors_and_rejects_one():
    tracks = _tracks_fixture()
    for frame in tracks["frames"]:
        for detection in frame["detections"]:
            if detection["label"] == "person":
                detection["torso_color"] = (
                    [20, 30, 220] if detection["track_id"] < 2 else [220, 30, 20]
                )
    assert len(split_teams(tracks)["team_split"]["players"]) == 4

    identical_tracks = _tracks_fixture()
    for frame in identical_tracks["frames"]:
        for detection in frame["detections"]:
            if detection["label"] == "person":
                detection["torso_color"] = [20, 30, 220]
    try:
        split_teams(identical_tracks)
    except ValueError as error:
        assert "two distinct shirt colors" in str(error)
    else:
        raise AssertionError("One shirt-color cluster cannot split two teams")


def test_frame_builder_rejects_empty_tracking_data():
    try:
        _make_frames(
            {"fps": 5.0, "frames": []},
            np.eye(3),
            {"players": {}},
            {},
            sample_fps=5,
        )
    except ValueError as error:
        assert "at least one frame" in str(error)
    else:
        raise AssertionError("Empty tracking data must fail clearly")


def test_frame_builder_rejects_zero_sample_rate_and_missing_ball():
    tracks = _tracks_fixture()
    try:
        _make_frames(tracks, np.eye(3), {"players": {}}, {}, sample_fps=0)
    except ValueError as error:
        assert "sample_fps" in str(error)
    else:
        raise AssertionError("A zero sample rate must fail clearly")

    try:
        _interpolate_ball([None, None], [0.0, 0.2], 2.0)
    except ValueError as error:
        assert "no ball detections" in str(error)
    else:
        raise AssertionError("Missing ball detections must not invent midfield data")


def test_ball_interpolation_rejects_observations_outside_configured_gap():
    try:
        _interpolate_ball(
            [((0.0, 0.0), (10.0, 10.0)), None],
            [0.0, 3.0],
            2.0,
        )
    except ValueError as error:
        assert "within 2 seconds" in str(error)
    else:
        raise AssertionError("Ball interpolation must respect the maximum gap")


def test_homography_uses_player_footpoint_but_ball_center():
    tracks = split_teams(_tracks_fixture())
    players, ball = _player_and_ball_positions(
        tracks["frames"][0],
        np.eye(3),
        {"players": {}},
        {"Rovers": "left_to_right", "United": "right_to_left"},
    )
    assert players[0]["x"] == 20.0
    assert players[0]["y"] == 55.0
    assert ball == (20.0, 50.0)


def test_fast_ball_crossing_goal_line_is_a_goal():
    frames = [
        {
            "t": time,
            "minute": time / 60,
            "poss": "Rovers",
            "ball": {"x": x, "y": 50.0},
        }
        for time, x in ((0.0, 80.0), (0.5, 98.0))
    ]
    events = _make_events(
        frames,
        [
            {
                "t": frame["t"],
                "players": [
                    {
                        "id": "p1",
                        "name": "Rico Vale",
                        "team": "Rovers",
                        "x": frame["ball"]["x"],
                        "y": 50.0,
                    }
                ],
            }
            for frame in frames
        ],
        config={
            "attacking_direction": {
                "Rovers": "left_to_right",
                "United": "right_to_left",
            },
            "shot_speed_threshold": 24.0,
            "possession_distance": 12.0,
            "goal_line_margin": 3.0,
        },
        clip_id="clip1",
        match_id="match1",
    )
    assert len(events) == 1
    assert events[0]["type"] == "shot"
    assert events[0]["outcome"] == "goal"


def test_pipeline_writes_shared_schema_metrics_and_annotated_video(tmp_path):
    video_path = tmp_path / "clip1.mp4"
    _write_video(video_path)
    config_path = tmp_path / "clip1.json"
    config_path.write_text(
        json.dumps(
            {
                "clip_id": "clip1",
                "match_id": "overseer-clip1",
                "frame_width": 160,
                "frame_height": 90,
                "reference_points": {
                    "image": [[0, 0], [159, 0], [159, 89], [0, 89]],
                    "pitch": [[0, 0], [100, 0], [100, 100], [0, 100]],
                },
                "attacking_direction": {
                    "Rovers": "left_to_right",
                    "United": "right_to_left",
                },
                "possession_distance": 12,
                "ball_interpolation_seconds": 2,
                "shot_speed_threshold": 24,
            }
        ),
        encoding="utf-8",
    )
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(
        json.dumps(
            {
                "team_clusters": {"0": "Rovers", "1": "United"},
                "players": {
                    "0": {"name": "Rico Vale", "team": "Rovers", "role": "MID"},
                    "1": {"name": "Leo Vale", "team": "Rovers", "role": "FWD"},
                    "2": {"name": "Ira West", "team": "United", "role": "FWD"},
                    "3": {"name": "Nico Lane", "team": "United", "role": "MID"},
                },
            }
        ),
        encoding="utf-8",
    )
    tracks_path = tmp_path / "tracks.json"
    tracks_data = _tracks_fixture()
    tracks_data["frames"][0]["detections"] = [
        detection for detection in tracks_data["frames"][0]["detections"]
        if detection["label"] != "ball"
    ]
    tracks_path.write_text(json.dumps(tracks_data), encoding="utf-8")

    outputs = process_clip(
        video_path,
        config_path=config_path,
        roster_map_path=roster_path,
        output_dir=tmp_path / "outputs",
        tracks_path=tracks_path,
    )
    events = json.loads(outputs["events"].read_text(encoding="utf-8"))
    frames = json.loads(outputs["frames"].read_text(encoding="utf-8"))
    metrics = json.loads(outputs["metrics"].read_text(encoding="utf-8"))
    assert events
    assert all(
        set(event) == {
            "match_id", "clip_id", "source", "minute", "t", "type", "team",
            "player", "outcome", "x", "y", "end_x", "end_y",
        }
        for event in events
    )
    assert all(set(frame) == {"t", "minute", "poss", "ball", "players"} for frame in frames)
    for event in events:
        validate_event(event)
    for frame in frames:
        validate_frame(frame)
    assert all(player["team"] in {"Rovers", "United"} for frame in frames for player in frame["players"])
    assert any(event["type"] == "pass" for event in events)
    assert "0" in metrics and metrics["0"]["distance_units"] >= 0
    assert outputs["annotated"].is_file() and outputs["annotated"].stat().st_size > 0


def test_hand_correct_requires_shared_schema(tmp_path):
    from vision.events_from_tracks import _validate_corrected_events

    path = tmp_path / "wrong.json"
    path.write_text('[{"type":"shot"}]', encoding="utf-8")
    try:
        _validate_corrected_events(path, clip_id="clip1", match_id="test")
    except ValueError as error:
        assert "shared Event schema" in str(error)
    else:
        raise AssertionError("Invalid hand-corrected event input should fail")


def test_hand_corrected_events_are_normalized_and_revalidated(tmp_path):
    from vision.events_from_tracks import _validate_corrected_events

    path = tmp_path / "corrected.json"
    path.write_text(
        json.dumps(
            [
                {
                    "match_id": "old-match",
                    "clip_id": "old-clip",
                    "source": "synthetic",
                    "minute": 5.0,
                    "t": 120.0,
                    "type": "shot",
                    "team": "Rovers",
                    "player": "Rico Vale",
                    "outcome": "goal",
                    "x": 99,
                    "y": 50,
                    "end_x": None,
                    "end_y": None,
                }
            ]
        ),
        encoding="utf-8",
    )
    [event] = _validate_corrected_events(
        path, clip_id="clip1", match_id="match1"
    )
    assert event["match_id"] == "match1"
    assert event["clip_id"] == "clip1"
    assert event["source"] == "video"
    assert event["minute"] == 2.0
    validate_event(event)
