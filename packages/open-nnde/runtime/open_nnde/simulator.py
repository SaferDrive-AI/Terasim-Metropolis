"""The object TeraSim's NNDE environment drives (``nnde_simulator``), backed by public MTR.

Interface used by ``terasim_nde_nade.envs.nnde.NNDE``: ``desc``, ``sim_resolution``,
``history_step``, ``current_step``, ``map`` (``pos_offset``, ``normalize``, ``denormalize``),
``agent_record``, ``scenario_manager`` (``current_step``, ``vector_map.formatted_update``),
``init_traffic_light``, ``update`` and ``step``.

``step`` predicts the vehicles that have a complete 1.1 s history (or the requested subset)
and returns, for each, the next 1.0 s of the chosen mode as rows [x, y, heading, vx, vy, ax,
ay] relative to ``map.pos_offset``. MTR does not read traffic lights and does not know a
vehicle's route; NNDE only hands the trajectory to vehicles away from junctions and stop
lines, and SUMO drives the rest.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .agents import STATE_DIM, AgentRecord
from .sumo_map import MtrMap, map_from_net

SIM_RESOLUTION = 0.1
HISTORY_STEP = 11
OUTPUT_STEPS = 10  # 1.0 s; NNDE executes the first CNDE_EXECUTE_STEPS of it
MOVING_MPS = 2.0  # below this speed any mode direction is acceptable
MAX_TURN_RAD = np.deg2rad(45.0)  # a mode turning more than this within 1 s is passed over
MAX_ACCEL = 8.0

_MAPS: dict[str, MtrMap] = {}


def map_for(map_path) -> MtrMap:
    """The MTR map of the MapStore package holding ``map_path`` (its ``net.net.xml``)."""
    path = Path(map_path)
    net = path if path.name.endswith(".net.xml") else path.parent / "net.net.xml"
    key = str(net.resolve())
    if key not in _MAPS:
        if not net.exists():
            raise FileNotFoundError(f"open NNDE needs the SUMO network next to the map: {net}")
        _MAPS[key] = map_from_net(net)
    return _MAPS[key]


class MapFrame:
    """SUMO coordinates <-> coordinates relative to ``pos_offset`` (the network's centre)."""

    def __init__(self, pos_offset):
        self.pos_offset = np.asarray(pos_offset, dtype=np.float64)

    def normalize(self, states, dim=(0, 1)):
        out = np.array(states, dtype=np.float64, copy=True)
        out[..., list(dim)] -= self.pos_offset[: len(dim)]
        return out

    def denormalize(self, states, dim=(0, 1)):
        out = np.array(states, dtype=np.float64, copy=True)
        out[..., list(dim)] += self.pos_offset[: len(dim)]
        return out


class _VectorMap:
    """Traffic light states arrive here; MTR does not read them."""

    def __init__(self):
        self.last_update = None

    def formatted_update(self, update, proceed=False):
        self.last_update = update


class _ScenarioManager:
    def __init__(self, record: AgentRecord):
        self._record = record
        self.vector_map = _VectorMap()

    @property
    def current_step(self) -> int:
        return self._record.current_step


def _wrap(angle):
    return (angle + np.pi) % (2 * np.pi) - np.pi


class OpenNndeSimulator:
    def __init__(self, mtr_map: MtrMap, predictor, desc: str = "terasim-cnde"):
        self.desc = desc
        self.sim_resolution = SIM_RESOLUTION
        self.history_step = HISTORY_STEP
        self.mtr_map = mtr_map
        self.map = MapFrame(mtr_map.pos_offset)
        self.agent_record = AgentRecord(HISTORY_STEP)
        self.scenario_manager = _ScenarioManager(self.agent_record)
        self.predictor = predictor
        self.traffic_lights = None
        self.stats = {"steps": 0, "predicted": 0, "turn_guard": 0}

    @property
    def current_step(self) -> int:
        return self.agent_record.current_step

    def init_traffic_light(self, tls_id, tls_states):
        self.traffic_lights = (list(tls_id), tls_states)

    def update(self, scenario, save_file=None):
        """Advance one step with the given vehicle states ((n, 7) or (n, steps, 7): first row)."""
        ids = list(scenario["vehicle_id"])
        states = np.asarray(scenario["vehicle_states"], dtype=np.float64)
        if states.ndim == 3:
            states = states[:, 0, :]
        states = states.reshape(len(ids), -1)[:, :STATE_DIM] if ids else np.zeros((0, STATE_DIM))
        self.agent_record.proceed(ids, states)

    def step(self, predicted_green_tls=None, center_agent_ids=None):
        record = self.agent_record
        centers = record.complete_history_ids()
        if center_agent_ids is not None:
            wanted = set(center_agent_ids)
            centers = [vid for vid in centers if vid in wanted]
        object_ids = record.current_track_agent_ids
        predictions = {}
        if centers:
            predictions = self.predictor.predict(
                object_ids, self._object_trajs(object_ids), centers, self.mtr_map
            )
        out_ids, out_states = [], []
        for vid in centers:
            if vid in predictions:
                positions, scores = predictions[vid]
                out_ids.append(vid)
                out_states.append(
                    self._trajectory(record.get_agents(vid).states[-1], positions, scores)
                )
        self.stats["steps"] += 1
        self.stats["predicted"] += len(out_ids)
        states = np.stack(out_states) if out_states else np.zeros((0, OUTPUT_STEPS, STATE_DIM))
        return {"vehicle_id": out_ids, "vehicle_states": states}

    # -- helpers -----------------------------------------------------------------------------
    def _object_trajs(self, object_ids) -> np.ndarray:
        """(n, 11, 10) MTR track rows in map coordinates, invalid rows before a short history."""
        trajs = np.zeros((len(object_ids), HISTORY_STEP, 10), dtype=np.float32)
        for k, vid in enumerate(object_ids):
            agent = self.agent_record.get_agents(vid)
            rows = agent.states[-HISTORY_STEP:]
            n = len(rows)
            block = trajs[k, HISTORY_STEP - n :]
            block[:, 0:2] = rows[:, 0:2] + self.map.pos_offset
            block[:, 3:6] = agent.static
            block[:, 6] = rows[:, 2]
            block[:, 7:9] = rows[:, 3:5]
            block[:, 9] = 1.0
        return trajs

    def _trajectory(self, current, positions, scores) -> np.ndarray:
        """The chosen mode's next 1.0 s as (OUTPUT_STEPS, 7) rows relative to pos_offset."""
        p0 = current[0:2] + self.map.pos_offset
        speed0 = float(np.hypot(current[3], current[4]))
        order = np.argsort(-np.asarray(scores))
        chosen = int(order[0])
        if speed0 > MOVING_MPS:
            for m in order:
                disp = positions[m, OUTPUT_STEPS - 1] - p0
                if (
                    np.hypot(*disp) < 1.0
                    or abs(_wrap(np.arctan2(disp[1], disp[0]) - current[2])) <= MAX_TURN_RAD
                ):
                    chosen = int(m)
                    break
            if chosen != int(order[0]):
                self.stats["turn_guard"] += 1

        pts = positions[chosen, :OUTPUT_STEPS].astype(np.float64) - self.map.pos_offset
        prev = np.vstack([current[None, 0:2], pts[:-1]])
        vel = (pts - prev) / SIM_RESOLUTION
        prev_vel = np.vstack([current[None, 3:5], vel[:-1]])
        acc = np.clip((vel - prev_vel) / SIM_RESOLUTION, -MAX_ACCEL, MAX_ACCEL)
        heading = np.empty(OUTPUT_STEPS)
        last = current[2]
        for k in range(OUTPUT_STEPS):
            if np.hypot(*vel[k]) > 0.5:
                last = np.arctan2(vel[k, 1], vel[k, 0])
            heading[k] = last
        out = np.zeros((OUTPUT_STEPS, STATE_DIM))
        out[:, 0:2], out[:, 2], out[:, 3:5], out[:, 5:7] = pts, heading, vel, acc
        return out
