"""End-to-end checks for the deliberately planted synthetic match traits."""

from __future__ import annotations

import pytest

from generator.match import generate_match
from engine.patterns import block, flank_use, marking, momentum, reliance


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
