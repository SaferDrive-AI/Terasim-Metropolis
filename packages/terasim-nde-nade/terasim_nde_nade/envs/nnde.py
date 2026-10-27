from addict import Dict
import builtins
import json
import pickle
import time
from loguru import logger
import numpy as np
import os
from pathlib import Path

# Fallback for line_profiler's @profile decorator
if not hasattr(builtins, 'profile'):
    builtins.profile = lambda f: f

from terasim.overlay import traci
from terasim.params import AgentType
import terasim.utils as utils

from .nde import NDE

from ..utils import (
    CommandType,
    NDECommand,
)
from ..utils.defence_logger import get_defence_logger


class CNDETrajectoryLogger:
    """Logs CNDE predicted trajectories and actual SUMO positions for verification.

    Uses incremental file writing so data is preserved even if the simulation
    crashes or is killed before on_stop() is called.
    """

    def __init__(self):
        self._active_vehicles = set()
        self._file = None
        self._path = None
        self._n_predictions = 0
        self._n_actuals = 0

    def set_output_path(self, output_path):
        """Open the output file for incremental writing. Must be called before logging."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = output_path
        self._file = open(output_path, "w")

    def _write_record(self, rec):
        if self._file is not None:
            self._file.write(json.dumps(rec) + "\n")
            self._file.flush()

    def update_active_vehicles(self, controlled_veh_ids):
        """Update the set of actively CNDE-controlled vehicles."""
        self._active_vehicles = set(controlled_veh_ids)

    def log_prediction(self, sim_time, vehicle_id, vehicle_states_terasim):
        """Log a CNDE prediction event (every 5 steps).

        Args:
            sim_time: Current simulation time.
            vehicle_id: Unused, kept for compatibility.
            vehicle_states_terasim: Dict mapping veh_id -> trajectory array in TeraSim coords
                                   Each trajectory is (N, 5): [x, y, heading, speed, relative_time].
        """
        if self._file is None:
            return
        for veh_id, traj in vehicle_states_terasim.items():
            traj_arr = np.array(traj) if not isinstance(traj, np.ndarray) else traj
            # Convert relative time (col 4) to absolute time
            if traj_arr.ndim == 2 and traj_arr.shape[1] >= 5:
                traj_arr = traj_arr.copy()
                traj_arr[:, 4] += sim_time
            self._write_record({
                "type": "prediction",
                "sim_time": float(sim_time),
                "vehicle_id": veh_id,
                "trajectory": traj_arr.tolist(),
            })
            self._n_predictions += 1
            self._active_vehicles.add(veh_id)

    def log_actual_positions_all_active(self, sim_time):
        """Log actual SUMO positions for all currently active CNDE-controlled vehicles.

        Called every simulation step to capture continuous actual trajectories.
        Each position is written as an individual record for crash-safety.
        """
        if self._file is None or not self._active_vehicles:
            return
        veh_list = set(traci.vehicle.getIDList())
        for veh_id in self._active_vehicles:
            if veh_id in veh_list:
                pos = traci.vehicle.getPosition(veh_id)
                heading = traci.vehicle.getAngle(veh_id)
                speed = traci.vehicle.getSpeed(veh_id)
                self._write_record({
                    "type": "actual",
                    "sim_time": float(sim_time),
                    "vehicle_id": veh_id,
                    "x": float(pos[0]), "y": float(pos[1]),
                    "heading": float(heading), "speed": float(speed),
                })
                self._n_actuals += 1

    def close(self):
        """Close the output file and log summary."""
        if self._file is not None:
            self._file.close()
            self._file = None
            logger.info(f"CNDE trajectory log saved to {self._path} "
                        f"({self._n_predictions} predictions, {self._n_actuals} actual records)")


class NNDE(NDE):
    """Neural Naturalistic Driving Environment.

    Same level as NDE, uses neural network for vehicle control decisions.
    This class can be used as a base class for NADE when neural network
    control is desired instead of traditional SUMO control.
    """

    # Number of trajectory steps to execute before releasing to SUMO.
    # The NN always predicts a full 5-step trajectory, but we may only execute
    # the first N steps. The remaining steps let SUMO's car-following model
    # correct lane position drift from CNDE's coordinate-based control.
    # Default 5 = execute full trajectory. Lane safety is handled by the
    # controller's lane mismatch detection + cooldown mechanism.
    CNDE_EXECUTE_STEPS = 5

    def __init__(self, *args, nnde_simulator, map_id_mapping: str | None = None, **kwargs):
        """Initialize the NNDE environment.

        Args:
            nnde_simulator: Neural network simulator for making decisions.
            map_id_mapping: Path to pickle file containing ID mapping for traffic lights.
            *args: Arguments passed to NDE.
            **kwargs: Keyword arguments passed to NDE.
        """
        super().__init__(*args, **kwargs)
        self.nnde_simulator = nnde_simulator
        if map_id_mapping and os.path.isfile(map_id_mapping):
            self.id_mapping = pickle.load(open(map_id_mapping, "rb"))
        else:
            self.id_mapping = None
        self.nnde_update_tag = 0
        self.cnde_traj_logger = CNDETrajectoryLogger()
        # Open log file immediately so data is written incrementally (crash-safe);
        # skipped entirely when the env runs with cnde_diag_logs=False (NDE.__init__).
        if getattr(self, "cnde_diag_logs", True):
            if hasattr(self, 'log_dir') and self.log_dir is not None:
                log_path = Path(self.log_dir) / "cnde_trajectory_log.jsonl"
            else:
                log_path = Path("output") / "cnde_trajectory_log.jsonl"
            self.cnde_traj_logger.set_output_path(log_path)

    def get_cnde_vehicle_state(self, env_observation):
        """Get vehicle states in CNDE format from environment observation.

        Args:
            env_observation: Environment observation dictionary.

        Returns:
            tuple: (vehicle_id array, vehicle_states array)
        """
        vehicles = env_observation[AgentType.VEHICLE]
        vehicle_id = []
        vehicle_states = []
        for vehicle in vehicles.values():
            ego_info = vehicle["ego"]
            vehicle_id.append(ego_info["veh_id"])
            ego_state = np.zeros(7)
            ego_state[:2] = ego_info["position"]
            ego_state[2] = ego_info["heading"]  # SUMO degrees (north=0, clockwise)
            ego_state = self.transform_terasim_states_to_cnde(ego_state)
            ego_state[3] = ego_info["velocity"] * np.cos(ego_state[2])
            ego_state[4] = ego_info["velocity"] * np.sin(ego_state[2])
            ego_state[5] = ego_info["acceleration"] * np.cos(ego_state[2])
            ego_state[6] = ego_info["acceleration"] * np.sin(ego_state[2])
            vehicle_states.append(ego_state)
        vehicle_id = np.array(vehicle_id, dtype=object)
        vehicle_states = np.vstack(vehicle_states)

        return vehicle_id, vehicle_states

    def get_predicted_tls_state_and_green_tls_id(self, env_observation, predicted_step=40):
        """Get predicted traffic light states and green light IDs.

        Args:
            env_observation: Environment observation dictionary.
            predicted_step: Number of steps to predict ahead.

        Returns:
            tuple: (mapped_tls_id, mapped_tls_state, mapped_predicted_green_tls)
        """
        def get_cnde_state(tl_states: str | list[str]):
            '''
            tl_states string should be of the same length if it is a list
            return shape: (link_num, time_step, 4)
            '''
            if isinstance(tl_states, str):
                tl_states = [tl_states]
            states = []
            for tl_state in tl_states:
                state = np.zeros((len(tl_state), 4)).astype(np.bool_)
                for i, s in enumerate(tl_state):
                    if s in ["G", "g", "o", "O"]:
                        state[i, 0] = True
                    else:
                        state[i, 2] = True
                states.append(state)
            return states[0] if isinstance(tl_states, str) else np.stack(states, axis=1)

        def get_tls_future_state(current_phase_index, spent_duration, tl_logic, future_step, resolution=0.1, include_current_step=True):
            phase_state_list = [p.state for p in tl_logic.phases]
            phase_duration_list = [p.duration for p in tl_logic.phases]
            ret_state = []
            if include_current_step:
                ret_state.append(phase_state_list[current_phase_index])
                future_step += 1

            while len(ret_state) < future_step:
                remain_time = phase_duration_list[current_phase_index] - spent_duration
                if remain_time >= resolution:
                    ret_state.append(phase_state_list[current_phase_index])
                    spent_duration += resolution
                else:
                    spent_duration = spent_duration + resolution - phase_duration_list[current_phase_index]
                    current_phase_index = (current_phase_index + 1) % len(phase_state_list)
            return ret_state

        tls_observation = env_observation[AgentType.TRAFFIC_LIGHT]

        tls_id_all = []
        tls_id_index = {}
        for tl_node_id, tls_info in tls_observation.items():
            current_time, tls_state, tl_control_link_list, tl_phase, tl_logic, tl_spent_duration = tls_info
            for tl_links in tl_control_link_list:
                for tl_link_triple in tl_links:
                    tl_id = tl_link_triple[2]
                    # Skip empty via lanes - these are pedestrian crossing connections
                    # that don't have a via lane in the SUMO network file.
                    if not tl_id:
                        continue
                    tls_id_all.append(tl_id)
                    tls_id_index[tl_id] = len(tls_id_all) - 1
        assert len(np.unique(tls_id_all)) == len(tls_id_all), "Duplicate traffic light ids"
        tls_state_all = np.zeros((len(tls_id_all), predicted_step+1, 4)).astype(np.bool_)
        for tl_node_id, tls_info in tls_observation.items():
            current_time, tls_state, tl_control_link_list, tl_phase, tl_logic, tl_spent_duration = tls_info
            tls_states_future = get_tls_future_state(tl_phase, tl_spent_duration, tl_logic, predicted_step)
            tls_cnde_states = get_cnde_state(tls_states_future)
            for i, links in enumerate(tl_control_link_list):
                # Filter out empty via lanes (pedestrian crossings) to match the first loop
                tls_lane_id = [link_triple[2] for link_triple in links if link_triple[2]]
                tls_lane_index = [tls_id_index[lane_id] for lane_id in tls_lane_id]
                if tls_lane_index:  # Only update if there are valid lane indices
                    tls_state_all[tls_lane_index] = tls_cnde_states[i:i+1]
        predicted_green_tls = [[] for _ in range(predicted_step)]
        for t in range(predicted_step):
            tl_is_green = tls_state_all[:, t+1, 0]
            tl_green_idx = np.where(tl_is_green)[0]
            for idx in tl_green_idx:
                predicted_green_tls[t].append(tls_id_all[idx])

        # map to new tls id (skip lanes not in id_mapping, e.g. internal junction lanes)
        mapped_tls_id, mapped_tls_state, mapped_predicted_green_tls = [], [], []
        for tl_id, tl_state in zip(tls_id_all, tls_state_all):
            if tl_id not in self.id_mapping:
                continue
            mapped_tls_id.extend(self.id_mapping[tl_id])
            for _ in range(len(self.id_mapping[tl_id])):
                mapped_tls_state.append(tl_state)
        mapped_tls_state = np.stack(mapped_tls_state, axis=0) if mapped_tls_state else np.zeros((0, predicted_step+1, 4), dtype=np.bool_)

        for green_tls in predicted_green_tls:
            mapped_green_tls = []
            for tl in green_tls:
                if tl in self.id_mapping:
                    mapped_green_tls.extend(self.id_mapping[tl])
            mapped_predicted_green_tls.append(mapped_green_tls)

        return mapped_tls_id, mapped_tls_state, mapped_predicted_green_tls

    def cnde_sim_to_control_commands(self, vehicle_id, vehicle_states, env_observation=None):
        """Convert CNDE simulation output to control commands.

        Includes a sanity filter: if CNDE's predicted trajectory first point is too far
        from the vehicle's current SUMO position, the prediction is logged and the vehicle
        falls back to SUMO control (DEFAULT command) for this cycle.

        Args:
            vehicle_id: Array of vehicle IDs.
            vehicle_states: Array of vehicle states from CNDE.
            env_observation: Optional environment observation for logging position mismatch.

        Returns:
            Dict: Control commands for each vehicle.
        """
        cnde_control_commands = Dict()
        sim_time = traci.simulation.getTime()

        for veh_id, veh_state in zip(vehicle_id, vehicle_states):
            terasim_veh_traj = np.zeros((veh_state.shape[0], 5))
            terasim_veh_traj[:, :3] = veh_state[:, :3]
            veh_vel = np.linalg.norm(veh_state[:, 3:5], axis=-1)
            terasim_veh_traj[:, 3] = veh_vel

            # Fix heading from velocity direction: CNDE NN's column 2 (heading)
            # can have wrong sign, but vx/vy (columns 3:5) are correct. For
            # moving vehicles, derive heading from velocity direction instead.
            moving_mask = veh_vel > 0.5  # only override when actually moving
            if np.any(moving_mask):
                vel_heading = np.arctan2(veh_state[moving_mask, 4], veh_state[moving_mask, 3])
                terasim_veh_traj[moving_mask, 2] = vel_heading

            # the following line is not essential, since the time is reset when executing the command
            terasim_veh_traj[:, 4] = np.arange(terasim_veh_traj.shape[0]) * self.nnde_simulator.sim_resolution
            terasim_veh_traj = self.transform_cnde_states_to_terasim(terasim_veh_traj)

            # --- Whole-trajectory backward shift correction ---
            # CNDE predicts from synced history which may lag behind SUMO's
            # current position (up to 0.5s of movement). Instead of fixing
            # each point individually in the controller (which distorts curves),
            # shift the entire trajectory forward along the vehicle's heading.
            try:
                current_pos = traci.vehicle.getPosition(veh_id)
                current_angle_rad = np.deg2rad(90 - traci.vehicle.getAngle(veh_id))
                fwd_x = np.cos(current_angle_rad)
                fwd_y = np.sin(current_angle_rad)
                # Project first trajectory point's displacement onto heading
                dx0 = terasim_veh_traj[0, 0] - current_pos[0]
                dy0 = terasim_veh_traj[0, 1] - current_pos[1]
                proj = dx0 * fwd_x + dy0 * fwd_y
                if proj < -0.05:  # first point is behind current position
                    shift = -proj  # shift forward by this amount
                    terasim_veh_traj[:, 0] += shift * fwd_x
                    terasim_veh_traj[:, 1] += shift * fwd_y
                    logger.debug(
                        f"[CNDE Traj Shift] {veh_id}: shifted entire trajectory "
                        f"forward by {shift:.2f}m (proj={proj:.2f})")
            except Exception as e:
                logger.warning(f"[CNDE Traj Shift] {veh_id}: exception: {e}")

            # --- Sanity filter: check predicted trajectory vs current SUMO position ---
            fallback_to_sumo = False
            filter_reason = None
            try:
                current_pos = traci.vehicle.getPosition(veh_id)
                current_speed = traci.vehicle.getSpeed(veh_id)
                current_angle = traci.vehicle.getAngle(veh_id)
                dt = self.nnde_simulator.sim_resolution

                # Check: max distance from current position to ANY predicted point
                # This catches cases where the entire trajectory drifts far from SUMO
                pred_dists = np.sqrt(
                    (terasim_veh_traj[:, 0] - current_pos[0])**2 +
                    (terasim_veh_traj[:, 1] - current_pos[1])**2
                )
                max_pred_speed = float(np.max(terasim_veh_traj[:, 3]))
                # Total trajectory duration * max speed * generous margin, min 10m
                total_duration = dt * terasim_veh_traj.shape[0]
                max_dist = max(10.0, max(max_pred_speed, current_speed) * total_duration * 3)
                max_pred_dist = float(np.max(pred_dists))

                if max_pred_dist > max_dist:
                    fallback_to_sumo = True
                    filter_reason = "trajectory_drift"
                    self._log_cnde_filter_event(
                        veh_id, sim_time, filter_reason,
                        dist=max_pred_dist, max_dist=max_dist,
                        current_pos=current_pos, current_speed=current_speed, current_angle=current_angle,
                        pred_traj_terasim=terasim_veh_traj, pred_traj_cnde=veh_state,
                    )
            except Exception as e:
                logger.warning(f"[CNDE Filter] {veh_id}: exception during filter check: {e}")

            if fallback_to_sumo:
                cnde_control_commands[veh_id] = NDECommand(
                    command_type=CommandType.DEFAULT,
                )
            else:
                # Duration controls how many steps the controller executes
                # before releasing to SUMO. The full trajectory is passed so
                # all steps are available, but only CNDE_EXECUTE_STEPS are used.
                execute_steps = min(self.CNDE_EXECUTE_STEPS, veh_state.shape[0])
                cnde_control_commands[veh_id] = NDECommand(
                    command_type=CommandType.CNDE_TRAJECTORY,
                    duration=self.nnde_simulator.sim_resolution * execute_steps,
                    time_resolution=self.nnde_simulator.sim_resolution,
                    future_trajectory=terasim_veh_traj,
                    keep_route_mode=2,
                )

        return cnde_control_commands

    def _log_cnde_filter_event(self, veh_id, sim_time, reason, **kwargs):
        """Log a CNDE filter event to outputs/cnde_filter_log.jsonl."""
        if not hasattr(self, '_cnde_filter_log_file'):
            log_path = Path("outputs") / "cnde_filter_log.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._cnde_filter_log_file = open(log_path, "w")

        record = {
            "sim_time": round(sim_time, 3),
            "vehicle_id": veh_id,
            "reason": reason,
        }

        if "dist" in kwargs:
            record["dist"] = round(kwargs["dist"], 3)
            record["max_dist"] = round(kwargs["max_dist"], 3)
        if "current_pos" in kwargs:
            pos = kwargs["current_pos"]
            record["current_x"] = round(pos[0], 3)
            record["current_y"] = round(pos[1], 3)
            record["current_speed"] = round(kwargs.get("current_speed", 0), 3)
            record["current_angle"] = round(kwargs.get("current_angle", 0), 3)
        if "pred_traj_terasim" in kwargs:
            traj = kwargs["pred_traj_terasim"]
            record["pred_traj_terasim"] = np.round(traj, 3).tolist()
        if "pred_traj_cnde" in kwargs:
            traj_cnde = kwargs["pred_traj_cnde"]
            record["pred_traj_cnde"] = np.round(traj_cnde, 3).tolist()

        logger.warning(
            f"[CNDE Filter] {veh_id}: {reason}, dist={kwargs.get('dist', '?'):.1f}m > "
            f"max={kwargs.get('max_dist', '?'):.1f}m, fallback to SUMO"
        )
        self._cnde_filter_log_file.write(json.dumps(record) + "\n")
        self._cnde_filter_log_file.flush()

    def get_default_control_commands(self, vehicle_id):
        """Get default control commands for vehicles.

        Args:
            vehicle_id: Array of vehicle IDs.

        Returns:
            Dict: Default control commands for each vehicle.
        """
        control_commands = Dict()
        for veh_id in vehicle_id:
            control_commands[veh_id] = NDECommand(
                command_type=CommandType.DEFAULT,
            )
        return control_commands

    def transform_cnde_states_to_terasim(self, cnde_states: np.ndarray):
        """Transform the normalized position to the terasim position & heading to sumo heading.

        Args:
            cnde_states: States in CNDE format.

        Returns:
            np.ndarray: States in TeraSim format.
        """
        terasim_states = self.nnde_simulator.map.denormalize(cnde_states, dim=[0, 1])
        degrees = np.rad2deg(cnde_states[..., 2])
        degrees = (degrees + 360) % 360
        degrees = (90 - degrees) % 360
        terasim_states[..., 2] = degrees
        return terasim_states

    def transform_terasim_states_to_cnde(self, terasim_states: np.ndarray):
        """Transform the terasim position to normalized cnde position & sumo heading to cnde heading.

        Args:
            terasim_states: States in TeraSim format.

        Returns:
            np.ndarray: States in CNDE format.
        """
        cnde_states = self.nnde_simulator.map.normalize(terasim_states, dim=[0, 1])
        radians = np.deg2rad(90 - terasim_states[..., 2])
        radians = np.arctan2(np.sin(radians), np.cos(radians))
        cnde_states[..., 2] = radians
        return cnde_states

    def convert_terasim_history_to_cnde(self, terasim_history: np.ndarray) -> np.ndarray:
        """Convert TeraSim history array to CNDE state format.

        TeraSim format: [time, x, y, velocity, heading, acceleration, length, width, height]
        CNDE format: [x, y, heading, vx, vy, ax, ay]

        Args:
            terasim_history: History array in TeraSim format, shape (N, 9)

        Returns:
            np.ndarray: History in CNDE format, shape (N, 7)
        """
        if terasim_history is None or len(terasim_history) == 0:
            return None

        n_steps = terasim_history.shape[0]
        cnde_history = np.zeros((n_steps, 7))

        # Extract fields from TeraSim format
        # [time, x, y, velocity, heading, acceleration, length, width, height]
        x = terasim_history[:, 1]
        y = terasim_history[:, 2]
        velocity = terasim_history[:, 3]
        heading_sumo = terasim_history[:, 4]  # SUMO heading (degrees, north=0, clockwise)
        acceleration = terasim_history[:, 5]

        # Convert SUMO heading to CNDE heading (radians, math convention)
        # SUMO: degrees, 0=north, clockwise
        # CNDE: radians, 0=east, counter-clockwise
        heading_rad = np.deg2rad(90 - heading_sumo)
        heading_rad = np.arctan2(np.sin(heading_rad), np.cos(heading_rad))

        # Compute velocity components
        vx = velocity * np.cos(heading_rad)
        vy = velocity * np.sin(heading_rad)

        # Compute acceleration components
        ax = acceleration * np.cos(heading_rad)
        ay = acceleration * np.sin(heading_rad)

        # Fill CNDE history
        cnde_history[:, 0] = x
        cnde_history[:, 1] = y
        cnde_history[:, 2] = heading_rad
        cnde_history[:, 3] = vx
        cnde_history[:, 4] = vy
        cnde_history[:, 5] = ax
        cnde_history[:, 6] = ay

        # Check for NaN/Inf values that could cause issues
        if np.any(np.isnan(cnde_history)) or np.any(np.isinf(cnde_history)):
            logger.warning(f"convert_terasim_history_to_cnde: NaN or Inf values detected, skipping")
            return None

        # Manually normalize positions (subtract map center offset)
        # We do this manually instead of calling map.normalize() to avoid potential segfault
        # CNDE uses relative coordinates centered at pos_offset, TeraSim uses absolute coordinates
        if hasattr(self.nnde_simulator, 'map') and hasattr(self.nnde_simulator.map, 'pos_offset'):
            pos_offset = self.nnde_simulator.map.pos_offset
            cnde_history[:, 0] -= pos_offset[0]  # x
            cnde_history[:, 1] -= pos_offset[1]  # y

        return cnde_history

    @profile
    def sync_terasim_history_to_cnde(self, sync_vehicle_ids: set):
        """Sync TeraSim vehicle history to CNDE agent_record.

        This ensures CNDE uses accurate real-time positions instead of accumulated drift
        from its own predictions. Should be called with cache_radius vehicles (large circle)
        so that vehicles are synced before they enter control_radius.

        If a vehicle's position drift exceeds LARGE_DRIFT_THRESHOLD, its history is fully
        reset and it must accumulate fresh history before CNDE generates control commands.

        Args:
            sync_vehicle_ids: Set of vehicle IDs to sync (typically cache_radius vehicles)
        """
        import sys

        if not sync_vehicle_ids:
            logger.debug("sync_terasim_history_to_cnde: No vehicles to sync")
            return

        # sync_sim_time = traci.simulation.getTime()
        # print(f"[COMPARE TIME] sync_terasim_history_to_cnde called at sim_time={sync_sim_time:.2f}", flush=True)

        history_step = self.nnde_simulator.history_step
        synced_count = 0
        skip_reasons = {}

        # Get pos_offset for coordinate validation
        pos_offset = None
        if hasattr(self.nnde_simulator, 'map') and hasattr(self.nnde_simulator.map, 'pos_offset'):
            pos_offset = self.nnde_simulator.map.pos_offset
            # Log pos_offset once at the start for debugging
            if not hasattr(self, '_pos_offset_logged'):
                # print(f"[SYNC] Using pos_offset = ({pos_offset[0]:.2f}, {pos_offset[1]:.2f})", flush=True)
                self._pos_offset_logged = True

        for veh_id in sync_vehicle_ids:
            # print(f"[SYNC DEBUG] Processing vehicle: {veh_id}", flush=True)
            # sys.stdout.flush()

            # Check if vehicle exists in TeraSim's vehicle_list
            if veh_id not in self.vehicle_list:
                skip_reasons[veh_id] = "not in vehicle_list"
                continue

            vehicle = self.vehicle_list[veh_id]

            # Get the ego sensor which has history
            # vehicle.sensors is a dict like {'ego': <NDEEgoSensor object>}
            if not vehicle.sensors or 'ego' not in vehicle.sensors:
                skip_reasons[veh_id] = "no ego sensor"
                continue

            ego_sensor = vehicle.sensors['ego']

            # Check if sensor has history caching enabled
            if not hasattr(ego_sensor, 'cache_history') or not ego_sensor.cache_history:
                skip_reasons[veh_id] = "history caching not enabled"
                continue

            # NOTE: Sensor update is now handled in NADE_decision_and_control() (Option B)
            # before calling nnde_make_decisions(), so history should already have current time

            # Get history array from sensor with padding support
            try:
                terasim_history = ego_sensor.get_partial_history_with_padding(history_step)
            except Exception as e:
                skip_reasons[veh_id] = f"get_partial_history_with_padding exception: {e}"
                continue

            if terasim_history is None:
                skip_reasons[veh_id] = "no history yet (vehicle just spawned)"
                continue

            # === DEBUG: Check time synchronization ===
            # CNDE needs 11 points: T-1.0, T-0.9, ..., T-0.1, T
            current_sumo_time = traci.simulation.getTime()
            history_times_in_array = terasim_history[:, 0]  # All times in history array
            history_last_time = history_times_in_array[-1]
            history_first_time = history_times_in_array[0]
            # Only print when history time is significantly stale (>2s off)
            time_diff = abs(current_sumo_time - history_last_time)
            if time_diff > 2.0:
                pass  # History stale by {time_diff}s — logged at debug level only

            # Convert TeraSim history to CNDE format
            cnde_history = self.convert_terasim_history_to_cnde(terasim_history)
            if cnde_history is None:
                skip_reasons[veh_id] = "convert_terasim_history_to_cnde returned None"
                continue

            # === FRESH SUMO DATA: Ensure current time point uses real SUMO position ===
            # Fetch current position directly from SUMO (not from potentially stale history)
            try:
                fresh_pos = traci.vehicle.getPosition(veh_id)
                fresh_heading = traci.vehicle.getAngle(veh_id)
                fresh_speed = traci.vehicle.getSpeed(veh_id)
                fresh_accel = traci.vehicle.getAcceleration(veh_id)

                # Convert SUMO heading to CNDE heading (radians, math convention)
                fresh_heading_cnde = np.deg2rad(90 - fresh_heading)

                # Get vehicle dimensions (cached in sensor)
                fresh_length = ego_sensor.length
                fresh_width = ego_sensor.width

                # Build fresh current state in TeraSim format
                # TeraSim history format: [time, x, y, vel, heading, accel, length, width, height]
                # CNDE history format after conversion: [x, y, heading_rad, vel, length, width]
                fresh_x = fresh_pos[0]
                fresh_y = fresh_pos[1]

                # Apply pos_offset for CNDE coordinates
                if pos_offset is not None:
                    fresh_x -= pos_offset[0]
                    fresh_y -= pos_offset[1]

                # Replace the last point of cnde_history with fresh SUMO data
                # CNDE format: [x, y, heading, vx, vy, ax, ay]
                old_last_pos = cnde_history[-1, :2].copy()
                cnde_history[-1, 0] = fresh_x
                cnde_history[-1, 1] = fresh_y
                cnde_history[-1, 2] = fresh_heading_cnde
                # Compute velocity components from speed and heading
                cnde_history[-1, 3] = fresh_speed * np.cos(fresh_heading_cnde)  # vx
                cnde_history[-1, 4] = fresh_speed * np.sin(fresh_heading_cnde)  # vy
                # Compute acceleration components
                cnde_history[-1, 5] = fresh_accel * np.cos(fresh_heading_cnde)  # ax
                cnde_history[-1, 6] = fresh_accel * np.sin(fresh_heading_cnde)  # ay

                pos_refresh_diff = np.sqrt((fresh_x - old_last_pos[0])**2 + (fresh_y - old_last_pos[1])**2)
                if pos_refresh_diff > 0.01:  # Only log large discrepancies
                    pass

            except Exception as e:
                pass  # Fresh fetch failed, continue with history data

            # Check if vehicle exists in CNDE agent_record (O(1) dict lookup)
            if veh_id not in self.nnde_simulator.agent_record._id_agent_mapping:
                skip_reasons[veh_id] = "not in CNDE agent_record"
                continue

            # Get the agent and update its history
            agent = self.nnde_simulator.agent_record.get_agents(veh_id)
            if agent is None:
                skip_reasons[veh_id] = "agent is None"
                continue

            agent_states_len = len(agent.states)
            n_history = len(cnde_history)

            # Log before/after positions for debugging
            old_pos = agent.states[-1, :2].copy() if agent_states_len > 0 else None
            new_pos = cnde_history[-1, :2].copy() if n_history > 0 else None
            new_state = cnde_history[-1].copy() if n_history > 0 else None

            # Print CNDE agent's current state
            # if old_pos is not None:
            #     print(f"[SYNC DEBUG] {veh_id} CNDE agent current pos: x={old_pos[0]:.2f}, y={old_pos[1]:.2f}", flush=True)

            # Validate new position
            if new_pos is not None:
                # CNDE relative coords should typically be within reasonable bounds
                if np.abs(new_pos[0]) > 5000 or np.abs(new_pos[1]) > 5000:
                    # print(f"[SYNC DEBUG] WARNING: {veh_id} position too large for relative coords: {new_pos}", flush=True)
                    skip_reasons[veh_id] = f"position too large: {new_pos}"
                    continue

                # Check for NaN/Inf
                if np.any(np.isnan(new_state)) or np.any(np.isinf(new_state)):
                    # print(f"[SYNC DEBUG] WARNING: {veh_id} state contains NaN/Inf!", flush=True)
                    skip_reasons[veh_id] = "NaN/Inf in new state"
                    continue

            # Calculate position difference
            LARGE_DRIFT_THRESHOLD = 10.0  # meters - treat as "reentry" if drift exceeds this
            if old_pos is not None and new_pos is not None:
                pos_diff = np.linalg.norm(old_pos - new_pos)

                # Log vehicles with significant drift (> 5m)
                if pos_diff > 5:
                    pass  # Significant drift, logged via logger.info below

                # FIX: If old_pos is at/near origin (0,0), this is zero-fill contamination
                # from proceed_step(). The history is corrupted (mix of zeros and real data),
                # so we must reset it entirely — same as large drift.
                # The old "skip reset" approach avoided a reset→zero-fill→reset loop, but
                # that loop is now prevented by proceed_step() itself (history_since_spawn < 15
                # skips zero-filling for recently reset vehicles). So resetting is safe.
                ZERO_FILL_THRESHOLD = 1.0  # meters from origin
                if np.linalg.norm(old_pos) < ZERO_FILL_THRESHOLD:
                    # CNDE cycles every 5 TeraSim steps, so we need enough cycles
                    # for real sync data to replace the tiled static history.
                    # Setting spawn_time_step forward by history_step ensures the vehicle
                    # needs 2*history_step - 1 total steps (~2.1s = ~4 CNDE cycles)
                    # before get_complete_history_active_agent_ids includes it.
                    logger.warning(
                        f"[SYNC RESET] {veh_id}: old_pos near origin ({old_pos[0]:.2f}, {old_pos[1]:.2f}), "
                        f"contamination — resetting history with fresh SUMO state")
                    # print(f"[SYNC RESET] {veh_id}: contamination, resetting history", flush=True)

                    # Build fresh history from current SUMO state
                    fresh_state = cnde_history[-1].copy()
                    fresh_history = np.tile(fresh_state[None, :], (history_step, 1))

                    self.nnde_simulator.agent_record.reset_agent_history(veh_id, fresh_history)
                    # No spawn_time_step delay: tiled history provides 11 valid steps.
                    # With last-known-state filling (not zeros), contamination loops
                    # are no longer possible.

                    synced_count += 1
                    # logger.info(f"Synced (zero-fill reset) {veh_id}: diff={pos_diff:.2f}m")
                    continue

                # Large drift: reset entire history like a reentry vehicle
                # Same extended wait as zero-fill to ensure enough real sync data
                elif pos_diff > LARGE_DRIFT_THRESHOLD:
                    logger.warning(
                        f"[SYNC RESET] {veh_id}: drift={pos_diff:.2f}m > {LARGE_DRIFT_THRESHOLD}m, "
                        f"resetting history with fresh SUMO state")
                    # print(f"[SYNC RESET] {veh_id}: drift={pos_diff:.2f}m, resetting history", flush=True)

                    # Build fresh history by tiling current SUMO state
                    fresh_state = cnde_history[-1].copy()
                    fresh_history = np.tile(fresh_state[None, :], (history_step, 1))

                    self.nnde_simulator.agent_record.reset_agent_history(veh_id, fresh_history)
                    # No spawn_time_step delay: tiled history is valid for NN.

                    synced_count += 1
                    # logger.info(f"Synced (reset) {veh_id}: diff={pos_diff:.2f}m")
                    continue

            # Normal sync: update ALL available history steps with real TeraSim data
            # This ensures CNDE history doesn't contain stale predictions from previous
            # cycles, especially for vehicles whose CNDE predictions were not executed by SUMO
            if agent_states_len > 0 and n_history > 0:
                # Write as many steps as we have, aligned to the end of agent states
                n_write = min(n_history, agent_states_len)
                old_last = agent._states_per_step.states[-1, :2].copy()
                agent._states_per_step.states[-n_write:] = cnde_history[-n_write:].copy()
                new_last = agent._states_per_step.states[-1, :2].copy()

                # Only log large position jumps during sync write
                write_diff = np.linalg.norm(new_last - old_last)
                if write_diff > 5.0:
                    pass  # Large sync jump, normal after drift reset

            synced_count += 1
            # if old_pos is not None and new_pos is not None:
            #     logger.info(f"Synced {veh_id}: diff={pos_diff:.2f}m")

        if skip_reasons:
            logger.debug(f"Sync skipped for: {skip_reasons}")
            # print(f"[SYNC DEBUG] Skip reasons: {skip_reasons}", flush=True)

        # logger.info(f"sync_terasim_history_to_cnde: Synced {synced_count}/{len(sync_vehicle_ids)} vehicles")
        # print(f"[SYNC DEBUG] Sync complete: {synced_count}/{len(sync_vehicle_ids)}", flush=True)

    def sync_terasim_history_to_cnde_debug(self, controlled_vehicle_ids: set):
        """Debug version of sync - tests each step to find segfault cause."""
        import sys

        if not controlled_vehicle_ids:
            return

        history_step = self.nnde_simulator.history_step
        # print(f"[DEBUG SYNC] Starting with {len(controlled_vehicle_ids)} vehicles, history_step={history_step}", flush=True)

        for veh_id in controlled_vehicle_ids:
            if veh_id == "AV":
                continue

            # print(f"[DEBUG SYNC] Processing {veh_id}...", flush=True)

            # Step 1: Check vehicle exists
            if veh_id not in self.vehicle_list:
                # print(f"[DEBUG SYNC] {veh_id} not in vehicle_list", flush=True)
                continue
            # print(f"[DEBUG SYNC] Step 1 OK: vehicle in list", flush=True)

            vehicle = self.vehicle_list[veh_id]

            # Step 2: Check sensor
            if not vehicle.sensors or 'ego' not in vehicle.sensors:
                # print(f"[DEBUG SYNC] {veh_id} no ego sensor", flush=True)
                continue
            # print(f"[DEBUG SYNC] Step 2 OK: has ego sensor", flush=True)

            ego_sensor = vehicle.sensors['ego']

            # Step 3: Check cache_history
            if not hasattr(ego_sensor, 'cache_history') or not ego_sensor.cache_history:
                # print(f"[DEBUG SYNC] {veh_id} cache_history not enabled", flush=True)
                continue
            # print(f"[DEBUG SYNC] Step 3 OK: cache_history enabled", flush=True)

            # Step 4: Get history
            # print(f"[DEBUG SYNC] Step 4: Getting history...", flush=True)
            sys.stdout.flush()
            try:
                terasim_history = ego_sensor.get_partial_history_with_padding(history_step)
            except Exception as e:
                # print(f"[DEBUG SYNC] Step 4 FAILED: {e}", flush=True)
                continue
            # print(f"[DEBUG SYNC] Step 4 OK: got history shape={terasim_history.shape if terasim_history is not None else None}", flush=True)

            if terasim_history is None:
                continue

            # Step 5: Convert to CNDE format (without normalize)
            # print(f"[DEBUG SYNC] Step 5: Converting format (no normalize)...", flush=True)
            sys.stdout.flush()
            try:
                n_steps = terasim_history.shape[0]
                cnde_history = np.zeros((n_steps, 7))
                x = terasim_history[:, 1]
                y = terasim_history[:, 2]
                velocity = terasim_history[:, 3]
                heading_sumo = terasim_history[:, 4]
                acceleration = terasim_history[:, 5]
                heading_rad = np.deg2rad(90 - heading_sumo)
                heading_rad = np.arctan2(np.sin(heading_rad), np.cos(heading_rad))
                cnde_history[:, 0] = x
                cnde_history[:, 1] = y
                cnde_history[:, 2] = heading_rad
                cnde_history[:, 3] = velocity * np.cos(heading_rad)
                cnde_history[:, 4] = velocity * np.sin(heading_rad)
                cnde_history[:, 5] = acceleration * np.cos(heading_rad)
                cnde_history[:, 6] = acceleration * np.sin(heading_rad)
            except Exception as e:
                # print(f"[DEBUG SYNC] Step 5 FAILED: {e}", flush=True)
                continue
            # print(f"[DEBUG SYNC] Step 5 OK: converted shape={cnde_history.shape}", flush=True)

            # Step 6: Normalize (this might be the problem)
            # print(f"[DEBUG SYNC] Step 6: Normalizing positions...", flush=True)
            sys.stdout.flush()
            try:
                cnde_history = self.nnde_simulator.map.normalize(cnde_history, dim=[0, 1])
            except Exception as e:
                # print(f"[DEBUG SYNC] Step 6 FAILED: {e}", flush=True)
                continue
            # print(f"[DEBUG SYNC] Step 6 OK: normalized", flush=True)

            # Step 7: Check CNDE agent exists
            # print(f"[DEBUG SYNC] Step 7: Checking CNDE agent...", flush=True)
            sys.stdout.flush()
            if veh_id not in self.nnde_simulator.agent_record.all_recorded_agent_ids:
                # print(f"[DEBUG SYNC] {veh_id} not in CNDE agent_record", flush=True)
                continue
            # print(f"[DEBUG SYNC] Step 7 OK: agent in CNDE record", flush=True)

            # Step 8: Get agent
            # print(f"[DEBUG SYNC] Step 8: Getting agent...", flush=True)
            sys.stdout.flush()
            try:
                agent = self.nnde_simulator.agent_record.get_agents(veh_id)
            except Exception as e:
                # print(f"[DEBUG SYNC] Step 8 FAILED: {e}", flush=True)
                continue
            if agent is None:
                # print(f"[DEBUG SYNC] Step 8: agent is None", flush=True)
                continue
            # print(f"[DEBUG SYNC] Step 8 OK: got agent, states shape={agent.states.shape}", flush=True)

            # Step 9: Update agent states (this might cause issues)
            # print(f"[DEBUG SYNC] Step 9: Updating agent states...", flush=True)
            sys.stdout.flush()
            try:
                agent_states_len = len(agent.states)
                n_history = len(cnde_history)
                end_idx = agent_states_len
                start_idx = max(0, agent_states_len - n_history)
                n_update = end_idx - start_idx

                if n_update > 0 and n_history > 0:
                    history_start_idx = max(0, n_history - n_update)
                    agent._states_per_step.states[start_idx:end_idx] = cnde_history[history_start_idx:].copy()
            except Exception as e:
                # print(f"[DEBUG SYNC] Step 9 FAILED: {e}", flush=True)
                continue
            # print(f"[DEBUG SYNC] Step 9 OK: updated {n_update} steps", flush=True)

        # print(f"[DEBUG SYNC] Completed all vehicles", flush=True)

    @profile
    def nnde_make_decisions(self, env_observation, controlled_vehicle_ids=None, cache_vehicle_ids=None):
        """Make decisions using the neural network.

        Args:
            env_observation: Environment observation dictionary.
            controlled_vehicle_ids: Vehicle IDs within control_radius (small circle) for control filtering.
            cache_vehicle_ids: Vehicle IDs within cache_radius (large circle) for history sync.
                              If None, falls back to controlled_vehicle_ids.

        Returns:
            Dict: Control commands from neural network decisions.
        """
        # Clean up CNDE references to vehicles that left SUMO or left cache_radius.
        # This prevents zero-fill contamination: vehicles outside cache_radius
        # won't be synced, so proceed_step() would zero-fill their history,
        # corrupting it and causing segfaults when they re-enter.
        # Instead, remove them entirely. They'll be re-added as new vehicles
        # if they re-enter cache_radius.
        _t_start = time.perf_counter()

        sumo_veh_ids = set(traci.vehicle.getIDList())
        cache_veh_ids = set(cache_vehicle_ids) if cache_vehicle_ids is not None else sumo_veh_ids
        cnde_tracked_ids = list(self.nnde_simulator.agent_record.current_track_agent_ids)

        # Remove vehicles that left SUMO entirely
        departed_ids = [vid for vid in cnde_tracked_ids if vid not in sumo_veh_ids]
        # Remove vehicles that left cache_radius (still in SUMO but too far from AV)
        out_of_cache_ids = [vid for vid in cnde_tracked_ids
                           if vid in sumo_veh_ids and vid not in cache_veh_ids]
        remove_ids = departed_ids + out_of_cache_ids

        if remove_ids:
            self.nnde_simulator.agent_record.force_remove_agents(remove_ids)
            if hasattr(self, 'cnde_traj_logger'):
                active = getattr(self.cnde_traj_logger, '_active_vehicles', set())
                active -= set(remove_ids)
            if departed_ids:
                logger.info(f"[CNDE Cleanup] Removed {len(departed_ids)} departed vehicles")
            if out_of_cache_ids:
                logger.info(f"[CNDE Cleanup] Removed {len(out_of_cache_ids)} out-of-cache vehicles")

        _t_cleanup = time.perf_counter()

        vehicle_id, vehicle_states = self.get_cnde_vehicle_state(env_observation)
        _t_get_state = time.perf_counter()
        cnde_control_commands = Dict()


        tls_id, tls_states, __ = self.get_predicted_tls_state_and_green_tls_id(env_observation, predicted_step=0)
        # update the last step of scenario in terasim to cnde
        if self.nnde_simulator.current_step < 0:
            self.nnde_simulator.init_traffic_light(tls_id, tls_states)
            # Get TeraSim root directory
            TERASIM_ROOT = Path(__file__).parent.parent.parent.parent.parent
            save_file = str(TERASIM_ROOT / f"outputs/temp_res/{self.nnde_simulator.desc}.pkl")
            self.nnde_simulator.update({"vehicle_id": vehicle_id, "vehicle_states": vehicle_states[:, None, :]},
                                        save_file=save_file)
        elif self.nnde_simulator.current_step < 10:
            existing_vehicle_mask = np.isin(vehicle_id, self.nnde_simulator.agent_record.get_valid_agent_ids(timestep=-1))
            existing_vehicle_id = np.array(vehicle_id, dtype=object)[existing_vehicle_mask].tolist()
            _, current_vehicle_states, _ = self.nnde_simulator.agent_record[-1, existing_vehicle_id, :7]
            existing_vehicle_new_states = vehicle_states[existing_vehicle_mask]
            existing_vehicle_pos_diff = existing_vehicle_new_states[:, :2] - current_vehicle_states[:, :2]
            existing_vehicle_new_states[:, 2] = np.arctan2(existing_vehicle_pos_diff[:, 1], existing_vehicle_pos_diff[:, 0])
            existing_vehicle_new_states[:, 3:5] = existing_vehicle_pos_diff[:, 0:2] / self.nnde_simulator.sim_resolution
            stopped_vehicle_mask = np.linalg.norm(existing_vehicle_new_states[:, 3:5], axis=-1) < 0.1
            existing_vehicle_new_states[stopped_vehicle_mask][:, 2] = current_vehicle_states[stopped_vehicle_mask][:, 2]
            existing_vehicle_vel_diff = existing_vehicle_new_states[:, 3:5] - current_vehicle_states[:, 3:5]

            existing_vehicle_new_states[:, 5:7] = np.clip(existing_vehicle_vel_diff / self.nnde_simulator.sim_resolution, -5, 5)
            vehicle_states[existing_vehicle_mask] = existing_vehicle_new_states
            # Get TeraSim root directory
            TERASIM_ROOT = Path(__file__).parent.parent.parent.parent.parent
            save_file = str(TERASIM_ROOT / f"outputs/temp_res/{self.nnde_simulator.desc}.pkl")
            self.nnde_simulator.update({"vehicle_id": vehicle_id, "vehicle_states": vehicle_states[:, None, :],
                                        "dynamic_polyline_id": tls_id, "dynamic_polyline_states": tls_states},
                                        save_file=save_file)
        else:
            # self.nnde_update_tag = (self.nnde_update_tag + 1) % 5
            # print('nnde is working')
            if self.nnde_update_tag % 5 == 1:
                tls_id, tls_states, predicted_green_tls = self.get_predicted_tls_state_and_green_tls_id(env_observation)

                # Sync TeraSim history to CNDE for vehicles within cache_radius (large circle)
                # This fixes the position drift issue by using real TeraSim positions
                # We sync all vehicles in cache_radius so that when they enter control_radius,
                # CNDE already has accurate history data
                # Use cache_vehicle_ids (large circle) for sync, NOT controlled_vehicle_ids (small circle)
                sync_vehicle_ids = cache_vehicle_ids if cache_vehicle_ids is not None else (controlled_vehicle_ids if controlled_vehicle_ids is not None else set(vehicle_id.tolist()))
                _t_pre_sync = time.perf_counter()
                if sync_vehicle_ids:
                    self.sync_terasim_history_to_cnde(sync_vehicle_ids)
                _t_post_sync = time.perf_counter()

                # Sync vehicles from TeraSim to CNDE agent_record before stepping
                # Handle three cases:
                # 1. Brand new vehicles: not in all_recorded_agent_ids -> add_agents_from_history_states
                # 2. Reentry vehicles: in all_recorded but not in current_track -> reset_agent_history
                # 3. Existing vehicles: already tracked -> handled by sync_terasim_history_to_cnde
                # Use dict directly (O(1) lookup) instead of creating a list from all_recorded_agent_ids
                cnde_id_mapping = self.nnde_simulator.agent_record._id_agent_mapping
                current_cnde_vehicle_ids = self.nnde_simulator.agent_record.current_track_agent_ids
                history_step = self.nnde_simulator.history_step
                cnde_current_step = self.nnde_simulator.scenario_manager.current_step

                # Only add vehicles within cache_radius to CNDE.
                # Without this filter, ALL observation vehicles (~1139) would be added
                # every cycle, then force-removed next cycle — a 1039-vehicle oscillation
                # causing O(N²) work in force_remove_agents scanning _agents_id_per_step.
                if cache_vehicle_ids is not None:
                    cache_mask = np.isin(vehicle_id, list(cache_veh_ids))
                    tracking_vehicle_id = vehicle_id[cache_mask]
                    tracking_vehicle_states = vehicle_states[cache_mask]
                else:
                    tracking_vehicle_id = vehicle_id
                    tracking_vehicle_states = vehicle_states

                new_vehicle_mask = ~np.isin(tracking_vehicle_id, list(cnde_id_mapping.keys()))
                if np.any(new_vehicle_mask):
                    new_vehicle_ids = np.array(tracking_vehicle_id, dtype=object)[new_vehicle_mask].tolist()
                    new_vehicle_states = tracking_vehicle_states[new_vehicle_mask]
                    new_vehicle_history = np.tile(new_vehicle_states[:, None, :], (1, history_step, 1))

                    for i, new_vid in enumerate(new_vehicle_ids):
                        self.nnde_simulator.agent_record.add_agents_from_history_states(
                            vehicles_id=new_vid,
                            vehicles_states=new_vehicle_history[i],
                            vehicles_static=np.array([3.8, 1.8, 1.5])
                        )

                    logger.info(f'Added {len(new_vehicle_ids)} new vehicles to CNDE (eligible after {history_step}-step history check)')

                # Remove vehicles that left TeraSim or fell outside cache from CNDE tracking.
                # Use force_remove_agents (not delete_id_agents) so _id_agent_mapping stays bounded.
                # Order-preserving difference (not list(set - set)): set order
                # follows the per-process string hash seed and would make the
                # removal order -- hence the run -- irreproducible across processes.
                _kept_vehicle_ids = set(vehicle_id.tolist())
                removed_vehicle_ids = [vid for vid in current_cnde_vehicle_ids if vid not in _kept_vehicle_ids]
                if len(removed_vehicle_ids) > 0:
                    self.nnde_simulator.agent_record.force_remove_agents(removed_vehicle_ids)
                    logger.info(f'Removed {len(removed_vehicle_ids)} vehicles from CNDE tracking')

                _t_post_new_remove = time.perf_counter()

                # Update traffic light states
                self.nnde_simulator.scenario_manager.vector_map.formatted_update(
                    {"dynamic_polyline_id": tls_id, "dynamic_polyline_states": tls_states}, proceed=False)

                # Predict only the control candidates when the env asks for it (every other
                # vehicle stays in the scene as context); the default predicts every vehicle.
                center_agent_ids = None
                if getattr(self, "cnde_predict_control_only", False):
                    center_agent_ids = list(self.control_ls.get('cnde_control_ids', []))
                nnde_output_scenario = self.nnde_simulator.step(predicted_green_tls=predicted_green_tls,
                                                                center_agent_ids=center_agent_ids)

                # Debug: Log which vehicles are excluded due to incomplete history
                # CNDE only outputs trajectories for vehicles with complete history (11 steps)
                output_veh_ids = nnde_output_scenario["vehicle_id"]
                output_veh_ids_set = set(output_veh_ids)

                # Compare with TeraSim's cnde_control_ids (vehicles that passed TeraSim filter)
                terasim_cnde_control_ids = set(self.control_ls.get('cnde_control_ids', []))

                # Vehicles that passed TeraSim filter but were excluded by CNDE history filter
                excluded_by_cnde_history = terasim_cnde_control_ids - output_veh_ids_set
                if excluded_by_cnde_history:
                    # Log why each vehicle was excluded
                    history_step = self.nnde_simulator.history_step
                    current_step = self.nnde_simulator.scenario_manager.current_step
                    for veh_id in excluded_by_cnde_history:
                        agent = self.nnde_simulator.agent_record.get_agents(veh_id)
                        if agent is not None:
                            first_valid = agent.first_valid_step
                            required = first_valid + history_step - 1
                            print(f"[CNDE HISTORY FILTER] {veh_id}: excluded, "
                                  f"first_valid_step={first_valid}, "
                                  f"requires {required} <= current_step({current_step}), "
                                  f"need {required - current_step} more steps", flush=True)
                        else:
                            print(f"[CNDE HISTORY FILTER] {veh_id}: excluded, agent not found in CNDE", flush=True)
                    logger.info(f"[CNDE History Filter] {len(excluded_by_cnde_history)} vehicles excluded: "
                               f"TeraSim passed {len(terasim_cnde_control_ids)}, CNDE outputs {len(output_veh_ids)}")

                # Pass env_observation to log CNDE vs TeraSim position mismatch
                # IMPORTANT: Compare BEFORE update() so we read CNDE's synced state (at T)
                # instead of the predicted state (at T+dt) that update() would write
                # Filter env_observation to only include vehicles from control_radius (small circle)
                # This ensures control commands are only generated for vehicles that will be controlled
                if controlled_vehicle_ids is not None and AgentType.VEHICLE in env_observation:
                    filtered_env_observation = {
                        agent_type: (
                            {
                                agent_id: obs
                                for agent_id, obs in env_observation[agent_type].items()
                                if agent_id in controlled_vehicle_ids
                            }
                            if agent_type == AgentType.VEHICLE
                            else env_observation[agent_type]
                        )
                        for agent_type in env_observation
                    }
                else:
                    filtered_env_observation = env_observation

                # nn_controL_commands outputs the command for all cache raduis
                nn_control_commands = self.cnde_sim_to_control_commands(
                    nnde_output_scenario["vehicle_id"],
                    nnde_output_scenario["vehicle_states"],
                    env_observation=env_observation
                )
                logger.info(f'nnde is working on {len(nn_control_commands)} vehicles')

                # Log CNDE predicted trajectories for verification
                sim_time = traci.simulation.getTime()
                pred_trajs = {}
                for veh_id, cmd in nn_control_commands.items():
                    if hasattr(cmd, 'future_trajectory') and cmd.future_trajectory is not None:
                        pred_trajs[veh_id] = cmd.future_trajectory
                self.cnde_traj_logger.log_prediction(sim_time, None, pred_trajs)

                # Get TeraSim root directory
                TERASIM_ROOT = Path(__file__).parent.parent.parent.parent.parent
                save_file = str(TERASIM_ROOT / f"outputs/temp_res/{self.nnde_simulator.desc}.pkl")
                self.nnde_simulator.update(nnde_output_scenario, save_file=save_file)

                # 0127 2026 YW: Cross-validate with cnde_control_ids from BaseEnvComplete
                # Only return commands for vehicles that passed the filter in base_complete.py
                # (excludes: AV, vehicles in junction, low speed vehicles, first in signal queue)
                cnde_control_ids = set(self.control_ls.get('cnde_control_ids', []))

                # Debug: Log vehicles in nn_control_commands but not in cnde_control_ids
                nn_control_ids = set(nn_control_commands.keys())
                in_nn_not_in_cnde = nn_control_ids - cnde_control_ids
                if in_nn_not_in_cnde:
                    logger.info(f"[CNDE->TeraSim Filter] {len(in_nn_not_in_cnde)} vehicles have CNDE trajectory "
                               f"but filtered by TeraSim: {list(in_nn_not_in_cnde)[:5]}...")  # Show first 5

                # Debug: Log vehicles in cnde_control_ids but not in nn_control_commands
                in_cnde_not_in_nn = cnde_control_ids - nn_control_ids
                if in_cnde_not_in_nn:
                    pass  # Vehicles passed filter but no CNDE trajectory available

                if cnde_control_ids:
                    cnde_control_commands = Dict()
                    for veh_id, cmd in nn_control_commands.items():
                        if veh_id in cnde_control_ids:
                            cnde_control_commands[veh_id] = cmd
                logger.debug(
                        f"CNDE control filter: observation {len(nn_control_commands)} vehicles -> control {len(cnde_control_commands)} vehicles")

            self.nnde_update_tag += 1

        # Update active vehicle set when new predictions arrive
        if cnde_control_commands:
            self.cnde_traj_logger.update_active_vehicles(cnde_control_commands.keys())

        # Log actual SUMO positions for all active CNDE-controlled vehicles (every step)
        sim_time = traci.simulation.getTime()
        self.cnde_traj_logger.log_actual_positions_all_active(sim_time)

        # Build final commands: CNDE trajectories for controlled vehicles,
        # explicit DEFAULT for all other vehicles in the observation so their
        # controllers restore SUMO's full safety checks (smode=31).
        # NOTE: On non-inference steps (2-5 of each 5-step cycle),
        # cnde_control_commands is empty so all vehicles get DEFAULT.
        # The controller handles this correctly: if a CNDE trajectory's
        # duration hasn't expired, DEFAULT continues execution instead of
        # releasing control.
        control_commands = {}
        # Observation (insertion) order, not set order: the command dict's
        # order is the order commands are executed in, and set order follows
        # the per-process string hash seed (reproducibility, 2026-09-23).
        all_observed_veh_ids = []
        if AgentType.VEHICLE in env_observation:
            all_observed_veh_ids = list(env_observation[AgentType.VEHICLE].keys())

        # One DEFAULT command object per step: it is immutable downstream (the merge
        # replaces it, the controller only dispatches on its type), and building a
        # pydantic model per vehicle per step was 1-2 % of the wall time.
        default_command = NDECommand(command_type=CommandType.DEFAULT)
        for veh_id in all_observed_veh_ids:
            if veh_id in cnde_control_commands:
                control_commands[veh_id] = cnde_control_commands[veh_id]
            else:
                control_commands[veh_id] = default_command
        # Count vehicles continuing cached CNDE trajectories (steps 2-5)
        n_continuing = 0
        for veh_id in all_observed_veh_ids:
            if veh_id not in cnde_control_commands and veh_id in self.vehicle_list:
                ctrl = self.vehicle_list[veh_id].controller
                if (hasattr(ctrl, 'is_busy') and ctrl.is_busy
                        and ctrl.cached_control_command is not None
                        and hasattr(ctrl.cached_control_command, 'cached_command')
                        and ctrl.cached_control_command.cached_command.command_type
                        in (CommandType.TRAJECTORY, CommandType.CNDE_TRAJECTORY)):
                    n_continuing += 1
        n_default = len(control_commands) - len(cnde_control_commands) - n_continuing
        logger.info(f"Control commands: {len(cnde_control_commands)} CNDE + "
                     f"{n_continuing} continuing + {n_default} DEFAULT")

        # Phase timing log (written when total nnde_ms > 200ms to avoid log spam)
        _t_end = time.perf_counter()
        _total_ms = (_t_end - _t_start) * 1000
        if _total_ms > 200:
            try:
                import json as _json
                from pathlib import Path as _Path
                _phase_record = {
                    "sim_time": round(traci.simulation.getTime(), 1),
                    "total_nnde_ms": round(_total_ms, 2),
                    "cleanup_ms": round((_t_cleanup - _t_start) * 1000, 2),
                    "get_state_ms": round((_t_get_state - _t_cleanup) * 1000, 2),
                    "n_tracked_before_cleanup": len(cnde_tracked_ids),
                    "n_remove": len(remove_ids),
                    "n_cache_veh": len(cache_veh_ids) if cache_vehicle_ids is not None else -1,
                    "n_cnde_tracked_after": len(self.nnde_simulator.agent_record.current_track_agent_ids),
                }
                try:
                    _phase_record["sync_ms"] = round((_t_post_sync - _t_pre_sync) * 1000, 2)
                    _phase_record["new_remove_ms"] = round((_t_post_new_remove - _t_post_sync) * 1000, 2)
                except NameError:
                    pass
                _log_dir = _Path(__file__).parent.parent.parent.parent.parent / "outputs"
                _log_dir.mkdir(parents=True, exist_ok=True)
                with open(_log_dir / "nnde_phase_timing.jsonl", "a") as _f:
                    _f.write(_json.dumps(_phase_record) + "\n")
            except Exception:
                pass

        return control_commands

    # TODO:
    """
    Currently, the cnde range is managed by av range and default intersection coordination. But in the future,
    the NNDE/NDE behavior should considering to move nnde_decision_making to this layer. 
    """

    # def make_decisions(self, ctx):
    #     """Override NDE's make_decisions with neural network decisions.
    #
    #     This method is called by NADE.base_decision() to get base decisions.
    #     When NADE extends NNDE, it will get neural network decisions instead of
    #     traditional SUMO decisions.
    #
    #     Args:
    #         ctx: Context dictionary.
    #
    #     Returns:
    #         tuple: (nnde_commands, env_command_information)
    #     """
    #     # First get the traditional NDE decisions for env_command_information
    #     _, env_command_information = super().make_decisions(ctx)
    #
    #     # Get environment observation for neural network
    #     env_observation = self.get_env_observation(ctx)
    #
    #     # Get neural network decisions
    #     nnde_commands = self.nnde_make_decisions(env_observation)
    #
    #     return nnde_commands, env_command_information
    #
    # @profile
    # def on_step(self, ctx):
    #     """Step using neural network decisions.
    #
    #     Args:
    #         ctx: Context dictionary.
    #
    #     Returns:
    #         bool: Flag to indicate if the simulation should continue.
    #     """
    #     self.cache_history_tls_data()
    #     control_cmds, _ = self.make_decisions(ctx)
    #     self.refresh_control_commands_state()
    #     self.execute_control_commands(control_cmds)
    #     return self.should_continue_simulation()

    def on_stop(self, ctx):
        """Stop the NNDE and close CNDE trajectory log file."""
        self.cnde_traj_logger.close()
        return super().on_stop(ctx)
