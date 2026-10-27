"""Tests for terasim_service.city.aggregate against the corridor demo snapshots."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from terasim_service.city.aggregate import aggregate_snapshots
from terasim_city.flow.descriptors import (
    TMCData,
    TMC_DIRECTION_TO_CANONICAL,
    TMC_TURN_TO_CANONICAL,
)

from tests.fixtures.tmc_templates import demo_snapshots

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def snapshots() -> list[dict]:
    return demo_snapshots()


def test_aggregate_six_demo_snapshots(snapshots):
    result = aggregate_snapshots(snapshots)

    assert result["corridor_id"] == "demo_arterial"
    # 6 x 10min = 3600 seconds.
    assert result["observation_interval"] == "3600s"
    # Earliest input timestamp_start wins.
    assert result["timestamp_start"] == min(s["timestamp_start"] for s in snapshots)

    # Same intersections, first-seen order preserved.
    ids = [ix["intersection_id"] for ix in result["intersections"]]
    assert ids == [ix["intersection_id"] for ix in snapshots[0]["intersections"]]

    # lat/lon from the first occurrence.
    first = snapshots[0]["intersections"][0]
    agg_first = result["intersections"][0]
    assert agg_first["lat"] == pytest.approx(first["lat"])
    assert agg_first["lon"] == pytest.approx(first["lon"])

    # Counts equal the elementwise sum over all snapshots.
    expected: dict[str, dict[str, dict[str, int]]] = {}
    for snap in snapshots:
        for ix in snap["intersections"]:
            per_ix = expected.setdefault(ix["intersection_id"], {})
            for direction, turns in ix["counts"].items():
                per_dir = per_ix.setdefault(direction, {})
                for turn, count in turns.items():
                    per_dir[turn] = per_dir.get(turn, 0) + int(count)
    for ix in result["intersections"]:
        assert ix["counts"] == expected[ix["intersection_id"]]

    # Verify one external TMC movement by hand.
    i1_east_straight = sum(
        ix["counts"]["East"]["Straight"]
        for snap in snapshots
        for ix in snap["intersections"]
        if ix["intersection_id"] == "i1"
    )
    agg_i1 = next(
        ix for ix in result["intersections"] if ix["intersection_id"] == "i1"
    )
    assert agg_i1["counts"]["East"]["Straight"] == i1_east_straight


def test_aggregate_output_round_trips_through_tmc_data(snapshots, tmp_path):
    result = aggregate_snapshots(snapshots)
    out = tmp_path / "aggregated_tmc.json"
    out.write_text(json.dumps(result), encoding="utf-8")

    data = TMCData.from_json(out)
    assert data.corridor_id == "demo_arterial"
    assert data.observation_seconds() == pytest.approx(3600.0)
    assert len(data.intersections) == len(result["intersections"])
    by_id = {ix.intersection_id: ix for ix in data.intersections}
    for ix in result["intersections"]:
        expected = {
            TMC_DIRECTION_TO_CANONICAL[direction]: {
                TMC_TURN_TO_CANONICAL[turn]: count
                for turn, count in turns.items()
            }
            for direction, turns in ix["counts"].items()
        }
        assert by_id[ix["intersection_id"]].counts == expected


def test_aggregate_mixed_corridor_id_raises(snapshots):
    other = dict(snapshots[1])
    other["corridor_id"] = "another_corridor"
    with pytest.raises(ValueError, match="corridor_id"):
        aggregate_snapshots([snapshots[0], other])


def test_aggregate_empty_list_raises():
    with pytest.raises(ValueError, match="empty"):
        aggregate_snapshots([])


def test_aggregate_rejects_unknown_tmc_direction(snapshots):
    bad = copy.deepcopy(snapshots[0])
    bad["intersections"][0]["counts"] = {
        "Southeast": {"Straight": 10}
    }
    with pytest.raises(ValueError, match="unknown direction"):
        aggregate_snapshots([bad])


def test_aggregate_missing_intersection_raises(snapshots):
    """A snapshot that drops i2 must fail loud, not silently halve i2's flow."""
    partial = copy.deepcopy(snapshots[1])
    partial["intersections"] = [
        ix for ix in partial["intersections"] if ix["intersection_id"] != "i2"
    ]
    with pytest.raises(ValueError) as exc:
        aggregate_snapshots([snapshots[0], partial])
    message = str(exc.value)
    assert "i2" in message
    assert "missing" in message
    # The offending snapshot is identified by its timestamp_start.
    assert partial["timestamp_start"] in message


def test_aggregate_negative_count_raises(snapshots):
    bad = copy.deepcopy(snapshots[0])
    bad["intersections"][0]["counts"]["East"]["Straight"] = -5
    with pytest.raises(ValueError, match=r">= 0.*-5"):
        aggregate_snapshots([bad])


def test_aggregate_duplicate_timestamp_raises(snapshots):
    duplicate = copy.deepcopy(snapshots[0])
    with pytest.raises(ValueError, match="duplicate timestamp_start"):
        aggregate_snapshots([snapshots[0], duplicate])


def test_aggregate_rejects_intersection_coordinate_drift(snapshots):
    moved = copy.deepcopy(snapshots[1])
    moved["intersections"][0]["lat"] += 0.001

    with pytest.raises(ValueError, match="coordinates changed"):
        aggregate_snapshots([snapshots[0], moved])


def test_aggregate_orders_timezone_aware_timestamps_chronologically(snapshots):
    first = copy.deepcopy(snapshots[0])
    second = copy.deepcopy(snapshots[1])
    first["timestamp_start"] = "2026-07-22T09:00:00+02:00"
    second["timestamp_start"] = "2026-07-22T07:10:00+00:00"

    result = aggregate_snapshots([second, first])

    assert result["timestamp_start"] == first["timestamp_start"]


def test_aggregate_rejects_overlapping_observation_windows(snapshots):
    first = copy.deepcopy(snapshots[0])
    second = copy.deepcopy(snapshots[1])
    first["timestamp_start"] = "2026-07-22T08:00:00Z"
    second["timestamp_start"] = "2026-07-22T08:05:00Z"

    with pytest.raises(ValueError, match="overlaps"):
        aggregate_snapshots([first, second])

