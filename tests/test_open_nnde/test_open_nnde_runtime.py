"""The open Neural NDE runtime (packages/open-nnde) without torch: map conversion, vehicle
histories, the simulator's update/step contract and the map artifacts."""

import importlib
import pickle
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "packages" / "open-nnde" / "runtime"
DEMO_NET = REPO / "deployment" / "brev" / "demo-map-package" / "net.net.xml"


@pytest.fixture(scope="module")
def rt():
    """Import open_nnde from the runtime directory without leaving it on sys.path (its csim and
    cnde packages would otherwise satisfy other tests' runtime checks)."""
    sys.path.insert(0, str(RUNTIME))
    try:
        modules = {
            name: importlib.import_module(f"open_nnde.{name}")
            for name in ("sumo_map", "agents", "simulator")
        }
        modules["x2cnde"] = importlib.import_module("tools.dataset.x2cnde")
    finally:
        sys.path.remove(str(RUNTIME))
    return modules


def test_densify_keeps_ends_and_spacing(rt):
    pts = rt["sumo_map"].densify([(0, 0), (10, 0), (10, 3)])
    assert np.linalg.norm(np.diff(pts, axis=0), axis=1).max() <= 0.5 + 1e-9
    assert np.allclose(pts[0], (0, 0)) and np.allclose(pts[-1], (10, 3))


def test_polyline_direction_is_from_the_previous_point(rt):
    sm = rt["sumo_map"]
    line = sm.polyline(np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]), sm.LANE_SURFACE_STREET)
    assert (
        np.allclose(line[0, 3:6], 0)
        and np.allclose(line[1, 3:5], (1, 0))
        and np.allclose(line[2, 3:5], (0, 1))
    )
    assert (line[:, 6] == sm.LANE_SURFACE_STREET).all()


def test_demo_network_converts(rt):
    sm = rt["sumo_map"]
    m = sm.map_from_net(DEMO_NET)
    types = {int(p[0, 6]) for p in m.polylines}
    assert {sm.LANE_SURFACE_STREET, sm.ROAD_EDGE_BOUNDARY} <= types
    for p in m.polylines:
        assert np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1).max() <= 0.5 + 1e-3
    pieces, mask = sm.batch_polylines(m.polylines)
    assert pieces.shape[1:] == (20, 7) and mask.sum() == m.num_points
    lo = np.min([p[:, :2].min(0) for p in m.polylines], 0)
    hi = np.max([p[:, :2].max(0) for p in m.polylines], 0)
    assert np.all(lo < m.pos_offset) and np.all(m.pos_offset < hi)


def test_agent_record_matches_the_nnde_environment(rt):
    rec = rt["agents"].AgentRecord(history_step=11)
    rec.proceed(["a", "b"], np.arange(14, dtype=float).reshape(2, 7))
    assert rec.current_step == 0 and rec.current_track_agent_ids == ["a", "b"]
    for _ in range(12):
        rec.proceed(["a"], np.ones((1, 7)))
    ids, states, valid = rec[-1, ["a", "b"], :7]
    assert states.shape == (2, 7) and valid.all() and np.allclose(states[1], np.arange(7, 14))
    assert len(rec.get_agents("a").states) == 13 and set(rec.complete_history_ids()) == {"a", "b"}
    rec.get_agents("a")._states_per_step.states[-3:] = 5.0
    assert np.allclose(rec.get_agents("a").states[-1], 5.0)
    rec.add_agents_from_history_states("c", np.zeros((11, 7)), np.array([3.8, 1.8, 1.5]))
    rec.reset_agent_history("c", np.ones((11, 7)))
    assert np.allclose(rec.get_agents("c").states, 1.0)
    rec.force_remove_agents(["a", "unknown"])
    assert rec.current_track_agent_ids == ["b", "c"]


class StraightOrTurn:
    """Mode 0 (higher score) turns left, mode 1 drives straight east at 10 m/s."""

    def predict(self, object_ids, object_trajs, center_ids, mtr_map):
        out = {}
        for vid in center_ids:
            x0, y0 = object_trajs[object_ids.index(vid), -1, 0:2]
            t = (np.arange(80) + 1) * 0.1
            pos = np.zeros((6, 80, 2))
            pos[:, :, 0], pos[:, :, 1] = x0 + 10 * t, y0
            pos[0, :, 0], pos[0, :, 1] = x0, y0 + 10 * t
            out[vid] = (pos, np.array([0.6, 0.3, 0.05, 0.02, 0.02, 0.01]))
        return out


def test_simulator_step_contract_and_turn_guard(rt):
    sim_mod, sm = rt["simulator"], rt["sumo_map"]
    sim = sim_mod.OpenNndeSimulator(
        sm.MtrMap(polylines=[], pos_offset=np.array([100.0, 50.0])), StraightOrTurn()
    )
    hist = np.zeros((11, 7))
    hist[:, 0] = np.arange(11) - 10.0
    hist[:, 3] = 10.0
    sim.agent_record.add_agents_from_history_states("v", hist)
    out = sim.step(predicted_green_tls=None, center_agent_ids=["v"])
    assert out["vehicle_id"] == ["v"] and out["vehicle_states"].shape == (
        1,
        sim_mod.OUTPUT_STEPS,
        7,
    )
    traj = out["vehicle_states"][0]
    assert np.allclose(traj[:, 1], 0) and np.allclose(traj[:, 3], 10) and np.allclose(traj[:, 2], 0)
    assert sim.stats["turn_guard"] == 1
    sim.update(out)
    assert sim.current_step == 0 and np.allclose(
        sim.agent_record.get_agents("v").states[-1, 0], 1.0
    )


def test_simulator_with_no_vehicles(rt):
    sim_mod, sm = rt["simulator"], rt["sumo_map"]

    class NoModel:
        def predict(self, *_):
            raise AssertionError("nothing to predict")

    sim = sim_mod.OpenNndeSimulator(sm.MtrMap(polylines=[], pos_offset=np.zeros(2)), NoModel())
    out = sim.step(center_agent_ids=[])
    assert out["vehicle_id"] == [] and out["vehicle_states"].shape == (0, sim_mod.OUTPUT_STEPS, 7)
    sim.update(out)
    sim.update({"vehicle_id": [], "vehicle_states": np.zeros((0, 1, 7))})
    assert sim.current_step == 1
    frame = sim.map
    assert np.allclose(frame.denormalize(frame.normalize(np.ones((3, 7)))), 1.0)


def test_map_artifacts_for_a_map_package(rt, tmp_path):
    map_pkl, ids_pkl = tmp_path / "cnde_map.pkl", tmp_path / "cnde_id_mapping.pkl"
    rt["x2cnde"].create_map_data_from_netxml(str(DEMO_NET), str(map_pkl), str(ids_pkl))
    info = pickle.loads(map_pkl.read_bytes())
    mapping = pickle.loads(ids_pkl.read_bytes())
    assert info["format"] == "open-nnde/1" and info["polylines"] > 0
    assert mapping and all(v == [k] for k, v in mapping.items())
