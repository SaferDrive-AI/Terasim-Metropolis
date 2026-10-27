import json
import os
from pathlib import Path

from addict import Dict
from loguru import logger
import numpy as np

from terasim.agent.agent_controller import AgentController
from terasim.overlay import traci
import terasim.utils as utils

from ..utils import (
    CommandType,
    NDECommand,
    interpolate_future_trajectory,
)
from ..utils.defence_logger import get_defence_logger, DefenceType


# Module-level file handle for move verification log (overwrite per run)
_move_verify_log_file = None
# Module-level file handle for moveTo diagnostic log (append mode, flushed per write)
_moveto_diag_file = None
# Module-level dict tracking active lane-change commands per vehicle.
# Maps veh_id -> sim_time when current LC duration expires. Prevents
# re-issuing changeLaneRelative every tick (which restarts sublane motion
# and causes visible heading jitter in merge_demo_aa). See
# execute_lane_change_command below.
_lc_active_until = {}

# vis_cutin: per-BV bookkeeping for the TWO-PHASE adversarial cut-in.
#   Phase 1 (swing): on commit the BV keeps the AV's speed so the sublane swing
#     completes while the BV is still AHEAD of the AV and lands fully in lane 0
#     (an instant brake at commit dropped the BV into the AV's longitudinal
#     overlap zone, where SUMO re-blocks the lateral move and the BV never
#     reaches lane 0).
#   Phase 2 (brake): once the swing time has elapsed AND the BV is on the AV's
#     lane, the BV hard-brakes so the AV (which is now close behind in the same
#     lane) cannot avoid rear-ending it.
# Maps veh_id -> {"t0": commit_sim_time, "braked": bool}.
_cutin_phase = {}
_CUTIN_SWING_TIME = 0.7   # s, must exceed the changeLaneRelative swing_dur
_CUTIN_BRAKE_SPEED = 0.3  # m/s, the post-swing hard brake target


def _moveto_log(msg):
    """Write a line to the moveTo diagnostic log (survives segfault via flush)."""
    global _moveto_diag_file
    if _moveto_diag_file is None:
        log_path = Path("outputs") / "moveTo_diag.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _moveto_diag_file = open(log_path, "w")
    _moveto_diag_file.write(msg + "\n")
    _moveto_diag_file.flush()


# Cartesian -> road position for every CNDE trajectory point.  libsumo's convertRoad
# re-scans every lane of the network per call (~0.2-0.3 ms, 10 % of a CNDE
# evaluation on a 1,000-vehicle corridor); the exact Python replica in
# terasim_nde_nade.utils.sumo_lane_locator returns the same (edge, pos, index) from
# a cached lane table.  TERASIM_NDE_CONVERTROAD_IMPL=libsumo restores the TraCI call.
_CONVERTROAD_IMPL = os.environ.get("TERASIM_NDE_CONVERTROAD_IMPL", "python").strip().lower()


def _convert_road(x, y):
    """Same result as ``traci.simulation.convertRoad(x, y, isGeo=False)``."""
    if _CONVERTROAD_IMPL == "libsumo":
        return traci.simulation.convertRoad(x, y, isGeo=False)
    from ..utils.sumo_lane_locator import convert_road

    return convert_road(x, y)


def _get_move_verify_log():
    """Get or create the move verification log file handle (overwrite mode)."""
    global _move_verify_log_file
    if _move_verify_log_file is None:
        log_path = Path("outputs") / "move_verify_log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _move_verify_log_file = open(log_path, "w")
    return _move_verify_log_file


def get_all_routes():
    """Get all routes in the network.

    Returns:
        list: List of all routes in the network.
    """
    return traci.route.getIDList()


def get_all_route_edges():
    """Get all route edges in the network.

    Returns:
        dict: Dictionary of all route edges in the network.
    """
    all_routes = get_all_routes()
    all_route_edges = {}
    for route in all_routes:
        all_route_edges[route] = traci.route.getEdges(route)
    return all_route_edges


