"""Tests for incident metadata in city signal-evaluation KPI output."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from terasim_city.evaluation.diagnostics import build_incident


def test_build_incident_uses_items_as_the_only_vehicle_detail_shape() -> None:
    env = SimpleNamespace(
        stalled_edge_ids=["edge-a"],
        stalled_lane_ids=["edge-a_1"],
    )

    incident = build_incident(
        env,
        [{"latitude": 37.335, "longitude": -121.90045}],
    )

    assert incident == {
        "present": True,
        "count": 1,
        "items": [
            {
                "edge": "edge-a",
                "lane": "edge-a_1",
                "lat": 37.335,
                "lon": -121.90045,
            }
        ],
    }


def test_build_incident_rejects_edge_lane_count_mismatch() -> None:
    env = SimpleNamespace(
        stalled_edge_ids=["edge-a"],
        stalled_lane_ids=[],
    )

    with pytest.raises(ValueError, match="stalled edge/lane count mismatch"):
        build_incident(env, [])


def test_build_incident_rejects_stall_geometry_count_mismatch() -> None:
    # edge/lane counts agree, but there is no source stall geometry for the
    # placed edge: must raise rather than fabricate (0.0, 0.0) coordinates.
    env = SimpleNamespace(
        stalled_edge_ids=["edge-a"],
        stalled_lane_ids=["edge-a_1"],
    )

    with pytest.raises(ValueError, match="stall geometry count mismatch"):
        build_incident(env, [])
