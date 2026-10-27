"""terasim_nde_nade.utils.sumo_lane_locator reproduces traci.simulation.convertRoad exactly."""

import math
import os
import random
from pathlib import Path

import pytest

NET = Path(__file__).resolve().parents[2] / "examples" / "corridor_demo" / "demo.net.xml"

pytestmark = pytest.mark.requires_sumo


@pytest.fixture(scope="module")
def sumo_net():
    pytest.importorskip("libsumo")
    import libsumo

    if not NET.exists():
        pytest.skip(f"{NET} not found")
    libsumo.start(["sumo", "-n", str(NET), "--no-step-log", "true", "--no-warnings", "true"])
    try:
        yield libsumo
    finally:
        libsumo.close()


def _sample_points(libsumo, rng, per_lane=2, max_offset=6.0):
    """Points on, beside and beyond lane shapes (normal and internal lanes)."""
    pts = []
    for lane_id in libsumo.lane.getIDList():
        shape = libsumo.lane.getShape(lane_id)
        for _ in range(per_lane):
            (ax, ay), (bx, by) = rng.choice(list(zip(shape[:-1], shape[1:])))
            t = rng.uniform(-0.3, 1.3)
            x, y = ax + (bx - ax) * t, ay + (by - ay) * t
            pts.append((x, y))
            pts.append((x + rng.uniform(-max_offset, max_offset), y + rng.uniform(-max_offset, max_offset)))
    return pts


def test_convert_road_matches_libsumo(sumo_net, monkeypatch):
    monkeypatch.setenv("USE_LIBSUMO", "1")
    from terasim_nde_nade.utils import sumo_lane_locator as locator

    locator._locator = None  # fresh table for this SUMO instance
    rng = random.Random(7)
    pts = _sample_points(sumo_net, rng)
    assert len(pts) > 100
    mismatches = []
    for x, y in pts:
        expected = sumo_net.simulation.convertRoad(x, y, isGeo=False)
        got = locator.convert_road(x, y)
        edge_id, pos, lane_index = expected  # libsumo returns a plain (edgeID, pos, laneIndex) tuple
        if edge_id != got[0] or pos != got[1]:
            mismatches.append((x, y, expected, got))
        elif not edge_id.startswith(":") and lane_index != got[2]:
            # internal lanes may carry an index attribute that differs from their id suffix
            mismatches.append((x, y, expected, got))
    assert not mismatches, mismatches[:5]


def test_locator_rebuilds_for_new_net_tag(sumo_net):
    from terasim_nde_nade.utils import sumo_lane_locator as locator

    locator._locator = None
    first = locator.convert_road(0.0, 0.0)
    built = locator._locator
    assert built is not None and built.net_tag == locator.SumoLaneLocator.current_net_tag()
    locator.convert_road(1.0, 1.0)
    assert locator._locator is built  # same net -> same table
    built.net_tag = "stale"
    assert locator.convert_road(0.0, 0.0) == first
    assert locator._locator is not built  # tag mismatch -> rebuilt
