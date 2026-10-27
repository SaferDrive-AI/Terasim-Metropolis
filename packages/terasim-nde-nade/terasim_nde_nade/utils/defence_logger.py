"""
Defence Logger Module for TeraSim NDE/NADE  

This module provides a dedicated logging system for tracking defence mechanisms
triggered during simulation. It helps diagnose issues like vehicle position jumps,
disappearances, and other artifacts caused by NaN/Inf values in trajectory calculations.

Created: 01092026
Purpose: Track and analyze defence code triggers to find root causes of trajectory issues.
"""

import os
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass, asdict
from enum import Enum
import threading

import numpy as np


class DefenceType(Enum):
    """Types of defence triggers."""
    NAN_INF_TRAJECTORY = "nan_inf_trajectory"
    NAN_INF_DISTANCE = "nan_inf_distance"
    NAN_INF_OFFSET = "nan_inf_offset"
    INVALID_POSITION = "invalid_position"
    CONVERT2D_FAILURE = "convert2d_failure"
    FALLBACK_POSITION = "fallback_position"
    OBSERVATION_MISMATCH = "observation_mismatch"  # For tracking CNDE vs TeraSim position mismatch


@dataclass
class DefenceEvent:
    """Represents a single defence trigger event."""
    timestamp: float  # Simulation time
    wall_time: str  # Real-world time
    defence_type: str
    vehicle_id: str
    location: str  # File and function where triggered
    original_values: Dict[str, Any]  # Original invalid values
    fallback_values: Dict[str, Any]  # Values used as fallback
    context: Dict[str, Any]  # Additional context (edge_id, lane_id, etc.)
    message: str  # Human-readable description


