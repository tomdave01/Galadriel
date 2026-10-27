"""Four-point perspective calibration for mapping video pixels to pitch space."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unable to load homography config {config_path}: {error}") from error
    points = config.get("reference_points", {})
    image_points = points.get("image")
    pitch_points = points.get("pitch")
    if not isinstance(image_points, list) or len(image_points) != 4:
        raise ValueError(f"{config_path} must define four reference_points.image coordinates")
    if not isinstance(pitch_points, list) or len(pitch_points) != 4:
        raise ValueError(f"{config_path} must define four reference_points.pitch coordinates")
    for label, coordinates in (("image", image_points), ("pitch", pitch_points)):
        if any(
            not isinstance(point, list)
            or len(point) != 2
            or not all(isinstance(value, (int, float)) for value in point)
            for point in coordinates
        ):
            raise ValueError(f"reference_points.{label} must contain four numeric [x, y] points")
    return config


def perspective_matrix(config: dict[str, Any]):
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise RuntimeError(
            "OpenCV and NumPy are required for video homography; "
            "install vision/requirements.txt"
        ) from error

    reference = config["reference_points"]
    image_points = np.asarray(reference["image"], dtype=np.float32)
    pitch_points = np.asarray(reference["pitch"], dtype=np.float32)
    if abs(float(cv2.contourArea(image_points))) < 1:
        raise ValueError("The four image reference points must enclose a non-zero area")
    matrix = cv2.getPerspectiveTransform(image_points, pitch_points)
    if not np.isfinite(matrix).all():
        raise ValueError("Could not calculate a finite homography matrix")
    return matrix


def transform_point(point: tuple[float, float] | list[float], matrix: Any) -> tuple[float, float]:
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise RuntimeError("OpenCV and NumPy are required; install vision/requirements.txt") from error
    source = np.asarray([[[float(point[0]), float(point[1])]]], dtype=np.float32)
    transformed = cv2.perspectiveTransform(source, matrix)[0][0]
    x = min(100.0, max(0.0, float(transformed[0])))
    y = min(100.0, max(0.0, float(transformed[1])))
    return round(x, 3), round(y, 3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="per-clip JSON calibration config")
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    matrix = perspective_matrix(config)
    print(f"Valid homography for {config.get('clip_id', Path(arguments.config).stem)}")
    print(matrix)


if __name__ == "__main__":
    main()
