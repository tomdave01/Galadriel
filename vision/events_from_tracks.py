"""Convert tracked video into shared-schema events/frames and an annotated MP4."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from vision.annotated_video import write_annotated_video
from vision.homography import load_config, perspective_matrix, transform_point
from vision.team_split import load_roster_map, split_teams
from vision.track import track_video, write_tracks


EVENT_TYPES = {"pass", "shot", "tackle", "pressure", "foul"}
EVENT_OUTCOMES = {
    "complete", "lost", "won", "blocked", "off_target",
    "on_target", "goal", "",
}
ROLES = {"GK", "DEF", "MID", "FWD"}


def _scale_homography_config(
    config: dict[str, Any], width: int, height: int
) -> dict[str, Any]:
    result = json.loads(json.dumps(config))
    expected_width = int(result.get("frame_width", width))
    expected_height = int(result.get("frame_height", height))
    if expected_width <= 0 or expected_height <= 0:
        raise ValueError("homography config frame_width/frame_height must be positive")
    points = result["reference_points"]["image"]
    result["reference_points"]["image"] = [
        [float(x) * width / expected_width, float(y) * height / expected_height]
        for x, y in points
    ]
    return result


def _identity(
    detection: dict[str, Any], roster: dict[str, Any]
) -> tuple[str | None, str | None, str | None]:
    tracker_id = detection.get("track_id")
    if tracker_id is None:
        return None, None, detection.get("team")
    entry = roster["players"].get(str(tracker_id), {})
    team = entry.get("team") or detection.get("team")
    if team not in {"Rovers", "United"}:
        return None, None, team
    name = entry.get("name") or f"{team} #{tracker_id}"
    return str(tracker_id), str(name), team


def _role(
    entry: dict[str, Any],
    team: str,
    x: float,
    directions: dict[str, str],
) -> str:
    if entry.get("role") in ROLES:
        return entry["role"]
    attacking_x = x if directions.get(team, "left_to_right") == "left_to_right" else 100.0 - x
    if attacking_x <= 9:
        return "GK"
    if attacking_x <= 36:
        return "DEF"
    if attacking_x <= 68:
        return "MID"
    return "FWD"


def _event(
    *,
    match_id: str,
    clip_id: str,
    t: float,
    event_type: str,
    team: str,
    player: str,
    outcome: str,
    start: tuple[float, float],
    end: tuple[float, float] | None = None,
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES or outcome not in EVENT_OUTCOMES:
        raise ValueError(f"Invalid event type/outcome: {event_type}/{outcome}")
    return {
        "match_id": match_id,
        "clip_id": clip_id,
        "source": "video",
        "minute": round(t / 60.0, 4),
        "t": round(t, 4),
        "type": event_type,
        "team": team,
        "player": player,
        "outcome": outcome,
        "x": round(start[0], 2),
        "y": round(start[1], 2),
        "end_x": round(end[0], 2) if end else None,
        "end_y": round(end[1], 2) if end else None,
    }


def _player_and_ball_positions(
    track_frame: dict[str, Any],
    matrix: Any,
    roster: dict[str, Any],
    directions: dict[str, str],
) -> tuple[list[dict[str, Any]], tuple[float, float] | None]:
    players: list[dict[str, Any]] = []
    ball_position = None
    for detection in track_frame.get("detections", []):
        center = detection.get("center")
        if not center or len(center) != 2:
            continue
        position = transform_point(center, matrix)
        if detection.get("label") == "ball":
            ball_position = position
            continue
        tracker_id, name, team = _identity(detection, roster)
        if tracker_id is None or name is None or team not in {"Rovers", "United"}:
            continue
        roster_entry = roster["players"].get(tracker_id, {})
        players.append(
            {
                "id": tracker_id,
                "name": name,
                "team": team,
                "role": _role(roster_entry, team, position[0], directions),
                "x": position[0],
                "y": position[1],
            }
        )
    return players, ball_position


def _interpolate_ball(
    positions: list[tuple[float, tuple[float, float]] | None],
    times: list[float],
    max_gap: float,
) -> list[tuple[float, float]]:
    observed = [
        (index, item[1])
        for index, item in enumerate(positions)
        if item is not None
    ]
    if not observed:
        return [(50.0, 50.0) for _ in positions]
    interpolated: list[tuple[float, float]] = []
    for index, item in enumerate(positions):
        if item is not None:
            interpolated.append(item[1])
            continue
        before = next(((i, point) for i, point in reversed(observed) if i < index), None)
        after = next(((i, point) for i, point in observed if i > index), None)
        if before and after and times[after[0]] - times[before[0]] <= max_gap:
            fraction = (times[index] - times[before[0]]) / (
                times[after[0]] - times[before[0]]
            )
            point = (
                before[1][0] + (after[1][0] - before[1][0]) * fraction,
                before[1][1] + (after[1][1] - before[1][1]) * fraction,
            )
        elif before:
            point = before[1]
        else:
            point = after[1] if after else (50.0, 50.0)
        interpolated.append(
            (round(min(100.0, max(0.0, point[0])), 3),
             round(min(100.0, max(0.0, point[1])), 3))
        )
    return interpolated


def _make_frames(
    tracks: dict[str, Any],
    matrix: Any,
    roster: dict[str, Any],
    config: dict[str, Any],
    sample_fps: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_frames = tracks["frames"]
    source_fps = float(tracks["fps"])
    step = max(1, round(source_fps / sample_fps))
    selected = list(range(0, len(source_frames), step))
    if selected[-1] != len(source_frames) - 1 and len(source_frames) > 1:
        selected.append(len(source_frames) - 1)

    directions = config.get("attacking_direction", {})
    transformed: list[dict[str, Any]] = []
    ball_observations: list[tuple[float, tuple[float, float]] | None] = []
    times: list[float] = []
    for source_index in selected:
        source_frame = source_frames[source_index]
        players, ball = _player_and_ball_positions(
            source_frame, matrix, roster, directions
        )
        timestamp = float(source_frame.get("t", source_index / source_fps))
        transformed.append({"t": timestamp, "players": players})
        times.append(timestamp)
        ball_observations.append((timestamp, ball) if ball is not None else None)

    ball_positions = _interpolate_ball(
        ball_observations,
        times,
        float(config.get("ball_interpolation_seconds", 2.0)),
    )
    frames: list[dict[str, Any]] = []
    last_possession: tuple[str, str] | None = None
    last_possession_time: float | None = None
    max_possession_gap = float(config.get("possession_distance", 12.0))
    for item, ball in zip(transformed, ball_positions):
        nearest = min(
            (
                (
                    player,
                    ((player["x"] - ball[0]) ** 2 + (player["y"] - ball[1]) ** 2) ** 0.5,
                )
                for player in item["players"]
            ),
            key=lambda candidate: candidate[1],
            default=(None, float("inf")),
        )
        if nearest[0] is not None and nearest[1] <= max_possession_gap:
            poss = nearest[0]["team"]
            last_possession = (nearest[0]["team"], nearest[0]["id"])
            last_possession_time = item["t"]
        elif (
            last_possession is not None
            and last_possession_time is not None
            and item["t"] - last_possession_time
            <= float(config.get("ball_interpolation_seconds", 2.0))
        ):
            poss = last_possession[0]
        else:
            poss = None

        frames.append(
            {
                "t": round(item["t"], 4),
                "minute": round(item["t"] / 60.0, 4),
                "poss": poss,
                "ball": {"x": ball[0], "y": ball[1]},
                "players": [
                    {
                        "id": player["id"],
                        "team": player["team"],
                        "role": player["role"],
                        "x": player["x"],
                        "y": player["y"],
                    }
                    for player in item["players"]
                ],
            }
        )
    return frames, transformed


def _make_events(
    frames: list[dict[str, Any]],
    transformed: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    clip_id: str,
    match_id: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous_holder: dict[str, Any] | None = None
    previous_ball: tuple[float, float] | None = None
    previous_time: float | None = None
    last_shot_time = -float("inf")
    direction = config.get("attacking_direction", {})
    shot_threshold = float(config.get("shot_speed_threshold", 24.0))
    possession_distance = float(config.get("possession_distance", 12.0))

    for frame, raw in zip(frames, transformed):
        ball = (float(frame["ball"]["x"]), float(frame["ball"]["y"]))
        current_holder = None
        if frame["poss"] is not None:
            nearest = min(
                (
                    (
                        player,
                        ((player["x"] - ball[0]) ** 2 + (player["y"] - ball[1]) ** 2) ** 0.5,
                    )
                    for player in raw["players"]
                    if player["team"] == frame["poss"]
                ),
                key=lambda candidate: candidate[1],
                default=(None, float("inf")),
            )
            if nearest[0] is not None and nearest[1] <= possession_distance:
                current_holder = nearest[0]

        timestamp = float(frame["t"])
        if previous_holder and current_holder and previous_holder["id"] != current_holder["id"]:
            start = (previous_holder["x"], previous_holder["y"])
            end = (current_holder["x"], current_holder["y"])
            if previous_holder["team"] == current_holder["team"]:
                events.append(
                    _event(
                        match_id=match_id,
                        clip_id=clip_id,
                        t=timestamp,
                        event_type="pass",
                        team=previous_holder["team"],
                        player=previous_holder["name"],
                        outcome="complete",
                        start=start,
                        end=end,
                    )
                )
            else:
                events.append(
                    _event(
                        match_id=match_id,
                        clip_id=clip_id,
                        t=timestamp,
                        event_type="pass",
                        team=previous_holder["team"],
                        player=previous_holder["name"],
                        outcome="lost",
                        start=start,
                        end=end,
                    )
                )
                events.append(
                    _event(
                        match_id=match_id,
                        clip_id=clip_id,
                        t=timestamp,
                        event_type="tackle",
                        team=current_holder["team"],
                        player=current_holder["name"],
                        outcome="won",
                        start=end,
                    )
                )

        if (
            previous_ball is not None
            and previous_time is not None
            and current_holder is not None
            and timestamp - previous_time > 0
        ):
            dx = ball[0] - previous_ball[0]
            dy = ball[1] - previous_ball[1]
            speed = (dx * dx + dy * dy) ** 0.5 / (timestamp - previous_time)
            team = current_holder["team"]
            toward_goal = (
                dx > 0
                if direction.get(team, "left_to_right") == "left_to_right"
                else dx < 0
            )
            if speed >= shot_threshold and toward_goal and timestamp - last_shot_time >= 1.5:
                goal_line = ball[0] >= 100 - float(config.get("goal_line_margin", 3.0)) if (
                    direction.get(team, "left_to_right") == "left_to_right"
                ) else ball[0] <= float(config.get("goal_line_margin", 3.0))
                on_target = abs(ball[1] - 50.0) <= 8.0
                events.append(
                    _event(
                        match_id=match_id,
                        clip_id=clip_id,
                        t=timestamp,
                        event_type="shot",
                        team=team,
                        player=current_holder["name"],
                        outcome="on_target" if on_target or goal_line else "off_target",
                        start=previous_ball,
                        end=ball,
                    )
                )
                last_shot_time = timestamp

        if current_holder:
            previous_holder = current_holder
        previous_ball = ball
        previous_time = timestamp

    return sorted(events, key=lambda event: (event["t"], event["type"]))


def _player_metrics(frames: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    observations: dict[str, list[tuple[float, float, float, str]]] = {}
    for frame in frames:
        timestamp = float(frame["t"])
        for player in frame["players"]:
            observations.setdefault(player["id"], []).append(
                (timestamp, float(player["x"]), float(player["y"]), player["team"])
            )

    metrics: dict[str, dict[str, Any]] = {}
    for player_id, samples in observations.items():
        distance = 0.0
        speeds: list[float] = []
        for previous, current in zip(samples, samples[1:]):
            elapsed = current[0] - previous[0]
            if elapsed <= 0:
                continue
            segment = ((current[1] - previous[1]) ** 2 + (current[2] - previous[2]) ** 2) ** 0.5
            distance += segment
            speeds.append(segment / elapsed)
        metrics[player_id] = {
            "team": samples[0][3],
            "distance_units": round(distance, 2),
            "mean_speed_units_per_second": round(sum(speeds) / len(speeds), 2) if speeds else 0.0,
            "max_speed_units_per_second": round(max(speeds, default=0.0), 2),
            "samples": len(samples),
        }
    return metrics


def _validate_corrected_events(
    path: str | Path, *, clip_id: str, match_id: str
) -> list[dict[str, Any]]:
    try:
        corrected = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unable to load hand-corrected events {path}: {error}") from error
    if not isinstance(corrected, list):
        raise ValueError("Hand-corrected event file must be a JSON array")

    required = {
        "match_id", "clip_id", "source", "minute", "t", "type", "team",
        "player", "outcome", "x", "y", "end_x", "end_y",
    }
    validated: list[dict[str, Any]] = []
    for index, event in enumerate(corrected):
        if not isinstance(event, dict) or set(event) != required:
            raise ValueError(
                f"Corrected event {index} must contain exactly the shared Event schema keys"
            )
        if event["type"] not in EVENT_TYPES or event["outcome"] not in EVENT_OUTCOMES:
            raise ValueError(f"Corrected event {index} has an invalid type or outcome")
        if event["team"] not in {"Rovers", "United"} or not isinstance(event["player"], str):
            raise ValueError(f"Corrected event {index} must have a valid team and player")
        try:
            timestamp = float(event["t"])
            x = float(event["x"])
            y = float(event["y"])
            event_minute = float(event["minute"])
            end_x = None if event["end_x"] is None else float(event["end_x"])
            end_y = None if event["end_y"] is None else float(event["end_y"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"Corrected event {index} has invalid numeric coordinates/time") from error
        if timestamp < 0 or event_minute < 0 or not 0 <= x <= 100 or not 0 <= y <= 100:
            raise ValueError(f"Corrected event {index} has out-of-range time or coordinates")
        if (end_x is None) != (end_y is None):
            raise ValueError(f"Corrected event {index} must set both end_x and end_y or neither")
        if end_x is not None and (not 0 <= end_x <= 100 or not 0 <= end_y <= 100):
            raise ValueError(f"Corrected event {index} has out-of-range end coordinates")
        validated.append(
            {
                **event,
                "match_id": match_id,
                "clip_id": clip_id,
                "source": "video",
                "minute": round(event_minute, 4),
                "t": round(timestamp, 4),
                "x": round(x, 2),
                "y": round(y, 2),
                "end_x": round(end_x, 2) if end_x is not None else None,
                "end_y": round(end_y, 2) if end_y is not None else None,
            }
        )
    return sorted(validated, key=lambda event: event["t"])


def process_clip(
    video_path: str | Path,
    *,
    config_path: str | Path,
    roster_map_path: str | Path,
    output_dir: str | Path,
    tracks_path: str | Path | None = None,
    hand_correct: str | Path | None = None,
    model_path: str = "yolo11n.pt",
    tracker_config: str = "bytetrack.yaml",
    device: str | None = None,
    sample_fps: float = 5.0,
) -> dict[str, Path]:
    """Run tracking through clip export, optionally using hand-corrected events."""
    if sample_fps <= 0:
        raise ValueError("sample_fps must be greater than zero")
    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"Input video does not exist: {source}")
    config = load_config(config_path)
    clip_id = str(config.get("clip_id", source.stem))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", clip_id):
        raise ValueError("clip_id must contain only letters, numbers, underscores, or hyphens")
    match_id = str(config.get("match_id", clip_id))
    roster = load_roster_map(roster_map_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    raw_tracks_path = Path(tracks_path) if tracks_path else destination / f"{clip_id}_tracks.json"
    if tracks_path:
        try:
            tracks = json.loads(raw_tracks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Unable to load tracks file {raw_tracks_path}: {error}") from error
    else:
        tracks = track_video(
            source,
            model_path=model_path,
            tracker_config=tracker_config,
            device=device,
        )
        write_tracks(tracks, raw_tracks_path)

    tracks = split_teams(tracks, team_clusters=roster["team_clusters"])
    import cv2

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV could not inspect video dimensions: {source}")
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    capture.release()
    scaled_config = _scale_homography_config(config, width, height)
    matrix = perspective_matrix(scaled_config)
    frames, transformed = _make_frames(
        tracks, matrix, roster, config, sample_fps
    )
    events = _make_events(
        frames,
        transformed,
        config=config,
        clip_id=clip_id,
        match_id=match_id,
    )
    event_path = destination / f"{clip_id}_events.json"
    frame_path = destination / f"{clip_id}_frames.json"
    if hand_correct is not None:
        correction_path = event_path if str(hand_correct) == "__OUTPUT__" else Path(hand_correct)
        events = _validate_corrected_events(
            correction_path, clip_id=clip_id, match_id=match_id
        )
    event_path.write_text(
        json.dumps(events, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    frame_path.write_text(
        json.dumps(frames, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )

    metrics_path = destination / f"{clip_id}_player_metrics.json"
    metrics_path.write_text(
        json.dumps(_player_metrics(frames), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    annotated_path = destination / f"{clip_id}_annotated.mp4"
    write_annotated_video(
        source,
        tracks,
        events,
        annotated_path,
        roster=roster,
    )
    return {
        "tracks": raw_tracks_path,
        "events": event_path,
        "frames": frame_path,
        "metrics": metrics_path,
        "annotated": annotated_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="30-second wide-angle, no-cut MP4")
    parser.add_argument(
        "--config",
        required=True,
        help="per-clip JSON homography calibration config",
    )
    parser.add_argument(
        "--roster-map",
        default=str(Path(__file__).with_name("roster_map.json")),
        help="operator-confirmed tracker-ID mapping",
    )
    parser.add_argument("--tracks", help="reuse an existing track_video JSON instead of running YOLO")
    parser.add_argument("--output-dir", default=str(Path(__file__).resolve().parent.parent / "outputs"))
    parser.add_argument(
        "--hand-correct",
        nargs="?",
        const="__OUTPUT__",
        help="load an edited events JSON (no value reuses outputs/<clip>_events.json)",
    )
    parser.add_argument("--model", default="yolo11n.pt")
    parser.add_argument("--tracker", default="bytetrack.yaml")
    parser.add_argument("--device", help="Ultralytics device, e.g. cpu or 0")
    parser.add_argument("--sample-fps", type=float, default=5.0)
    arguments = parser.parse_args()
    paths = process_clip(
        arguments.video,
        config_path=arguments.config,
        roster_map_path=arguments.roster_map,
        output_dir=arguments.output_dir,
        tracks_path=arguments.tracks,
        hand_correct=arguments.hand_correct,
        model_path=arguments.model,
        tracker_config=arguments.tracker,
        device=arguments.device,
        sample_fps=arguments.sample_fps,
    )
    for kind, path in paths.items():
        print(f"Wrote {kind}: {path}")


if __name__ == "__main__":
    main()