class NDEController(AgentController):
    # Cooldown duration (seconds) after lane mismatch before re-entering CNDE control.
    LANE_MISMATCH_COOLDOWN = 2.0

    # Vehicle body colors for demo visualization
    COLOR_CNDE_ACTIVE   = (0,   100, 255, 255)  # bright blue  — new CNDE trajectory
    COLOR_CNDE_CONTINUE = (100, 180, 255, 255)  # sky blue     — sustaining cached CNDE
    COLOR_NDE_NORMAL    = (180, 180, 180, 255)  # light gray   — IDM/SUMO, no adversity

    # Per-vehicle smoothed angle state (degrees in SUMO convention [0,360)).
    # Used by _smooth_angle to rate-limit logged heading values when SUMO's
    # internal junction geometry causes natural angle snaps (e.g. degenerate
    # 0.20m connector edges in real-world OSM maps like merge_demo_aa where
    # BV on internal junction lane :29314517_3_1 produces 150°+ flips between
    # ticks). Log/replay-only — SUMO physics/position untouched.
    _smoothed_angles = {}

    @staticmethod
    def _set_color(veh_id, color):
        try:
            traci.vehicle.setColor(veh_id, color)
        except Exception:
            pass

    @staticmethod
    def _smooth_angle(veh_id, max_step_deg=15.0):
        """Compute a rate-limited smoothed heading for veh_id and return it.

        Returns the smoothed angle (degrees in SUMO convention) for this tick.
        Maintains per-vehicle state in NDEController._smoothed_angles.

        Why no setAngle/moveToXY: SUMO TraCI exposes no setAngle. moveToXY
        with a same-position angle override is silently ignored on internal
        junction lanes — exactly the geometry that causes the jitter in
        merge_demo_aa (BV on :29314517_3_1 between two overlapping junctions
        in real-world Ann Arbor OSM). The codebase also warns that any
        moveToXY on junction lanes can corrupt SUMO state and segfault.

        Approach: smoothing is applied at the LOG/REPLAY layer. The
        heading_tracker.jsonl (consumed by scripts/heading_tracker_to_fcd.py
        for SUMO-gui replay and by analytics) records the SMOOTHED angle;
        SUMO's live GUI keeps its internal jitter (visible only at runtime),
        but every downstream artifact and replay reflects the smooth heading.
        SUMO physics / collision detection are entirely untouched, so
        adversity fire counts and AV crashes are preserved.
        """
        try:
            cur = traci.vehicle.getAngle(veh_id)
        except Exception:
            return None
        prev = NDEController._smoothed_angles.get(veh_id)
        if prev is None:
            NDEController._smoothed_angles[veh_id] = cur
            return cur
        # Shortest signed angular delta in [-180, 180]
        d = ((cur - prev + 180.0) % 360.0) - 180.0
        if abs(d) <= max_step_deg:
            # Within tolerance — accept SUMO's value as new baseline
            NDEController._smoothed_angles[veh_id] = cur
            return cur
        # Snap detected — rate-limit the change
        step = max_step_deg if d > 0 else -max_step_deg
        new = (prev + step) % 360.0
        NDEController._smoothed_angles[veh_id] = new
        return new

    def __init__(self, simulator, params=None):
        self.is_busy = False
        self.cached_control_command = None
        # Move verification: store target from moveToXY, check at next step
        self._pending_move_target = None
        # Saved route info for restoring after CNDE control release
        self._saved_route = None
        self._saved_destination_edge = None
        # Cooldown: when a lane mismatch is detected, exclude this vehicle
        # from CNDE control until the cooldown expires.
        self.cnde_cooldown_until = 0.0
        # Track current control mode: 'sumo' or 'cnde'
        self._control_mode = 'sumo'
        # pos_error watchdog: abort trajectory when position keeps drifting
        self._pos_error_streak = 0
        # Total trajectory aborts across the run. After ABORT_BLACKLIST_THRESHOLD
        # aborts we permanently exclude this vehicle from CNDE control by
        # parking cnde_cooldown_until in the far future. Without this, a vehicle
        # whose SUMO state cannot track CNDE trajectories (e.g. real-world FCD
        # vehicles that get stranded on routes incompatible with CNDE's
        # predictions) is re-acquired by CNDE every inference cycle, drifts,
        # aborts, and re-acquires — accumulating per-cycle work that doubles
        # adversity_ms each cycle and grinds the sim to a halt past sim_t≈370 s.
        self._total_aborts = 0
        return super().__init__(
            simulator, control_command_schema=NDECommand, params=params
        )

    @staticmethod
    def _vehicle_exists_static(veh_id):
        """Check if vehicle exists in SUMO without calling getIDList().
        Uses getSpeed as a lightweight probe — raises exception if vehicle gone."""
        try:
            traci.vehicle.getSpeed(veh_id)
            return True
        except Exception:
            return False

    def _update_controller_status(self, veh_id, current_time=None):
        """Refresh controller state each timestep.

        Simple logic: if busy with a cached command and its duration expired,
        release control back to SUMO.
        """
        # Verify previous moveToXY was executed; abort trajectory on persistent drift
        if self._pending_move_target is not None:
            target = self._pending_move_target
            self._pending_move_target = None
            try:
                actual_pos = traci.vehicle.getPosition(veh_id)
                pos_error = np.sqrt((actual_pos[0] - target["x"])**2 + (actual_pos[1] - target["y"])**2)
                if pos_error > 1.5:
                    self._pos_error_streak += 1
                    logger.warning(
                        f"[Move Verify] {veh_id}: pos_error={pos_error:.2f}m "
                        f"(streak={self._pos_error_streak}, "
                        f"target=({target['x']:.1f},{target['y']:.1f}), "
                        f"actual=({actual_pos[0]:.1f},{actual_pos[1]:.1f}))"
                    )
                    if self._pos_error_streak >= 4:
                        # Trajectory not tracking — release to IDM to prevent libsumo crash
                        self._total_aborts += 1
                        logger.warning(
                            f"[Move Verify] {veh_id}: aborting trajectory after "
                            f"{self._pos_error_streak} consecutive drift steps "
                            f"(total_aborts={self._total_aborts})"
                        )
                        self.is_busy = False
                        self.cached_control_command = None
                        self.all_checks_on(veh_id)
                        self._pos_error_streak = 0
                        # ABORT_BLACKLIST_THRESHOLD: after this many total aborts,
                        # permanently exclude the vehicle from CNDE control. CNDE
                        # was repeatedly re-acquiring it and re-issuing trajectories
                        # that don't track — each retry doubles adversity_ms.
                        # Dropped 2 → 1 (2026-05-14): in dense map areas N vehicles
                        # entered drift state simultaneously; with 2-strike each
                        # spent 8 steps grinding before exclusion → cumulative
                        # adversity_ms exploded 100×. One strike is enough — a
                        # vehicle that drifts 4 consecutive steps has shown CNDE
                        # can't track its SUMO state.
                        ABORT_BLACKLIST_THRESHOLD = 1
                        if self._total_aborts >= ABORT_BLACKLIST_THRESHOLD:
                            self.cnde_cooldown_until = 1e18
                            logger.warning(
                                f"[Move Verify] {veh_id}: blacklisted from CNDE "
                                f"control (total_aborts={self._total_aborts} ≥ "
                                f"{ABORT_BLACKLIST_THRESHOLD})"
                            )
                else:
                    self._pos_error_streak = 0
            except Exception:
                self._pos_error_streak = 0

        # Check if cached command duration expired → release to SUMO
        if self.is_busy and self.cached_control_command is not None:
            current_time = traci.simulation.getTime() if current_time is None else current_time
            duration_expired = (
                current_time - self.cached_control_command.timestep
                >= self.cached_control_command.cached_command.duration
            )
            if duration_expired:
                self.is_busy = False
                self.cached_control_command = None
                self.all_checks_on(veh_id)

    def execute_control_command(self, veh_id, control_command, obs_dict):
        """Execute control command based on command type.

        Simple dispatch:
        - DEFAULT: hand control to SUMO (or continue cached non-CNDE command)
        - CNDE_TRAJECTORY: disable safety checks, execute moveToXY
        - TRAJECTORY (adversarial): same as CNDE_TRAJECTORY
        - CUSTOM/ACCELERATION/LEFT/RIGHT: execute as before

        New CNDE_TRAJECTORY always preempts old one (rolling 0.5s updates).
        DEFAULT releases CNDE control back to SUMO only when the trajectory
        duration has expired. On intermediate steps (2-5 of the 5-step CNDE
        cycle), DEFAULT continues executing the cached trajectory.
        """
        if control_command.command_type == CommandType.DEFAULT:
            if self.is_busy:
                if self.cached_control_command.cached_command.command_type in (
                    CommandType.CNDE_TRAJECTORY, CommandType.TRAJECTORY
                ):
                    current_time = traci.simulation.getTime()
                    duration_expired = (
                        current_time - self.cached_control_command.timestep
                        >= self.cached_control_command.cached_command.duration
                    )
                    if duration_expired:
                        self.is_busy = False
                        self.cached_control_command = None
                        self.all_checks_on(veh_id)
                        # Trajectory expired — vehicle returns to NDE normal
                        NDEController._set_color(veh_id, NDEController.COLOR_NDE_NORMAL)
                        return
                    # Sustaining cached CNDE trajectory between inference cycles
                    if self.cached_control_command.cached_command.command_type == CommandType.CNDE_TRAJECTORY:
                        NDEController._set_color(veh_id, NDEController.COLOR_CNDE_CONTINUE)
                    self.execute_control_command_onestep(
                        veh_id, self.cached_control_command, obs_dict, first_step=False
                    )
                    return
                # Non-trajectory busy (LEFT/RIGHT/ACCELERATION) → continue executing
                self.execute_control_command_onestep(
                    veh_id, self.cached_control_command, obs_dict, first_step=False
                )
            else:
                # DEFAULT + not busy → pure IDM/SUMO, no adversity, no CNDE
                NDEController._set_color(veh_id, NDEController.COLOR_NDE_NORMAL)
            return

        if control_command.command_type == CommandType.CUSTOM:
            if self.is_busy:
                self.execute_control_command_onestep(
                    veh_id, self.cached_control_command, obs_dict, first_step=False
                )
                return
            self.cached_control_command = Dict(
                {
                    "timestep": traci.simulation.getTime(),
                    "cached_command": control_command,
                }
            )
            self.execute_control_command_onestep(
                veh_id, self.cached_control_command, obs_dict, first_step=True
            )
            return

        # For TRAJECTORY, CNDE_TRAJECTORY, ACCELERATION, LEFT, RIGHT:
        # Always accept — preempt any existing command
        self.all_checks_off(veh_id)
        self.is_busy = True
        control_command = interpolate_control_command(control_command, obs_dict)
        self.cached_control_command = Dict(
            {
                "timestep": traci.simulation.getTime(),
                "cached_command": control_command,
            }
        )
        # New CNDE trajectory arrived this inference cycle
        if control_command.command_type == CommandType.CNDE_TRAJECTORY:
            NDEController._set_color(veh_id, NDEController.COLOR_CNDE_ACTIVE)
        self.execute_control_command_onestep(
            veh_id, self.cached_control_command, obs_dict, first_step=True
        )

    def execute_control_command_onestep(
        self, veh_id, cached_control_command, obs_dict, first_step=False
    ):
        """Execute the control command for one step.

        Args:
            veh_id (str): Vehicle ID.
            cached_control_command (dict): Cached control command.
            obs_dict (dict): Observation of the ego agent.
            first_step (bool, optional): Flag to indicate if this is the first step. Defaults to False.
        """
        if cached_control_command["cached_command"].command_type == CommandType.CNDE_TRAJECTORY:
            # Demoted CRITICAL → DEBUG (2026-05-14): this log fired ~1.5×/step,
            # producing 15k+ entries per run. Loguru's async queued_writer was a
            # suspected contributor to the multi-GB RAM growth in long runs.
            logger.debug(f"vehicle {veh_id}, applying CNDE trajectory")
        if cached_control_command["cached_command"].command_type == CommandType.CUSTOM:
            if (
                cached_control_command["cached_command"].custom_control_command
                is not None
                and cached_control_command[
                    "cached_command"
                ].custom_execute_control_command
                is not None
            ):
                cached_control_command["cached_command"].custom_execute_control_command(
                    veh_id,
                    cached_control_command["cached_command"].custom_control_command,
                    obs_dict,
                )
                return
            else:
                logger.error(
                    "Custom control command or Custom control command execution is not defined"
                )
                return

        if cached_control_command["cached_command"].command_type in (
            CommandType.TRAJECTORY, CommandType.CNDE_TRAJECTORY
        ):
            move_target = self.execute_trajectory_command(
                veh_id, cached_control_command["cached_command"], obs_dict
            )
            if move_target is not None:
                # moveToXY was executed — store target for verification
                move_target["sim_time"] = traci.simulation.getTime()
                move_target["is_first_step"] = first_step
                self._pending_move_target = move_target
            else:
                # Safety check skipped the move (junction, edge end, etc.).
                # Restore SUMO control so the vehicle isn't stuck with smode=32
                # inside a junction. SUMO will handle traversal naturally.
                self.is_busy = False
                self.cached_control_command = None
                self.all_checks_on(veh_id)
        elif (
            cached_control_command["cached_command"].command_type == CommandType.LEFT
            or cached_control_command["cached_command"].command_type
            == CommandType.RIGHT
        ):
            self.execute_lane_change_command(
                veh_id,
                cached_control_command["cached_command"],
                obs_dict,
                first_step=first_step,
            )
        elif (
            cached_control_command["cached_command"].command_type
            == CommandType.ACCELERATION
        ):
            self.execute_acceleration_command(
                veh_id, cached_control_command["cached_command"], obs_dict
            )
        else:
            logger.error("Invalid command type")
        return

    @staticmethod
    def execute_trajectory_command(veh_id, control_command, obs_dict):
        """Execute the trajectory command.

        Args:
            veh_id (str): Vehicle ID.
            control_command (NDECommand): Control command.
            obs_dict (dict): Observation of the ego agent.
        """
        assert control_command.command_type in (CommandType.TRAJECTORY, CommandType.CNDE_TRAJECTORY)

        # Defensive check FIRST: ensure vehicle still exists before ANY TraCI calls
        if not NDEController._vehicle_exists_static(veh_id):
            logger.warning(f"Vehicle {veh_id} no longer exists in SUMO, skipping trajectory control")
            return

        # Defensive check SECOND: skip ALL trajectory control when the vehicle is
        # currently inside an internal junction lane (lane id starts with ":"). 
        # Calling setSpeedMode / setLaneChangeMode / moveToXY / moveTo on such a 
        # vehicle corrupts SUMO's internal state and causes libsumo executeMove 
        # to SEGFAULT on the next step. Let SUMO finish traversing the junction
        # naturally; we resume control once the vehicle is back on a normal lane.
        try:
            current_road = traci.vehicle.getRoadID(veh_id)
            if current_road.startswith(":"):
                logger.debug(
                    f"[Junction Skip] {veh_id}: on junction lane {current_road}, "
                    f"skipping ALL trajectory control to prevent libsumo segfault"
                )
                return
        except Exception as e:
            logger.warning(f"[Junction Check] {veh_id}: getRoadID failed: {e}, skipping")
            return

        # get the closest timestep trajectory point in control_command.trajectory to current timestep
        trajectory_array = control_command.future_trajectory
        next_timestep = traci.simulation.getTime() + traci.simulation.getDeltaT()
        closest_timestep_trajectory = min(
            trajectory_array, key=lambda x: abs(x[-1] - next_timestep)
        )

        x, y, angle, speed = closest_timestep_trajectory[0], closest_timestep_trajectory[1], closest_timestep_trajectory[2], closest_timestep_trajectory[3]

        # Check for zero/invalid trajectory output from CNDE neural network
        if x == 0.0 and y == 0.0:
            logger.warning(
                f"[CNDE Safety] {veh_id}: trajectory output is (0.0, 0.0), "
                f"likely invalid CNDE prediction, SKIPPING"
            )
            return

        if speed < 0.0 or speed > 100.0:
            logger.warning(
                f"[CNDE Safety] {veh_id}: invalid speed {speed:.1f} m/s, SKIPPING"
            )
            return

        # --- Safety checks BEFORE any setSpeedMode/setLaneChangeMode/moveToXY ---
        # These TraCI calls can segfault if SUMO's internal state is corrupted,
        # so we validate the trajectory target first using only getPosition/getAngle/getSpeed.
        effective_keep_route = control_command.keep_route_mode
        if effective_keep_route == 2:
            try:
                current_pos = traci.vehicle.getPosition(veh_id)
                current_angle = traci.vehicle.getAngle(veh_id)
                current_speed = traci.vehicle.getSpeed(veh_id)

                # Check 1: angle deviation > 90° → skip
                angle_diff = abs(angle - current_angle) % 360
                if angle_diff > 180:
                    angle_diff = 360 - angle_diff
                if angle_diff > 45:
                    logger.warning(
                        f"[CNDE Safety] {veh_id}: target angle {angle:.1f} vs current {current_angle:.1f} "
                        f"(diff={angle_diff:.1f}°>45°), SKIPPING moveToXY to prevent heading jump"
                    )
                    return

                # Check 2: position jump too large relative to speed.
                dt = traci.simulation.getDeltaT()
                dist = np.sqrt((x - current_pos[0])**2 + (y - current_pos[1])**2)
                max_dist = max(5.0, max(speed, current_speed) * dt * 3)
                if dist > max_dist:
                    logger.warning(
                        f"[CNDE Safety] {veh_id}: position jump {dist:.1f}m > max {max_dist:.1f}m "
                        f"(current=({current_pos[0]:.1f},{current_pos[1]:.1f}), "
                        f"target=({x:.1f},{y:.1f})), SKIPPING moveToXY"
                    )
                    return
            except Exception as e:
                logger.warning(f"[CNDE Safety] {veh_id}: exception during safety check: {e}, skipping moveToXY")
                return

        # Fix CNDE heading sign error: CNDE's NN sometimes outputs heading with
        # wrong sign (e.g. +93° instead of -93° in CNDE convention), which after
        # conversion becomes ~357° instead of ~183° in SUMO convention — exactly
        # 180° off. Detect this by comparing with SUMO's current angle and flip
        # back if the difference is close to 180°.
        try:
            current_angle = traci.vehicle.getAngle(veh_id)
            angle_diff = abs(angle - current_angle) % 360
            if angle_diff > 180:
                angle_diff = 360 - angle_diff
            if angle_diff > 120:
                # CNDE heading is ~180° off — flip it
                angle = (angle + 180) % 360
        except Exception:
            pass

        # Note: backward displacement correction is now handled upstream in
        # cnde_sim_to_control_commands() which shifts the entire trajectory at
        # once, preserving curve shape. Per-point fix was removed to avoid
        # distorting trajectories on curved segments.

        logger.debug(f"Traj LOG: Trajectory for {veh_id}: x={x:.2f}, y={y:.2f}, angle={angle:.2f}, speed={speed:.2f}")

        # Set CNDE speed mode — keeps junction right-of-way and red light
        # checks active while allowing trajectory control.
        # Must match all_checks_off() to avoid mode mismatch.
        traci.vehicle.setSpeedMode(veh_id, NDEController.CNDE_SPEED_MODE)
        # Disable SUMO's automatic lane change logic.
        traci.vehicle.setLaneChangeMode(veh_id, 0)

        # 01092026 YW: Improved defence mechanism - use last valid position instead of skipping
        # This addresses the issue where hardcoded return causes vehicle disappearance in FCD output
        defence_logger = get_defence_logger()
        sim_time = traci.simulation.getTime()

        if np.isnan([x, y, angle, speed]).any() or np.isinf([x, y, angle, speed]).any():
            # Get last valid position as fallback instead of skipping entirely
            last_valid = defence_logger.get_last_valid_position(veh_id)

            if last_valid is not None:
                fallback_x, fallback_y, fallback_angle, fallback_speed = last_valid
                # Log defence event with full context
                defence_logger.log_defence_event(
                    defence_type=DefenceType.NAN_INF_TRAJECTORY,
                    vehicle_id=veh_id,
                    location="nde_controller.py:execute_trajectory_command",
                    original_values={"x": x, "y": y, "angle": angle, "speed": speed},
                    fallback_values={"x": fallback_x, "y": fallback_y, "angle": fallback_angle, "speed": fallback_speed},
                    sim_time=sim_time,
                    context={
                        "trajectory_length": len(trajectory_array),
                        "next_timestep": next_timestep,
                        "obs_ego_position": obs_dict.get("ego", {}).get("position", None),
                        "obs_ego_velocity": obs_dict.get("ego", {}).get("velocity", None),
                    },
                    message=f"NaN/Inf detected in trajectory for {veh_id}, using last valid position as fallback"
                )
                logger.warning(
                    f"DEFENCE: NaN/Inf trajectory for {veh_id}, using last valid position: "
                    f"({fallback_x:.2f}, {fallback_y:.2f}) instead of ({x}, {y})"
                )
                # Use fallback values
                x, y, angle, speed = fallback_x, fallback_y, fallback_angle, fallback_speed
            else:
                # No fallback available - try to get current position from SUMO as last resort
                try:
                    current_pos = traci.vehicle.getPosition(veh_id)
                    current_angle = traci.vehicle.getAngle(veh_id)
                    current_speed = traci.vehicle.getSpeed(veh_id)
                    fallback_x, fallback_y = current_pos[0], current_pos[1]
                    fallback_angle, fallback_speed = current_angle, current_speed

                    defence_logger.log_defence_event(
                        defence_type=DefenceType.NAN_INF_TRAJECTORY,
                        vehicle_id=veh_id,
                        location="nde_controller.py:execute_trajectory_command",
                        original_values={"x": x, "y": y, "angle": angle, "speed": speed},
                        fallback_values={"x": fallback_x, "y": fallback_y, "angle": fallback_angle, "speed": fallback_speed, "source": "traci_current"},
                        sim_time=sim_time,
                        context={
                            "trajectory_length": len(trajectory_array),
                            "next_timestep": next_timestep,
                            "no_cached_position": True,
                        },
                        message=f"NaN/Inf detected, no cached position, using current SUMO position for {veh_id}"
                    )
                    logger.warning(
                        f"DEFENCE: NaN/Inf trajectory for {veh_id}, no cache available, "
                        f"using current SUMO position: ({fallback_x:.2f}, {fallback_y:.2f})"
                    )
                    x, y, angle, speed = fallback_x, fallback_y, fallback_angle, fallback_speed
                except Exception as e:
                    # Truly no fallback - must skip to prevent crash
                    defence_logger.log_defence_event(
                        defence_type=DefenceType.NAN_INF_TRAJECTORY,
                        vehicle_id=veh_id,
                        location="nde_controller.py:execute_trajectory_command",
                        original_values={"x": x, "y": y, "angle": angle, "speed": speed},
                        fallback_values={"skipped": True},
                        sim_time=sim_time,
                        context={"error": str(e), "no_fallback_available": True},
                        message=f"NaN/Inf detected, no fallback available, skipping moveToXY for {veh_id}"
                    )
                    logger.error(
                        f"DEFENCE CRITICAL: NaN/Inf trajectory for {veh_id}, no fallback available, "
                        f"skipping to prevent crash. Error: {e}"
                    )
                    return  # Skip only as last resort
        else:
            # Valid trajectory - store for future fallback use
            defence_logger.store_valid_position(veh_id, x, y, angle, speed)

        # Placement primitive depends on command type:
        #
        # * CNDE_TRAJECTORY (neural network, MANY vehicles): use moveTo. moveToXY
        #   modifies internal lane/edge data structures which can corrupt memory
        #   and cause SIGSEGV in executeMove with many simultaneously-controlled
        #   vehicles. moveTo directly places on a lane (minimal state changes,
        #   safe). Trade-off: ~0.7m lane-center SNAPPING.
        #
        # * TRAJECTORY (adversarial, e.g. lane-change-abort): use moveToXY with
        #   keepRoute=2. The lane-center snapping of moveTo is FATAL to a
        #   sub-lane lateral maneuver — it quantizes every intermediate lateral
        #   offset of the swing to the nearest lane center, collapsing the smooth
        #   raised-cosine sweep into a binary lane teleport (the "swing looks
        #   abrupt/teleporty" bug). moveToXY honors the exact (x,y) so the
        #   gradual lateral sweep renders continuously. This is safe here: a
        #   handful of adversarial BVs (not the dense CNDE control set), already
        #   guarded above against junction lanes / NaN / large jumps, and
        #   all_checks_on() snaps the vehicle back to lane center on release.
        is_adversarial_trajectory = (
            control_command.command_type == CommandType.TRAJECTORY
        )
        try:
            target_edge, lane_pos, lane_idx = _convert_road(x, y)
            if not target_edge or target_edge.startswith(":"):
                _moveto_log(f"[SKIP] {veh_id}: target ({x:.1f},{y:.1f}) -> junction edge '{target_edge}'")
                return

            # Skip if vehicle is currently on a junction internal lane —
            # let SUMO complete the junction traversal naturally
            current_road = traci.vehicle.getRoadID(veh_id)
            if current_road.startswith(":"):
                _moveto_log(f"[SKIP] {veh_id}: on junction lane {current_road}, letting SUMO traverse")
                return

            # Skip cross-edge moves — moveTo across edge boundaries skips
            # junction internal lanes, corrupting SUMO internal state
            if target_edge != current_road:
                _moveto_log(f"[SKIP] {veh_id}: target edge '{target_edge}' != current '{current_road}'")
                return

            # Skip if target position is near edge end — moveTo near edge
            # boundaries can cause issues as vehicle transitions through junction
            try:
                lane_id = f"{target_edge}_{lane_idx}"
                lane_length = traci.lane.getLength(lane_id)
                if lane_pos > lane_length - 2.0:
                    _moveto_log(f"[SKIP] {veh_id}: lane_pos {lane_pos:.1f} near edge end (len={lane_length:.1f})")
                    return
            except Exception:
                pass

            # Validate lane index
            try:
                target_lane_count = traci.edge.getLaneNumber(target_edge)
            except Exception:
                target_lane_count = -1
            if target_lane_count > 0 and lane_idx >= target_lane_count:
                _moveto_log(f"[SKIP] {veh_id}: lane_idx={lane_idx} >= lane_count={target_lane_count} on {target_edge}")
                return

            if is_adversarial_trajectory:
                # keepRoute=2: free placement at exact (x,y) with the trajectory
                # heading, preserving the sub-lane lateral offset of the swing.
                traci.vehicle.moveToXY(
                    veh_id, target_edge, lane_idx, x, y, angle, 2
                )
            else:
                target_lane_id = f"{target_edge}_{lane_idx}"
                traci.vehicle.moveTo(veh_id, target_lane_id, lane_pos)

        except Exception as e:
            _moveto_log(f"[ERROR] {veh_id}: move failed: {e}")
            logger.warning(f"[CNDE Safety] {veh_id}: move failed ({e}), skipping move")
            return

        traci.vehicle.setPreviousSpeed(veh_id, max(0, speed))
        return {"x": x, "y": y, "speed": speed, "angle": angle}

    @staticmethod
    def execute_lane_change_command(
        veh_id, control_command, obs_dict, first_step=False
    ):
        """Execute the lane change command.

        Args:
            veh_id (str): Vehicle ID.
            control_command (NDECommand): Control command.
            obs_dict (dict): Observation of the ego agent.
            first_step (bool, optional): Flag to indicate if this is the first step. Defaults to False.
        """
        assert (
            control_command.command_type == CommandType.LEFT
            or control_command.command_type == CommandType.RIGHT
        )
        # Defensive check: ensure vehicle still exists before TraCI calls
        if not NDEController._vehicle_exists_static(veh_id):
            logger.warning(f"Vehicle {veh_id} no longer exists in SUMO, skipping lane change control")
            return
        if first_step:  # only execute lane change command once
            indexOffset = 1 if control_command.command_type == CommandType.LEFT else -1
            # merge_demo_aa fix v2: guard against re-issuing changeLaneRelative
            # every tick. When adversity.probability == 1.0, MergeAdversity
            # trigger returns True every tick and produces a NEW NDECommand
            # with first_step=True each time. Without this guard, every 0.1s
            # SUMO restarts the sublane LC motion, causing visible heading
            # jitter ("head flying").
            #
            # v1 attempt (LCA_TRACI flag check via getLaneChangeState) didn't
            # work because the flag is cleared between sublane physics ticks.
            #
            # v2: Python-side timestamp tracking via _lc_active_until module
            # dict. If a previous LC for this vehicle is still within its
            # commanded duration, skip re-issue entirely.
            try:
                sim_t = utils.get_time()
            except Exception:
                sim_t = None
            if sim_t is not None:
                expiry = _lc_active_until.get(veh_id)
                if expiry is not None and sim_t < expiry:
                    # LC still in progress, don't re-issue (would restart sublane motion).
                    return
                _lc_active_until[veh_id] = sim_t + float(control_command.duration)
            # tune4: BV uses LC mode 0 (LCP_ALWAYS) just like AV. The previous
            # mode 256 (LCP_NOOVERLAP) silently denied the LC whenever AV was
            # in overlap range — exactly the scenario where the IS-committed
            # cutin should produce a crash. tune3 data (n=40, 144 cutin commits,
            # 0 attr) showed BV did NOT move laterally because of this silent
            # deny. The crash-rate estimator measures "if we force the
            # adversary command, what's the crash probability" — silent denial
            # by SUMO LC autonomy biases the estimate to 0. With mode 0 the
            # LC fires unconditionally; the resulting collision (or successful
            # avoidance) is the true sample.
            mode_value = 0
            traci.vehicle.setLaneChangeMode(veh_id, mode_value)
            # vis_cutin: FORCE the lateral swing with changeSublane (a direct
            # lateral-offset command) rather than changeLaneRelative. With the BV
            # holding the AV's speed at a steady ~7 m lead, changeLaneRelative was
            # accepted by SUMO's LC-state ('right','sublane') but the sublane
            # planner deferred the actual lateral motion for many seconds (the BV
            # sat in lane 1 ~12 s before finally moving). changeSublane with a
            # high maxSpeedLat commands an immediate, fast lateral displacement of
            # one full lane width toward lane 0 (negative = right). This makes the
            # cut-in execute right away while the BV is still ahead.
            _lc_swing_dur = 0.6
            try:
                _cur_lane = traci.vehicle.getLaneID(veh_id)
                _lane_w = traci.lane.getWidth(_cur_lane)
            except Exception:
                _lane_w = 3.2
            # indexOffset -1 (RIGHT) -> negative lateral offset; +1 (LEFT) -> positive
            _lat_off = indexOffset * float(_lane_w)
            try:
                # fast lateral: cross a full lane in ~_lc_swing_dur seconds
                traci.vehicle.setMaxSpeedLat(veh_id, max(abs(_lat_off) / max(_lc_swing_dur, 0.1), 4.0))
            except Exception:
                pass
            traci.vehicle.changeSublane(veh_id, _lat_off)
            # Also issue the lane-index change so SUMO commits the BV's lane
            # membership to lane 0 (keeps downstream lane bookkeeping correct).
            traci.vehicle.changeLaneRelative(veh_id, indexOffset, _lc_swing_dur)
            try:
                _st = traci.vehicle.getLaneChangeStatePretty(veh_id, indexOffset)
                logger.info(f"[vis_cutin] {veh_id} swing right into AV lane, lat_off={_lat_off:.2f} lcState={_st}")
            except Exception:
                pass
            # vis_cutin PHASE 1: keep the BV at (just below) the AV's speed so it
            # holds its lead and the swing completes while it is still AHEAD and
            # fully clears into lane 0. Do NOT brake here — braking now drops the
            # BV into the AV overlap zone and SUMO re-blocks the lateral move.
            try:
                _av_v = traci.vehicle.getSpeed("AV") if "AV" in traci.vehicle.getIDList() else 8.0
            except Exception:
                _av_v = 8.0
            try:
                traci.vehicle.setSpeed(veh_id, max(_av_v, 7.5))
            except Exception:
                pass
            _cutin_phase[veh_id] = {"t0": sim_t if sim_t is not None else 0.0, "braked": False}
        else:
            # vis_cutin PHASE 2: the swing was issued on first_step. Once the
            # swing time has elapsed AND the BV is on the AV's lane (fully cut
            # in, in front of the AV), hard-brake so the close-following AV
            # cannot avoid rear-ending it. One-shot per commit.
            ph = _cutin_phase.get(veh_id)
            if ph is not None and not ph["braked"]:
                try:
                    now = utils.get_time()
                except Exception:
                    now = None
                if now is not None and (now - ph["t0"]) >= _CUTIN_SWING_TIME:
                    try:
                        bv_lane = traci.vehicle.getLaneID(veh_id)
                        av_lane = traci.vehicle.getLaneID("AV") if "AV" in traci.vehicle.getIDList() else None
                        if av_lane and bv_lane == av_lane:
                            traci.vehicle.setSpeed(veh_id, _CUTIN_BRAKE_SPEED)
                            ph["braked"] = True
                            logger.warning(f"[CUTIN-BRAKE] t={now:.1f} {veh_id} on AV lane {bv_lane} -> brake {_CUTIN_BRAKE_SPEED}")
                    except Exception:
                        pass

    @staticmethod
    def execute_acceleration_command(veh_id, control_command, obs_dict):
        """Execute the acceleration command.

        Args:
            veh_id (str): Vehicle ID.
            control_command (NDECommand): Control command.
            obs_dict (dict): Observation of the ego agent.
        """
        # logger.critical("the acceleration command should not be executed")
        assert control_command.command_type == CommandType.ACCELERATION
        # Defensive check: ensure vehicle still exists before TraCI calls
        if not NDEController._vehicle_exists_static(veh_id):
            logger.warning(f"Vehicle {veh_id} no longer exists in SUMO, skipping acceleration control")
            return
        acceleration = control_command.acceleration
        final_speed = obs_dict["ego"]["velocity"] + acceleration * utils.get_step_size()
        final_speed = 0 if final_speed < 0 else final_speed
        traci.vehicle.setSpeed(veh_id, final_speed)

    def all_checks_on(self, veh_id):
        """Restore full SUMO default control for a vehicle exiting CNDE control.

        Key steps:
        1. Restore SUMO control modes (SpeedMode, LaneChangeMode)
        2. Re-place vehicle on its current lane using moveTo (fixes lane position)
        3. Reroute to original destination
        4. Release speed control

        Args:
            veh_id (str): Vehicle ID.
        """
        if not NDEController._vehicle_exists_static(veh_id):
            self._saved_route = None
            self._saved_destination_edge = None
            self._control_mode = 'sumo'
            return

        # Step 1: Restore SUMO default speed mode and signals
        traci.vehicle.setSpeedMode(veh_id, 31)
        traci.vehicle.setSignals(veh_id, -1)
        # tune10: release any fixed-speed override set during an adversarial
        # cut-in (IS-Adv-Relax setSpeed longitudinal hold). -1 hands speed
        # control back to SUMO's car-following model.
        try:
            traci.vehicle.setSpeed(veh_id, -1)
        except Exception:
            pass

        # Step 2: Hold current lane briefly before restoring full lane-change
        # autonomy. Without this, SUMO may immediately change lanes during the
        # gap between CNDE cycles, and the next cycle's trajectory (still
        # targeting the old lane) causes an implicit lane change → segfault.
        try:
            current_lane = traci.vehicle.getLaneID(veh_id)
            if current_lane and not current_lane.startswith(':'):
                current_lane_idx = int(current_lane.split('_')[-1])
                # Hold lane for 1s — covers the SUMO gap + next CNDE cycle start
                traci.vehicle.changeLane(veh_id, current_lane_idx, duration=1.0)
        except Exception:
            pass
        # Restore default lane-change mode (changeLane duration will still hold)
        traci.vehicle.setLaneChangeMode(veh_id, 1621)

        # Step 3: Snap vehicle back to lane center and smooth speed transition.
        # moveToXY(keepRoute=2) may have left the vehicle offset from the lane
        # center. Without correction, SUMO's first step after takeover produces
        # a visible position jump as it snaps to its internal lane geometry.
        #
        # Junction vehicles are left untouched — they are on internal lanes
        # (`:` prefix) where moveTo/moveToXY can corrupt SUMO state. Junction
        # vehicles should not have been under CNDE control in the first place
        # (filtered out by _should_include_in_cnde_control), so no correction
        # is needed.
        try:
            current_lane = traci.vehicle.getLaneID(veh_id)
            if current_lane and not current_lane.startswith(':'):
                current_lane_pos = traci.vehicle.getLanePosition(veh_id)
                current_speed = traci.vehicle.getSpeed(veh_id)
                # Snap to lane center — fixes lateral offset from moveToXY
                traci.vehicle.moveTo(veh_id, current_lane, current_lane_pos)
                # Don't call setPreviousSpeed here — it only takes a scalar,
                # but SUMO's internal previous speed is a vector. Setting the
                # scalar can cause SUMO to infer a wrong velocity direction,
                # leading to visual heading flips in the GUI.
        except Exception:
            pass  # Best effort

        # Step 3: Reroute to original destination — but only if the new route
        # is compatible with the current lane. If the vehicle is near a stop bar
        # and the new route would require a different lane (no connection from
        # current lane), the vehicle gets permanently stuck. In that case,
        # keep the original route so the vehicle can proceed through the junction.
        # Route-degeneracy guard: the findRoute + setRoute pair below fires every
        # CNDE release cycle (~0.5s sim_t) for each controlled BV. Some
        # interaction between SUMO's setRoute and findRoute when called on a
        # vehicle mid-route accumulates edges into SUMO's internal route
        # storage with a perfect 2^N doubling pattern (observed up to 2^26 =
        # 67M edges). Once the route shows signs of degeneracy, skip the
        # entire reroute block — SUMO continues fine with the existing route
        # and DTM despawns the BV when it leaves the AV's vicinity.
        try:
            # Save current route as fallback
            original_route = traci.vehicle.getRoute(veh_id)
            original_route_idx = traci.vehicle.getRouteIndex(veh_id)

            if len(original_route) > 1000:
                # Route already degenerate — calling findRoute + setRoute here
                # is what feeds the doubling, so do nothing.
                pass
            else:
                # Attempt reroute
                if self._saved_destination_edge is not None:
                    current_edge = traci.vehicle.getRoadID(veh_id)
                    if current_edge.startswith(':'):
                        if original_route_idx + 1 < len(original_route):
                            current_edge = original_route[original_route_idx + 1]
                        elif original_route:
                            current_edge = original_route[min(original_route_idx, len(original_route) - 1)]

                    dest_edge = self._saved_destination_edge
                    route_stage = traci.simulation.findRoute(current_edge, dest_edge)
                    if route_stage.edges:
                        traci.vehicle.setRoute(veh_id, route_stage.edges)
                    else:
                        traci.vehicle.rerouteTraveltime(veh_id)
                else:
                    traci.vehicle.rerouteTraveltime(veh_id)

                # Verify new route is reachable from current lane
                lane_id = traci.vehicle.getLaneID(veh_id)
                if lane_id and not lane_id.startswith(':'):
                    best_lanes = traci.vehicle.getBestLanes(veh_id)
                    for bl in best_lanes:
                        if bl[0] == lane_id:
                            if not bl[4]:  # allowsContinuation = False
                                # New route not reachable from current lane — revert
                                traci.vehicle.setRoute(veh_id, original_route)
                                logger.debug(
                                    f"[all_checks_on] {veh_id}: reroute would strand vehicle "
                                    f"on {lane_id}, reverting to original route"
                                )
                            break
        except Exception:
            pass

        # Keep _saved_destination_edge — don't clear it.
        # The original destination is always valid for rerouting.
        # Only clear _saved_route (the specific edge sequence may be stale).
        self._saved_route = None

        # Step 4: Release speed control — setSpeed(-1) lets SUMO compute speed
        traci.vehicle.setSpeed(veh_id, -1)
        self._control_mode = 'sumo'

    # CNDE speed mode: controls which SUMO safety checks remain active
    # during trajectory control. Bitmask:
    #   Bit 0 (1):  Car-following safe speed    — off (CNDE controls speed)
    #   Bit 1 (2):  Max acceleration limit      — off (CNDE trajectories may exceed)
    #   Bit 2 (4):  Max deceleration limit      — off (CNDE trajectories may exceed)
    #   Bit 3 (8):  Right-of-way at junctions   — ON  (prevent junction conflicts)
    #   Bit 4 (16): Brake hard for red lights   — ON  (prevent red light running)
    #   Bit 5 (32): TraCI speed override        — ON  (needed for moveToXY)
    # = 32 + 16 + 8 = 56
    # Change to 32 if junction safety causes issues with CNDE trajectories.
    # smode=32: all SUMO safety checks off, TraCI override on.
    # smode=56 (32+16+8) was tested but causes executeMove segfault because
    # SUMO's right-of-way logic (bit 3) conflicts with moveToXY-placed vehicles,
    # corrupting internal leader/follower state. Stick with 32 until SUMO fixes this.
    # Junction safety is handled by excluding approaching-edge-end vehicles from
    # CNDE control in _should_include_in_cnde_control() instead.
    CNDE_SPEED_MODE = 32

    def all_checks_off(self, veh_id):
        """Set CNDE control modes and save route info before CNDE takes control.

        Uses CNDE_SPEED_MODE which keeps junction right-of-way and red light
        checks active while disabling car-following and accel/decel limits
        that would conflict with CNDE trajectory control.

        AV (ego vehicle) is NEVER modified — its safety checks must remain on
        at all times to prevent ego-vehicle collisions, regardless of who
        controls it.

        Args:
            veh_id (str): Vehicle ID.
        """
        if veh_id == "AV":
            logger.debug(f"[Safety] AV: all_checks_off skipped — AV safety checks must stay on")
            return

        # Defensive check: ensure vehicle still exists before TraCI calls
        if not NDEController._vehicle_exists_static(veh_id):
            return

        # Save original destination on first-ever takeover (never overwrite)
        if self._saved_destination_edge is None:
            try:
                route = traci.vehicle.getRoute(veh_id)
                if route:
                    self._saved_destination_edge = route[-1]
                    logger.debug(f"[Route Save] {veh_id}: saved original dest={route[-1]}")
            except Exception as e:
                logger.warning(f"[Route Save] {veh_id}: failed to save route: {e}")

        traci.vehicle.setSpeedMode(veh_id, self.CNDE_SPEED_MODE)
        traci.vehicle.setLaneChangeMode(veh_id, 0)
        # Disable turn signal auto-computation during external control.
        # moveToXY(keepRoute=2) can place vehicles on lanes inconsistent with
        # their SUMO route, causing SUMO to display wrong turn signals (e.g.
        # left blinker on a right-turn lane) based on stale route info.
        traci.vehicle.setSignals(veh_id, 0)


