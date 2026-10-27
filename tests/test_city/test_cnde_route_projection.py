"""BaseVectorMap.get_current_lane_from_candidate_batch == the per-agent shapely path, bit for bit.

``RouteTLScenarioManager._agent_map_interaction`` used to project every agent onto every
lane of its route with one shapely LineString/Point/project per (agent, lane); the batch
re-implements GEOS' project() and polyline_relative_pos as numpy ops over all pairs in the
same operation order. Same lane ids and the same distances to the last bit, including
exact vertex hits, points beyond either end and degenerate (duplicate-point) segments.
Needs csim (numpy + shapely); no GPU.
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

NEURAL_NDE_MISSING = "the optional Neural NDE runtime (cnde) is not installed"

if importlib.util.find_spec("csim") is None:
    pytest.skip(NEURAL_NDE_MISSING, allow_module_level=True)

scenario = pytest.importorskip("csim.scenario.scenario", reason=NEURAL_NDE_MISSING)


def _synthetic_map(seed=0):
    rng = np.random.default_rng(seed)
    polylines = {}
    for k in range(16):
        n = int(rng.integers(2, 21))
        start = rng.uniform(-300, 300, size=2)
        direction = rng.uniform(-np.pi, np.pi)
        pts = start + np.outer(np.arange(n) * 6.0, [np.cos(direction), np.sin(direction)])
        pts = pts + rng.normal(0, 0.4, size=pts.shape)
        heading = np.full((n,), direction) + rng.normal(0, 0.05, size=n)
        if k == 3 and n > 3:
            pts[2] = pts[1]  # duplicate consecutive point -> degenerate segment
        states = np.column_stack([pts, heading])
        polylines[f"lane_{k}"] = {
            "states": states, "type": 0, "prev_lanes_id": [], "next_lanes_id": [],
            "left_lanes_id": [], "right_lanes_id": [],
        }
    vm = scenario.BaseVectorMap()
    vm.load({"polyline": polylines, "map_center": np.zeros(2)})
    return vm


def test_batched_projection_matches_per_agent_shapely_path():
    vm = _synthetic_map()
    rng = np.random.default_rng(1)
    lane_ids = list(vm.polyline_dict)
    positions, routes = [], []
    for _ in range(400):
        route = list(rng.choice(lane_ids, size=int(rng.integers(1, 5)), replace=False))
        pts = np.asarray(vm.polyline_dict[route[0]].points)[:, :2]
        i = int(rng.integers(0, len(pts) - 1))
        t = rng.uniform(-0.6, 1.6)
        p = pts[i] + (pts[i + 1] - pts[i]) * t + rng.normal(0, 3.0, size=2)
        if rng.random() < 0.15:
            p = pts[i].copy()  # exact vertex hit
        positions.append(np.asarray(p, dtype=np.float64))
        routes.append(route)
    expected = [vm.get_current_lane_from_candidate(p, r, ret_dist=True) for p, r in zip(positions, routes)]
    got = vm.get_current_lane_from_candidate_batch(positions, routes)
    assert len(got) == len(expected)
    for (lane_a, dist_a), (lane_b, dist_b) in zip(expected, got):
        assert lane_a == lane_b
        assert dist_a == dist_b  # bitwise, not approx


def test_batched_projection_handles_empty_input():
    vm = _synthetic_map()
    assert vm.get_current_lane_from_candidate_batch([], []) == []
