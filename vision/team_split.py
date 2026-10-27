"""Split tracked players into two shirt-color teams using KMeans."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from typing import Any


def load_roster_map(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {"team_clusters": {"0": "Rovers", "1": "United"}, "players": {}}
    roster_path = Path(path)
    try:
        data = json.loads(roster_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unable to read roster map {roster_path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("The roster map must be a JSON object")
    team_clusters = data.get("team_clusters", {"0": "Rovers", "1": "United"})
    players = data.get("players", {})
    if not isinstance(team_clusters, dict) or not isinstance(players, dict):
        raise ValueError("roster_map.team_clusters and roster_map.players must be JSON objects")
    for cluster, team in team_clusters.items():
        if str(cluster) not in {"0", "1"} or team not in {"Rovers", "United"}:
            raise ValueError("team_clusters must map cluster 0/1 to Rovers or United")
    for tracker_id, entry in players.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError(f"Roster entry {tracker_id} must have a player name")
        if entry.get("team") not in {"Rovers", "United"}:
            raise ValueError(f"Roster entry {tracker_id} must have team Rovers or United")
        if entry.get("role") not in {"GK", "DEF", "MID", "FWD"}:
            raise ValueError(f"Roster entry {tracker_id} must have role GK, DEF, MID, or FWD")
    return {
        "team_clusters": {str(key): value for key, value in team_clusters.items()},
        "players": {str(key): value for key, value in players.items()},
    }


def split_teams(
    tracks: dict[str, Any],
    *,
    team_clusters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assign player detections to shirt-color clusters; distant colors are referees."""
    try:
        import numpy as np
        from sklearn.cluster import KMeans
    except ImportError as error:
        raise RuntimeError(
            "Team splitting requires NumPy and scikit-learn; "
            "install vision/requirements.txt"
        ) from error

    observations: dict[str, list[list[int]]] = {}
    for frame in tracks.get("frames", []):
        for detection in frame.get("detections", []):
            color = detection.get("torso_color")
            tracker_id = detection.get("track_id")
            if (
                detection.get("label") == "person"
                and tracker_id is not None
                and isinstance(color, list)
                and len(color) == 3
            ):
                observations.setdefault(str(tracker_id), []).append(
                    [int(channel) for channel in color]
                )
    if len(observations) < 2:
        raise ValueError(
            "At least two tracked players with torso colors are required to split teams"
        )

    identities = sorted(observations)
    colors = np.asarray(
        [np.median(observations[identity], axis=0) for identity in identities],
        dtype=np.float64,
    )
    if len(np.unique(colors, axis=0)) < 2:
        raise ValueError(
            "At least two distinct shirt colors are required to split teams"
        )
    model = KMeans(n_clusters=2, random_state=42, n_init=10)
    labels = model.fit_predict(colors)
    centers = model.cluster_centers_
    distances = np.linalg.norm(colors - centers[labels], axis=1)

    cutoff_by_cluster: dict[int, float] = {}
    for cluster in (0, 1):
        cluster_distances = distances[labels == cluster].tolist()
        typical = median(cluster_distances) if cluster_distances else 0.0
        cutoff_by_cluster[cluster] = max(28.0, typical * 3.0 + 14.0)

    configured_teams = {
        str(key): value
        for key, value in (team_clusters or {"0": "Rovers", "1": "United"}).items()
    }
    if set(configured_teams) != {"0", "1"} or set(configured_teams.values()) != {"Rovers", "United"}:
        raise ValueError("team_clusters must assign one cluster to each of Rovers and United")

    identity_result: dict[str, dict[str, Any]] = {}
    for index, identity in enumerate(identities):
        cluster = int(labels[index])
        is_outlier = float(distances[index]) > cutoff_by_cluster[cluster]
        identity_result[identity] = {
            "cluster": cluster,
            "team": None if is_outlier else configured_teams[str(cluster)],
            "referee_candidate": is_outlier,
            "color_distance": round(float(distances[index]), 2),
        }

    for frame in tracks.get("frames", []):
        for detection in frame.get("detections", []):
            if detection.get("label") != "person" or detection.get("track_id") is None:
                continue
            classification = identity_result.get(str(detection["track_id"]))
            if classification:
                detection.update(classification)
            else:
                detection.update(
                    {"cluster": None, "team": None, "referee_candidate": True}
                )

    tracks["team_split"] = {
        "algorithm": "KMeans(2) on median torso BGR per tracker ID",
        "teams_by_cluster": configured_teams,
        "color_centers_bgr": [
            [round(float(channel), 2) for channel in center] for center in centers
        ],
        "players": identity_result,
    }
    return tracks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tracks", help="intermediate tracks JSON from vision.track")
    parser.add_argument("--roster-map", default=str(Path(__file__).with_name("roster_map.json")))
    parser.add_argument("--output", help="output path; defaults to <tracks>_teams.json")
    arguments = parser.parse_args()
    tracks_path = Path(arguments.tracks)
    try:
        tracks = json.loads(tracks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Unable to read tracks file {tracks_path}: {error}") from error
    roster = load_roster_map(arguments.roster_map)
    classified = split_teams(tracks, team_clusters=roster["team_clusters"])
    destination = Path(arguments.output) if arguments.output else tracks_path.with_name(
        f"{tracks_path.stem}_teams.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(classified, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Classified {len(classified['team_split']['players'])} tracked players: {destination}")


if __name__ == "__main__":
    main()
