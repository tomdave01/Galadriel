from __future__ import annotations

import json

import cv2
import numpy as np

from vision.events_from_tracks import process_clip
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
                "bbox": [18, 45, 22, 80],
                "center": [20, 60],
                "torso_color": [10, 20, 220],
            },
            {
                "track_id": 1,
                "class_id": 0,
                "label": "person",
                "bbox": [35, 45, 39, 80],
                "center": [35, 60],
                "torso_color": [10, 30, 210],
            },
            {
                "track_id": 2,
                "class_id": 0,
                "label": "person",
                "bbox": [80, 45, 84, 80],
                "center": [80, 60],
                "torso_color": [220, 30, 10],
            },
            {
                "track_id": 3,
                "class_id": 0,
                "label": "person",
                "bbox": [90, 45, 94, 80],
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
    tracks_path.write_text(json.dumps(_tracks_fixture()), encoding="utf-8")

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
