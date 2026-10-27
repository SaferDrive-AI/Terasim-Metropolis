"""Unit tests for TMC schema parsing in :mod:`terasim_city.flow.descriptors`.

These tests exercise pure-Python validation logic — no SUMO, no filesystem
beyond ``tmp_path``. They are the negative-case backstop for the strict
validation we rely on in production (see ``TMCData.from_json``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from terasim_city.flow.descriptors import (
    CANONICAL_DIRECTIONS,
    CANONICAL_TURNS,
    FlowDescriptor,
    TMCData,
    TMCIntersection,
)


# ---------------------------------------------------------------------------
# TMCData.from_json — positive
# ---------------------------------------------------------------------------

def _write_json(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "tmc.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _minimal_payload() -> dict:
    """A payload that round-trips through ``TMCData.from_json`` cleanly."""
    return {
        "corridor_id": "demo",
        "observation_interval": "1h",
        "timestamp_start": "2026-01-01T00:00:00",
        "intersections": [
            {
                "intersection_id": "A",
                "lat": 37.3,
                "lon": -121.8,
                "counts": {
                    "East": {"Left": 10, "Straight": 20, "Right": 5},
                    "West": {"Left": 8, "Straight": 22, "Right": 4},
                },
            }
        ],
    }


def test_from_json_minimal_payload_parses(tmp_path: Path):
    data = TMCData.from_json(_write_json(tmp_path, _minimal_payload()))
    assert data.corridor_id == "demo"
    assert data.observation_interval == "1h"
    assert data.timestamp_start == "2026-01-01T00:00:00"
    assert len(data.intersections) == 1

    inter = data.intersections[0]
    assert isinstance(inter, TMCIntersection)
    assert inter.intersection_id == "A"
    assert inter.lat == pytest.approx(37.3)
    assert inter.lon == pytest.approx(-121.8)
    assert inter.counts["EB"]["through"] == 20


# ---------------------------------------------------------------------------
# TMCData.from_json — top-level negatives
# ---------------------------------------------------------------------------


def test_from_json_intersections_must_be_list(tmp_path: Path):
    payload = _minimal_payload()
    payload["intersections"] = {"not": "a list"}
    with pytest.raises(ValueError, match="must be a list"):
        TMCData.from_json(_write_json(tmp_path, payload))


def test_from_json_empty_intersections_rejected(tmp_path: Path):
    payload = _minimal_payload()
    payload["intersections"] = []
    with pytest.raises(ValueError, match="empty"):
        TMCData.from_json(_write_json(tmp_path, payload))


# ---------------------------------------------------------------------------
# TMCData.from_json — per-intersection negatives
# ---------------------------------------------------------------------------


def test_from_json_intersection_bad_lat(tmp_path: Path):
    payload = _minimal_payload()
    payload["intersections"][0]["lat"] = "not-a-number"
    with pytest.raises(ValueError, match="numeric"):
        TMCData.from_json(_write_json(tmp_path, payload))


def test_from_json_intersection_counts_not_dict(tmp_path: Path):
    payload = _minimal_payload()
    payload["intersections"][0]["counts"] = [1, 2, 3]
    with pytest.raises(ValueError, match="must be a dict"):
        TMCData.from_json(_write_json(tmp_path, payload))


def test_from_json_intersection_turn_count_not_int(tmp_path: Path):
    payload = _minimal_payload()
    payload["intersections"][0]["counts"]["East"]["Left"] = "many"
    with pytest.raises(ValueError, match="int-coercible"):
        TMCData.from_json(_write_json(tmp_path, payload))


def test_from_json_intersection_empty_counts_ok(tmp_path: Path):
    """An empty ``counts`` dict is valid — it just means no observed flow."""
    payload = _minimal_payload()
    payload["intersections"][0]["counts"] = {}
    data = TMCData.from_json(_write_json(tmp_path, payload))
    assert data.intersections[0].counts == {}


# ---------------------------------------------------------------------------
# TMCData.observation_seconds
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Constants & FlowDescriptor surface area
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TMC input contract
# ---------------------------------------------------------------------------

def _tmc_payload() -> dict:
    """The production turn-movement-count shape (synthetic values): a ten-minute
    window, a UTC ``Z`` timestamp with milliseconds, approaches listed
    South/West/North/East and turns Right/Straight/Left."""
    return {
        "corridor_id": "main_st",
        "observation_interval": "10m",
        "timestamp_start": "2026-07-01T15:20:00.000Z",
        "intersections": [
            {
                "intersection_id": "main_1st",
                "lat": 37.335,
                "lon": -121.90339,
                "counts": {
                    "South": {"Right": 5, "Straight": 21, "Left": 0},
                    "West": {"Right": 7, "Straight": 96, "Left": 0},
                    "North": {"Right": 4, "Straight": 18, "Left": 0},
                    "East": {"Right": 9, "Straight": 104, "Left": 3},
                },
            }
        ],
    }


def test_tmc_data_normalizes_external_movement_names(tmp_path: Path):
    from terasim_city.flow.descriptors import TMCData, TMCIntersection

    data = TMCData.from_json(_write_json(tmp_path, _tmc_payload()))

    assert isinstance(data.intersections[0], TMCIntersection)
    assert data.intersections[0].counts == {
        "SB": {"right": 5, "through": 21, "left": 0},
        "WB": {"right": 7, "through": 96, "left": 0},
        "NB": {"right": 4, "through": 18, "left": 0},
        "EB": {"right": 9, "through": 104, "left": 3},
    }
    assert data.raw == _tmc_payload()


def test_tmc_data_parses_single_letter_minute_interval(tmp_path: Path):
    from terasim_city.flow.descriptors import TMCData

    data = TMCData.from_json(_write_json(tmp_path, _tmc_payload()))
    assert data.observation_seconds() == pytest.approx(600.0)


@pytest.mark.parametrize(
    ("direction", "turn"),
    [("Southeast", "Straight"), ("East", "Uturn")],
)
def test_tmc_data_rejects_unknown_movement_names(
    tmp_path: Path, direction: str, turn: str
):
    from terasim_city.flow.descriptors import TMCData

    payload = _tmc_payload()
    payload["intersections"][0]["counts"] = {direction: {turn: 1}}
    with pytest.raises(ValueError, match="unknown (direction|turn)"):
        TMCData.from_json(_write_json(tmp_path, payload))