def interpolate_control_command(control_command, obs_dict):
    """Interpolate the control command.

    Args:
        control_command (NDECommand): Control command.
        obs_dict (dict): Observation of the ego agent.

    Returns:
        NDECommand: Interpolated control command.
    """
    if control_command.command_type in (CommandType.TRAJECTORY, CommandType.CNDE_TRAJECTORY):
        # 1. interpolate the trajectory
        future_trajectory_array = np.array(control_command.future_trajectory)
        # 1.1 check first point of the trajectory, if it is not the current position, add it
        trajectory_array = np.vstack(
            (
                np.array(
                    [
                        obs_dict["ego"]["position"][0],
                        obs_dict["ego"]["position"][1],
                        obs_dict["ego"]["heading"],
                        obs_dict["ego"]["velocity"],
                        0,
                    ]
                ),
                future_trajectory_array,
            )
        )
        # 1.2 clear the time of the trajectory, start from 0, with time resolution which is equal to the time resolution of the control command 
        trajectory_array[:, -1] = np.array(
            [i*control_command.time_resolution for i in range(len(trajectory_array))]
        )
        # 1.3 interpolate the trajectory
        interpolated_trajecotory = interpolate_future_trajectory(trajectory_array, traci.simulation.getDeltaT())
        # 1.4 update the time of the trajectory
        interpolated_trajecotory[:, -1] += traci.simulation.getTime()
        control_command.future_trajectory = interpolated_trajecotory[1:]  # TODO: Angle cannot be interpolated
        return control_command
    else:
        return control_command