class DefenceLogger:
    """
    Singleton logger for defence mechanism events.

    This logger writes to a dedicated file (defence_log.jsonl) in the output directory,
    recording every time a defence mechanism is triggered with detailed context.
    """

    _instance: Optional['DefenceLogger'] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._log_file: Optional[Path] = None
        self._file_handle = None
        self._event_count = 0
        self._vehicles_with_nan_inf: set = set()  # Vehicles that had NaN/Inf values (skipped)
        self._trajectory_deviations: Dict[str, list] = {}  # veh_id -> list of (sim_time, commanded_pos, actual_pos)
        self._last_valid_positions: Dict[str, Tuple[float, float, float, float]] = {}  # veh_id -> (x, y, angle, speed)
        self._last_valid_distance_offsets: Dict[str, Tuple[float, float]] = {}  # cache_key -> (distance, offset)

    def initialize(self, output_dir: str) -> None:
        """
        Initialize the defence logger with the output directory.

        This clears all previous data and starts fresh for each simulation run.

        Args:
            output_dir: Path to the output directory (e.g., output/cnde_test/raw_data/0_0/)
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        self._log_file = output_path / "defence_log.jsonl"

        # Close existing handle if any
        if self._file_handle:
            self._file_handle.close()

        # Reset all internal state for new simulation
        self._event_count = 0
        self._vehicles_with_nan_inf = set()
        self._trajectory_deviations = {}
        self._last_valid_positions = {}

        # Open file in write mode (overwrites previous log)
        self._file_handle = open(self._log_file, 'w', encoding='utf-8')

        # Write header comment
        header = {
            "type": "header",
            "initialized_at": datetime.now().isoformat(),
            "description": "Defence mechanism trigger log for TeraSim NDE/NADE",
            "purpose": "Track NaN/Inf values and fallback positions to diagnose vehicle trajectory issues"
        }
        self._write_event(header)

    def _write_event(self, event_dict: Dict[str, Any]) -> None:
        """Write an event to the log file."""
        if self._file_handle:
            self._file_handle.write(json.dumps(event_dict, default=str) + '\n')
            self._file_handle.flush()

    def log_defence_event(
        self,
        defence_type: DefenceType,
        vehicle_id: str,
        location: str,
        original_values: Dict[str, Any],
        fallback_values: Dict[str, Any],
        sim_time: float,
        context: Optional[Dict[str, Any]] = None,
        message: str = ""
    ) -> None:
        """
        Log a defence trigger event.

        Args:
            defence_type: Type of defence triggered
            vehicle_id: ID of the affected vehicle
            location: Source location (file:function:line)
            original_values: The invalid values that triggered the defence
            fallback_values: The values used as fallback
            sim_time: Current simulation time
            context: Additional context information
            message: Human-readable description
        """
        # Convert numpy values to Python types for JSON serialization
        def convert_numpy(obj):
            if isinstance(obj, (np.floating, np.integer)):
                return float(obj)
            elif isinstance(obj, np.ndarray):
                return obj.tolist()
            elif isinstance(obj, dict):
                return {k: convert_numpy(v) for k, v in obj.items()}
            return obj

        event = DefenceEvent(
            timestamp=float(sim_time),
            wall_time=datetime.now().isoformat(),
            defence_type=defence_type.value,
            vehicle_id=vehicle_id,
            location=location,
            original_values=convert_numpy(original_values),
            fallback_values=convert_numpy(fallback_values),
            context=convert_numpy(context or {}),
            message=message
        )

        self._event_count += 1
        self._vehicles_with_nan_inf.add(vehicle_id)  # Track vehicles with NaN/Inf values
        event_dict = asdict(event)
        event_dict["event_number"] = self._event_count
        self._write_event(event_dict)

    def get_event_count(self) -> int:
        """Get the total number of defence events logged."""
        return self._event_count

    def store_valid_position(self, veh_id: str, x: float, y: float, angle: float, speed: float) -> None:
        """
        Store a valid position for a vehicle to be used as fallback.

        Args:
            veh_id: Vehicle ID
            x: X coordinate
            y: Y coordinate
            angle: Heading angle
            speed: Vehicle speed
        """
        if not (np.isnan([x, y, angle, speed]).any() or np.isinf([x, y, angle, speed]).any()):
            self._last_valid_positions[veh_id] = (float(x), float(y), float(angle), float(speed))

    def get_last_valid_position(self, veh_id: str) -> Optional[Tuple[float, float, float, float]]:
        """
        Get the last valid position for a vehicle.

        Args:
            veh_id: Vehicle ID

        Returns:
            Tuple of (x, y, angle, speed) or None if no valid position stored
        """
        return self._last_valid_positions.get(veh_id)

    def store_valid_distance_offset(self, cache_key: str, distance: float, offset: float) -> None:
        """Store valid distance/offset for a lane-change-abort trajectory cache key."""
        if not (np.isnan([distance, offset]).any() or np.isinf([distance, offset]).any()):
            self._last_valid_distance_offsets[cache_key] = (float(distance), float(offset))

    def get_last_valid_distance_offset(self, cache_key: str) -> Optional[Tuple[float, float]]:
        """Return last valid (distance, offset) for a cache key, or None."""
        return self._last_valid_distance_offsets.get(cache_key)

    def log_trajectory_deviation(
        self,
        veh_id: str,
        sim_time: float,
        commanded_x: float,
        commanded_y: float,
        actual_x: float,
        actual_y: float,
        deviation_threshold: float = 5.0,
        # Additional context parameters
        commanded_angle: Optional[float] = None,
        actual_angle: Optional[float] = None,
        pos_before: Optional[Tuple[float, float]] = None,
        angle_before: Optional[float] = None,
        speed_before: Optional[float] = None,
        road_id_before: Optional[str] = None,
        lane_id_before: Optional[str] = None,
        lane_index_before: Optional[int] = None,
        road_id_after: Optional[str] = None,
        lane_id_after: Optional[str] = None,
        lane_index_after: Optional[int] = None,
        keep_route_mode: Optional[int] = None,
        # Trajectory data for plotting
        raw_future_trajectory: Optional[np.ndarray] = None,  # Raw NN output before interpolation
        interpolated_trajectory: Optional[np.ndarray] = None,  # After interpolation
        historical_positions: Optional[list] = None,  # List of recent (x, y, time) positions
        obs_dict_ego: Optional[Dict[str, Any]] = None,  # Ego observation at decision time
    ) -> None:
        """
        Log when a vehicle's actual position deviates significantly from the commanded position.
        This helps identify vehicles that are not following the neural network commands.

        Args:
            veh_id: Vehicle ID
            sim_time: Simulation time
            commanded_x: X position commanded by the controller
            commanded_y: Y position commanded by the controller
            actual_x: Actual X position from SUMO
            actual_y: Actual Y position from SUMO
            deviation_threshold: Minimum deviation to log (default 5.0 meters)
            commanded_angle: Angle commanded to SUMO
            actual_angle: Actual angle after moveToXY
            pos_before: Position (x, y) before moveToXY
            angle_before: Angle before moveToXY
            speed_before: Speed before moveToXY
            road_id_before: Road ID before moveToXY
            lane_id_before: Lane ID before moveToXY
            lane_index_before: Lane index before moveToXY
            road_id_after: Road ID after moveToXY
            lane_id_after: Lane ID after moveToXY
            lane_index_after: Lane index after moveToXY
            keep_route_mode: The keepRoute mode used in moveToXY
            raw_future_trajectory: Raw neural network trajectory output (before interpolation)
                                   Format: [[x, y, angle, speed, time], ...]
            interpolated_trajectory: Interpolated trajectory used for execution
                                     Format: [[x, y, angle, speed, time], ...]
            historical_positions: Recent historical positions of the vehicle
                                  Format: [(x, y, time), ...]
            obs_dict_ego: Ego observation dict at decision time (position, heading, velocity)
        """
        deviation = np.sqrt((commanded_x - actual_x)**2 + (commanded_y - actual_y)**2)

        if deviation > deviation_threshold:
            if veh_id not in self._trajectory_deviations:
                self._trajectory_deviations[veh_id] = []

            # Calculate movement from before to after
            move_from_before = None
            if pos_before and pos_before[0] is not None:
                move_from_before = np.sqrt(
                    (actual_x - pos_before[0])**2 + (actual_y - pos_before[1])**2
                )

            # Calculate how far the commanded position is from the before position
            cmd_from_before = None
            if pos_before and pos_before[0] is not None:
                cmd_from_before = np.sqrt(
                    (commanded_x - pos_before[0])**2 + (commanded_y - pos_before[1])**2
                )

            # Determine likely cause of deviation
            deviation_cause = self._analyze_deviation_cause(
                deviation=deviation,
                road_id_before=road_id_before,
                road_id_after=road_id_after,
                lane_id_before=lane_id_before,
                lane_id_after=lane_id_after,
                move_from_before=move_from_before,
                cmd_from_before=cmd_from_before,
                keep_route_mode=keep_route_mode,
            )

            # Convert trajectory arrays to lists for JSON serialization
            raw_traj_list = None
            if raw_future_trajectory is not None:
                if isinstance(raw_future_trajectory, np.ndarray):
                    raw_traj_list = raw_future_trajectory.tolist()
                else:
                    raw_traj_list = list(raw_future_trajectory)

            interp_traj_list = None
            if interpolated_trajectory is not None:
                if isinstance(interpolated_trajectory, np.ndarray):
                    interp_traj_list = interpolated_trajectory.tolist()
                else:
                    interp_traj_list = list(interpolated_trajectory)

            # Convert obs_dict_ego values
            ego_obs = None
            if obs_dict_ego is not None:
                ego_obs = {
                    "position": list(obs_dict_ego.get("position", [])) if obs_dict_ego.get("position") is not None else None,
                    "heading": float(obs_dict_ego.get("heading")) if obs_dict_ego.get("heading") is not None else None,
                    "velocity": float(obs_dict_ego.get("velocity")) if obs_dict_ego.get("velocity") is not None else None,
                }

            deviation_record = {
                "sim_time": float(sim_time),
                "commanded": {
                    "x": float(commanded_x),
                    "y": float(commanded_y),
                    "angle": float(commanded_angle) if commanded_angle is not None else None,
                },
                "actual": {
                    "x": float(actual_x),
                    "y": float(actual_y),
                    "angle": float(actual_angle) if actual_angle is not None else None,
                },
                "before_moveToXY": {
                    "x": float(pos_before[0]) if pos_before and pos_before[0] is not None else None,
                    "y": float(pos_before[1]) if pos_before and pos_before[1] is not None else None,
                    "angle": float(angle_before) if angle_before is not None else None,
                    "speed": float(speed_before) if speed_before is not None else None,
                    "road_id": road_id_before,
                    "lane_id": lane_id_before,
                    "lane_index": lane_index_before,
                },
                "after_moveToXY": {
                    "road_id": road_id_after,
                    "lane_id": lane_id_after,
                    "lane_index": lane_index_after,
                },
                "deviation_meters": float(deviation),
                "move_from_before_meters": float(move_from_before) if move_from_before is not None else None,
                "cmd_from_before_meters": float(cmd_from_before) if cmd_from_before is not None else None,
                "keep_route_mode": keep_route_mode,
                "deviation_cause": deviation_cause,
                # Trajectory data for plotting
                "trajectory_data": {
                    "raw_future_trajectory": raw_traj_list,  # NN output: [[x, y, angle, speed, time], ...]
                    "interpolated_trajectory": interp_traj_list,  # After interpolation
                    "historical_positions": historical_positions,  # [(x, y, time), ...]
                    "ego_obs_at_decision": ego_obs,  # Vehicle state when NN made decision
                },
            }
            self._trajectory_deviations[veh_id].append(deviation_record)

            # Also write to log file immediately for real-time monitoring
            event = {
                "type": "trajectory_deviation",
                "wall_time": datetime.now().isoformat(),
                "vehicle_id": veh_id,
                **deviation_record,
                "message": f"Vehicle {veh_id} deviated {deviation:.2f}m from commanded position. Cause: {deviation_cause}"
            }
            self._write_event(event)

    def _analyze_deviation_cause(
        self,
        deviation: float,
        road_id_before: Optional[str],
        road_id_after: Optional[str],
        lane_id_before: Optional[str],
        lane_id_after: Optional[str],
        move_from_before: Optional[float],
        cmd_from_before: Optional[float],
        keep_route_mode: Optional[int],
    ) -> str:
        """
        Analyze the likely cause of trajectory deviation.

        Returns a string describing the likely cause.
        """
        causes = []

        # Check if road changed unexpectedly
        if road_id_before and road_id_after:
            if road_id_before != road_id_after:
                causes.append(f"road_changed:{road_id_before}->{road_id_after}")

            # Check if vehicle is on internal edge (intersection)
            if road_id_after.startswith(":"):
                causes.append("on_internal_edge(intersection)")
            elif road_id_before.startswith(":") and not road_id_after.startswith(":"):
                causes.append("exited_intersection")

        # Check if vehicle barely moved (SUMO rejected the command)
        if move_from_before is not None and cmd_from_before is not None:
            if move_from_before < 1.0 and cmd_from_before > 5.0:
                causes.append(f"sumo_rejected_large_move(cmd={cmd_from_before:.1f}m,actual={move_from_before:.1f}m)")
            elif move_from_before < deviation * 0.5:
                causes.append(f"sumo_limited_movement(requested={cmd_from_before:.1f}m)")

        # Check keepRoute mode effects
        if keep_route_mode is not None:
            # keepRoute modes: 0=no restriction, 1=keep route, 2=exact position
            if keep_route_mode == 1:
                causes.append("keepRoute=1(must_stay_on_route)")
            elif keep_route_mode == 2:
                causes.append("keepRoute=2(exact_but_may_fail)")

        # Check if commanded position might be off-network
        if deviation > 50:
            causes.append("large_deviation(>50m,likely_off_network)")
        elif deviation > 20:
            causes.append("medium_deviation(>20m,possibly_off_road)")

        # Check lane changes
        if lane_id_before and lane_id_after and lane_id_before != lane_id_after:
            causes.append(f"lane_changed:{lane_id_before}->{lane_id_after}")

        if not causes:
            causes.append("unknown")

        return "; ".join(causes)

    def log_observation_mismatch(
        self,
        veh_id: str,
        sim_time: float,
        cnde_position: Tuple[float, float],
        cnde_heading: float,
        terasim_position: Tuple[float, float],
        terasim_heading: float,
        mismatch_threshold: float = 1.0,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log when CNDE's internal position doesn't match TeraSim's observation.
        This helps identify the root cause of trajectory inconsistencies.

        Args:
            veh_id: Vehicle ID
            sim_time: Simulation time
            cnde_position: Position recorded in CNDE's agent_record (x, y)
            cnde_heading: Heading recorded in CNDE's agent_record
            terasim_position: Position from TeraSim/SUMO observation (x, y)
            terasim_heading: Heading from TeraSim/SUMO observation
            mismatch_threshold: Minimum position mismatch to log (default 1.0 meters)
            context: Additional context information
        """
        position_diff = np.sqrt(
            (cnde_position[0] - terasim_position[0])**2 +
            (cnde_position[1] - terasim_position[1])**2
        )
        heading_diff = abs(cnde_heading - terasim_heading)
        # Normalize heading difference to [-pi, pi]
        heading_diff = (heading_diff + np.pi) % (2 * np.pi) - np.pi
        heading_diff = abs(heading_diff)

        if position_diff > mismatch_threshold:
            event = {
                "type": "observation_mismatch",
                "wall_time": datetime.now().isoformat(),
                "vehicle_id": veh_id,
                "sim_time": float(sim_time),
                "cnde_state": {
                    "position": [float(cnde_position[0]), float(cnde_position[1])],
                    "heading": float(cnde_heading),
                },
                "terasim_state": {
                    "position": [float(terasim_position[0]), float(terasim_position[1])],
                    "heading": float(terasim_heading),
                },
                "position_mismatch_meters": float(position_diff),
                "heading_mismatch_rad": float(heading_diff),
                "context": context or {},
                "message": f"Vehicle {veh_id} position mismatch: CNDE={cnde_position}, TeraSim={terasim_position}, diff={position_diff:.2f}m"
            }
            self._write_event(event)

    def get_vehicles_with_deviations(self) -> Dict[str, list]:
        """Get all vehicles that had trajectory deviations."""
        return self._trajectory_deviations.copy()

    def get_vehicles_with_nan_inf(self) -> set:
        """Get vehicles that had NaN/Inf values in their trajectory."""
        return self._vehicles_with_nan_inf.copy()

    def close(self) -> None:
        """Close the log file."""
        if self._file_handle:
            # Write summary
            summary = {
                "type": "summary",
                "closed_at": datetime.now().isoformat(),
                "total_nan_inf_events": self._event_count,
                "vehicles_with_nan_inf": list(self._vehicles_with_nan_inf),  # Vehicles skipped due to NaN/Inf
                "vehicles_with_trajectory_deviations": list(self._trajectory_deviations.keys()),  # Vehicles where SUMO rejected/modified command
                "total_deviation_events": sum(len(v) for v in self._trajectory_deviations.values()),
            }
            self._write_event(summary)
            self._file_handle.close()
            self._file_handle = None

    def __del__(self):
        self.close()


# Global singleton instance
_defence_logger: Optional[DefenceLogger] = None


def get_defence_logger() -> DefenceLogger:
    """Get the global defence logger instance."""
    global _defence_logger
    if _defence_logger is None:
        _defence_logger = DefenceLogger()
    return _defence_logger


def init_defence_logger(output_dir: str) -> DefenceLogger:
    """
    Initialize the global defence logger with the output directory.

    Args:
        output_dir: Path to the output directory

    Returns:
        The initialized DefenceLogger instance
    """
    logger = get_defence_logger()
    logger.initialize(output_dir)
    return logger
