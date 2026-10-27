"""Run Ultralytics YOLO person/ball detection with persistent ByteTrack IDs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PERSON_CLASS_ID = 0
SPORTS_BALL_CLASS_ID = 32


def _torso_color(frame: Any, box: tuple[int, int, int, int]) -> list[int] | None:
    import numpy as np

    height, width = frame.shape[:2]
    x1, y1, x2, y2 = box
    box_width = max(1, x2 - x1)
    box_height = max(1, y2 - y1)
    left = max(0, min(width, x1 + int(box_width * 0.2)))
    right = max(left + 1, min(width, x2 - int(box_width * 0.2)))
    top = max(0, min(height, y1 + int(box_height * 0.18)))
    bottom = max(top + 1, min(height, y1 + int(box_height * 0.58)))
    crop = frame[top:bottom, left:right]
    if crop.size == 0:
        return None
    return [int(value) for value in np.median(crop.reshape(-1, 3), axis=0)]


def track_video(
    video_path: str | Path,
    *,
    model_path: str = "yolo11n.pt",
    tracker_config: str = "bytetrack.yaml",
    confidence: float = 0.2,
    device: str | None = None,
) -> dict[str, Any]:
    """Track every source frame and return an intermediate tracker JSON structure."""
    try:
        import cv2
        from ultralytics import YOLO
    except ImportError as error:
        raise RuntimeError(
            "Video tracking requires Ultralytics and OpenCV. "
            "Install them with: python -m pip install -r vision/requirements.txt"
        ) from error

    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"Input video does not exist: {source}")
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV could not open video: {source}")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0 or width <= 0 or height <= 0:
        capture.release()
        raise RuntimeError(f"Video has invalid FPS or dimensions: {source}")

    try:
        model = YOLO(model_path)
    except Exception as error:
        capture.release()
        raise RuntimeError(f"Unable to load YOLO model {model_path}: {error}") from error

    frames: list[dict[str, Any]] = []
    frame_index = 0
    try:
        while True:
            success, image = capture.read()
            if not success:
                break
            kwargs: dict[str, Any] = {
                "source": image,
                "persist": True,
                "tracker": tracker_config,
                "classes": [PERSON_CLASS_ID, SPORTS_BALL_CLASS_ID],
                "conf": confidence,
                "verbose": False,
            }
            if device:
                kwargs["device"] = device
            try:
                results = model.track(**kwargs)
            except Exception as error:
                raise RuntimeError(f"YOLO tracking failed at frame {frame_index}: {error}") from error

            detections: list[dict[str, Any]] = []
            if results:
                boxes = results[0].boxes
                if boxes is not None and len(boxes):
                    coordinates = boxes.xyxy.cpu().numpy()
                    class_ids = boxes.cls.cpu().numpy().astype(int)
                    track_ids = (
                        boxes.id.cpu().numpy().astype(int)
                        if boxes.id is not None
                        else [None] * len(coordinates)
                    )
                    for coordinates_row, class_id, track_id in zip(
                        coordinates, class_ids, track_ids
                    ):
                        x1, y1, x2, y2 = (int(round(value)) for value in coordinates_row)
                        detection: dict[str, Any] = {
                            "track_id": int(track_id) if track_id is not None else None,
                            "class_id": int(class_id),
                            "label": "person" if class_id == PERSON_CLASS_ID else "ball",
                            "bbox": [x1, y1, x2, y2],
                            "center": [round((x1 + x2) / 2, 2), round((y1 + y2) / 2, 2)],
                        }
                        if class_id == PERSON_CLASS_ID:
                            detection["torso_color"] = _torso_color(
                                image, (x1, y1, x2, y2)
                            )
                        detections.append(detection)

            frames.append(
                {
                    "frame_index": frame_index,
                    "t": round(frame_index / fps, 4),
                    "detections": detections,
                }
            )
            frame_index += 1
    finally:
        capture.release()

    if not frames:
        raise RuntimeError(f"No frames were decoded from {source}")
    return {
        "video_path": str(source.resolve()),
        "fps": fps,
        "width": width,
        "height": height,
        "duration": round(len(frames) / fps, 4),
        "frames": frames,
    }


def write_tracks(tracks: dict[str, Any], output_path: str | Path) -> Path:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(tracks, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="wide-angle, no-cut MP4 input")
    parser.add_argument("--model", default="yolo11n.pt", help="Ultralytics model (default: yolo11n.pt)")
    parser.add_argument("--tracker", default="bytetrack.yaml", help="Ultralytics tracker config")
    parser.add_argument("--confidence", type=float, default=0.2)
    parser.add_argument("--device", help="Ultralytics device, e.g. cpu, 0")
    parser.add_argument("--output", help="tracking JSON path; defaults to outputs/<clip>_tracks.json")
    arguments = parser.parse_args()
    video_path = Path(arguments.video)
    output = Path(arguments.output) if arguments.output else (
        Path(__file__).resolve().parent.parent / "outputs" / f"{video_path.stem}_tracks.json"
    )
    tracks = track_video(
        video_path,
        model_path=arguments.model,
        tracker_config=arguments.tracker,
        confidence=arguments.confidence,
        device=arguments.device,
    )
    saved = write_tracks(tracks, output)
    print(
        f"Tracked {len(tracks['frames'])} frames from {video_path.name} "
        f"({tracks['width']}x{tracks['height']} at {tracks['fps']:.2f} FPS)."
    )
    print(f"Wrote {saved}")


if __name__ == "__main__":
    main()
