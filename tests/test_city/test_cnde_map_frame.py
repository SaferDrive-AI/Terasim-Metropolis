"""Polylines and agent states share one frame after a Neural NDE map is loaded.

``BaseVectorMap.export`` stores polylines relative to ``map_center``, while maps converted from
SUMO (``tools/dataset/x2cnde.py``) store the SUMO network center next to absolute polylines.
Agent states are normalized with ``pos_offset`` in both cases, so ``load`` moves absolute
polylines into that frame and leaves relative ones alone.

The runtime (``csim``, the ``cnde`` package) is an optional GPU module installed separately;
these checks skip when it is not installed.
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

NEURAL_NDE_MISSING = "the optional Neural NDE runtime (cnde) is not installed"

if importlib.util.find_spec("csim") is None:
    pytest.skip(NEURAL_NDE_MISSING, allow_module_level=True)

scenario = pytest.importorskip("csim.scenario.scenario", reason=NEURAL_NDE_MISSING)

CENTER = np.array([338.0, 481.0])
LANE_STARTS = [(300.0, 450.0), (360.0, 500.0), (340.0, 470.0)]


def _map_dict(offset, map_center=None):
    """Three straight lanes near CENTER, given relative to ``offset``."""
    polylines = {}
    for k, (x, y) in enumerate(LANE_STARTS):
        xs = np.linspace(x, x + 20.0, 5)
        states = np.stack([xs, np.full_like(xs, y), np.zeros_like(xs)], axis=1)
        states[:, :2] -= offset
        polylines[f"lane{k}"] = {"states": states, "type": 1}
    dic = {"polyline": polylines}
    if map_center is not None:
        dic["map_center"] = np.asarray(map_center, dtype=np.float64)
    return dic


def test_absolute_polylines_next_to_map_center_are_shifted():
    vm = scenario.BaseVectorMap().load(_map_dict(offset=np.zeros(2), map_center=CENTER))
    lane = vm.polyline_dict["lane0"]
    np.testing.assert_allclose(lane.states[0, :2], [300.0 - 338.0, 450.0 - 481.0])
    np.testing.assert_allclose(lane.center_location[0], [310.0 - 338.0, 450.0 - 481.0])
    np.testing.assert_allclose(vm.denormalize(lane.states[:1, :2], dim=[0, 1])[0], [300.0, 450.0])


def test_relative_polylines_next_to_map_center_are_unchanged():
    vm = scenario.BaseVectorMap().load(_map_dict(offset=CENTER, map_center=CENTER))
    np.testing.assert_allclose(vm.polyline_dict["lane0"].states[0, :2], [300.0 - 338.0, 450.0 - 481.0])


def test_export_round_trip_keeps_the_frame():
    vm = scenario.BaseVectorMap().load(_map_dict(offset=np.zeros(2), map_center=CENTER))
    again = scenario.BaseVectorMap().load(vm.export())
    for polyline_id, lane in vm.polyline_dict.items():
        np.testing.assert_allclose(again.polyline_dict[polyline_id].states, lane.states)


def test_load_leaves_the_input_dict_untouched():
    dic = _map_dict(offset=np.zeros(2), map_center=CENTER)
    scenario.BaseVectorMap().load(dic)
    np.testing.assert_allclose(dic["polyline"]["lane0"]["states"][0, :2], [300.0, 450.0])


def test_without_map_center_load_still_normalizes():
    vm = scenario.BaseVectorMap().load(_map_dict(offset=np.zeros(2)))
    points = np.concatenate([lane.points for lane in vm.polyline_dict.values()])
    np.testing.assert_allclose(points.mean(axis=0), [0.0, 0.0], atol=1e-9)
