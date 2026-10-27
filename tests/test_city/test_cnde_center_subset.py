"""RouteController._to_agent_centric_scenario with a centre subset (predict-control-only mode).

With ``center_vehicle_ids`` absent every scene agent is a centre (the original behaviour);
with a subset, only those agents get an agent-centric view while every scene agent can
still appear as a neighbour. Needs csim + a CUDA device (the controller builds its tensors
on the GPU); skipped otherwise.
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

NEURAL_NDE_MISSING = "the optional Neural NDE runtime (cnde) is not installed"

if importlib.util.find_spec("csim") is None:
    pytest.skip(NEURAL_NDE_MISSING, allow_module_level=True)

torch = pytest.importorskip("torch", reason=NEURAL_NDE_MISSING)
controller = pytest.importorskip("csim.controller.controller", reason=NEURAL_NDE_MISSING)

pytestmark = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs a CUDA device")


def _scenario(num_agent=12, num_poly=30, history=11, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.uniform(-40, 40, size=(num_agent, 2))
    states = np.zeros((num_agent, history, 10))
    states[..., :2] = centers[:, None, :] + rng.normal(0, 0.5, size=(num_agent, history, 2))
    states[..., 2] = rng.uniform(-np.pi, np.pi, size=(num_agent, 1))
    states[..., 3:7] = rng.normal(0, 1, size=(num_agent, history, 4))
    poly_center = rng.uniform(-60, 60, size=(num_poly, 2))
    poly_states = rng.normal(0, 1, size=(num_poly, 20, 9))
    poly_ids = [f"p{k}" for k in range(num_poly)]
    dyn_ids = poly_ids[:4]
    return {
        "vehicle_id": [f"v{k}" for k in range(num_agent)],
        "required_update_vehicle_ids": [f"v{k}" for k in range(num_agent)],
        "vehicle_states": states,
        "vehicle_mask": np.ones((num_agent, history), dtype=bool),
        "polyline_id": poly_ids,
        "polyline_center": poly_center,
        "polyline_states": poly_states,
        "polyline_mask": np.ones((num_poly, 20), dtype=bool),
        "route_polyline_id": [[] for _ in range(num_agent)],
        "route_polyline_states": rng.normal(0, 1, size=(num_agent, 3, 20, 9)),
        "route_polyline_mask": np.ones((num_agent, 3, 20)),
        "dynamic_polyline_id": dyn_ids,
        "dynamic_polyline_states": rng.normal(0, 1, size=(4, history, 4)),
        "dynamic_polyline_mask": np.ones((4, history), dtype=bool),
    }


class _Ctrl:
    pass


def _run(scenario):
    ctrl = _Ctrl()
    out = controller.RouteController._to_agent_centric_scenario(ctrl, scenario)
    return ctrl, out


def test_all_centers_equals_default_path():
    base_ctrl, base = _run(_scenario())
    sc = _scenario()
    sc["center_vehicle_ids"] = list(sc["vehicle_id"])
    ctrl, out = _run(sc)
    for key in ("vehicle_states", "vehicle_mask", "_map_polyline_indices", "dynamic_polyline_states", "dynamic_polyline_mask", "route_polyline"):
        assert torch.equal(base[key], out[key]), key
    assert list(base["vehicle_id"]) == list(out["vehicle_id"])
    assert torch.equal(base_ctrl._center_position, ctrl._center_position)


def test_center_subset_keeps_all_agents_as_context():
    sc = _scenario()
    chosen = ["v3", "v7", "v0"]
    sc["center_vehicle_ids"] = chosen
    # routes are built for the centres only in prepare_formatted_scenario; mimic that shape here
    sc["route_polyline_states"] = sc["route_polyline_states"][:3]
    sc["route_polyline_mask"] = sc["route_polyline_mask"][:3]
    ctrl, out = _run(sc)
    assert list(out["vehicle_id"]) == ["v3", "v7", "v0"]  # scene order of the chosen ids
    assert out["vehicle_states"].shape[0] == 3 and ctrl._center_position.shape[0] == 3
    # the view of each centre starts with itself, and its neighbours may be non-centres
    full = _scenario()
    full_ctrl, full_out = _run(full)
    for row, vid in enumerate(["v3", "v7", "v0"]):
        k = int(vid[1:])
        assert torch.equal(out["vehicle_states"][row], full_out["vehicle_states"][k])
        assert torch.equal(out["vehicle_mask"][row], full_out["vehicle_mask"][k])
        assert torch.equal(out["_map_polyline_indices"][row], full_out["_map_polyline_indices"][k])


def test_no_center_left_yields_empty_prediction_set():
    sc = _scenario()
    sc["center_vehicle_ids"] = ["not-a-vehicle"]
    _, out = _run(sc)
    assert len(out["vehicle_id"]) == 0
