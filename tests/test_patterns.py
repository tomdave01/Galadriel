"""End-to-end checks for the deliberately planted synthetic match traits."""

from __future__ import annotations

import pytest

from generator.match import generate_match
from engine.patterns import (
    _classify_lost_pass,
    _press_success,
    block,
    flank_use,
    influence,
    marking,
    momentum,
    press_triggers,
    reliance,
    shape,
    turnover_zones,
)
from engine.schema import validate_event, validate_frame


@pytest.fixture(scope="module")
def match_data():
    return generate_match(seed=42)


def test_rovers_high_press_and_right_flank(match_data):
    events, frames = match_data
    assert block(frames, "Rovers")["block"] == "high press"
    right_flank = flank_use(events, "Rovers")
    assert right_flank["top_flank"] == "right"
    assert right_flank["flagged"] is True
    assert right_flank["top_share"] > 0.45


def test_united_low_block_and_man_oriented_marking(match_data):
    events, frames = match_data
    assert block(frames, "United")["block"] == "low block"
    result = marking(frames, "United", "Rovers")
    assert result["style"] == "man-oriented"
    assert result["consistency"] > 0.65
    assert result["mean_distance"] < 9


def test_rovers_key_player_reliance_is_flagged(match_data):
    events, _ = match_data
    result = reliance(events, "Rovers")
    assert result["top_player"] == "Rico Vale"
    assert result["flagged"] is True
    assert result["share"] > 0.15


def test_momentum_swing_is_detected_near_minute_60(match_data):
    events, _ = match_data
    series = momentum(events)
    crossings = [
        current["minute"]
        for previous, current in zip(series, series[1:])
        if previous["value"] < 0 <= current["value"]
        and 50 <= current["minute"] <= 70
    ]
    assert crossings, [
        (item["minute"], item["value"]) for item in series if 45 <= item["minute"] <= 70
    ]
    assert min(abs(minute - 60) for minute in crossings) <= 10


def test_synthetic_score_contains_two_to_four_goals(match_data):
    events, _ = match_data
    goals = [event for event in events if event["outcome"] == "goal"]
    assert 2 <= len(goals) <= 4
    assert {event["team"] for event in goals} == {"Rovers", "United"}


def test_generated_data_matches_shared_schema_and_normalized_coordinates(match_data):
    events, frames = match_data
    for event in events:
        validate_event(event)
    for frame in frames:
        validate_frame(frame)

    assert all(0 <= event["x"] <= 100 and 0 <= event["y"] <= 100 for event in events)
    assert all(
        0 <= player["x"] <= 100 and 0 <= player["y"] <= 100
        for frame in frames
        for player in frame["players"]
    )


def test_team_direction_normalizes_longitudinal_and_lateral_zones():
    rovers_frame = {
        "poss": "United",
        "players": [
            {"id": f"r{i}", "team": "Rovers", "role": "MID", "x": x, "y": y}
            for i, (x, y) in enumerate(((60, 80), (65, 75), (70, 70)))
        ],
    }
    united_frame = {
        "poss": "Rovers",
        "players": [
            {"id": f"u{i}", "team": "United", "role": "MID", "x": x, "y": y}
            for i, (x, y) in enumerate(((40, 20), (35, 25), (30, 30)))
        ],
    }
    assert shape([rovers_frame], "Rovers", False)["line_height"] == shape(
        [united_frame], "United", False
    )["line_height"]

    rovers_event = {
        "team": "Rovers", "type": "pass", "outcome": "lost",
        "x": 20, "y": 20, "end_x": 10, "end_y": 20,
    }
    united_event = {
        "team": "United", "type": "pass", "outcome": "lost",
        "x": 80, "y": 80, "end_x": 90, "end_y": 80,
    }
    assert flank_use([rovers_event], "Rovers")["top_flank"] == "left"
    assert flank_use([united_event], "United")["top_flank"] == "left"
    assert turnover_zones([rovers_event], "Rovers")["zone"] == "defensive_left"
    assert turnover_zones([united_event], "United")["zone"] == "defensive_left"
    assert _classify_lost_pass(
        {"team": "United", "x": 70, "y": 50, "end_x": 80, "end_y": 50}
    ) == "back_pass"


def test_empty_inputs_and_sparse_shape_are_safe():
    assert shape([], "Rovers", False)["formation"] == ""
    one_sample = {
        "poss": "United",
        "players": [
            {"id": "p1", "team": "Rovers", "role": "MID", "x": 60, "y": 50}
        ],
    }
    assert shape([one_sample], "Rovers", False)["formation"] == ""
    assert block([], "Rovers")["block"] == "low block"
    assert flank_use([], "United")["flagged"] is False
    assert reliance([], "United")["flagged"] is False
    assert turnover_zones([], "United")["count"] == 0
    assert press_triggers([], "United")["best_trigger"] is None
    assert marking([], "United", "Rovers")["style"] == "zonal"
    assert momentum([]) == [{"minute": 0.0, "rovers": 0.0, "united": 0.0, "value": 0.0}]
    assert influence([]) == []
    missing_ball_frame = {
        "ball": None,
        "players": [
            {"team": "United", "role": "DEF", "x": 50, "y": 50},
            {"team": "United", "role": "MID", "x": 51, "y": 50},
        ],
    }
    assert _press_success(missing_ball_frame, "United") is False
