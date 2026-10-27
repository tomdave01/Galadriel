"""Write a review video with tracker boxes, roster names, and event labels."""

from __future__ import annotations

from pathlib import Path
from typing import Any


TEAM_BGR = {"Rovers": (105, 235, 182), "United": (108, 166, 240)}


def write_annotated_video(
    video_path: str | Path,
    tracks: dict[str, Any],
    events: list[dict[str, Any]],
    output_path: str | Path,
    *,
    roster: dict[str, Any] | None = None,
) -> Path:
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError(
            "Annotated video output requires OpenCV; install vision/requirements.txt"
        ) from error

    source = Path(video_path)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV could not open video for annotation: {source}")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0 or width <= 0 or height <= 0:
        capture.release()
        raise RuntimeError(f"Input video has invalid metadata: {source}")

    writer = cv2.VideoWriter(
        str(destination),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        capture.release()
        raise RuntimeError(f"OpenCV could not create annotated output: {destination}")

    roster_players = (roster or {}).get("players", {})
    frame_records = tracks.get("frames", [])
    event_by_frame: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        index = max(0, round(float(event.get("t", 0.0)) * fps))
        event_by_frame.setdefault(index, []).append(event)

    frame_index = 0
    try:
        while True:
            success, image = capture.read()
            if not success:
                break
            if frame_index < len(frame_records):
                for detection in frame_records[frame_index].get("detections", []):
                    box = detection.get("bbox")
                    if not box or len(box) != 4:
                        continue
                    team = detection.get("team")
                    color = TEAM_BGR.get(team, (205, 205, 205))
                    x1, y1, x2, y2 = (int(value) for value in box)
                    cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
                    tracker_id = detection.get("track_id")
                    roster_entry = roster_players.get(str(tracker_id), {})
                    if detection.get("label") == "ball":
                        label = f"BALL #{tracker_id}" if tracker_id is not None else "BALL"
                    else:
                        name = roster_entry.get("name")
                        label = name or (
                            f"{team} #{tracker_id}" if team and tracker_id is not None
                            else f"REFEREE? #{tracker_id}"
                        )
                    cv2.putText(
                        image,
                        label,
                        (x1, max(18, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.48,
                        color,
                        1,
                        cv2.LINE_AA,
                    )

            for event in event_by_frame.get(frame_index, []):
                message = (
                    f"{float(event['minute']):.2f}'  {event['team']}  "
                    f"{event['player']}  {event['type'].upper()} {event['outcome']}"
                )
                cv2.rectangle(image, (8, 8), (min(width - 8, 20 + len(message) * 10), 38), (12, 18, 14), -1)
                cv2.putText(
                    image,
                    message,
                    (15, 29),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (245, 250, 245),
                    1,
                    cv2.LINE_AA,
                )
            writer.write(image)
            frame_index += 1
    finally:
        capture.release()
        writer.release()

    if frame_index == 0:
        raise RuntimeError(f"No frames were written from {source}")
    return destination
