from addict import Dict
from collections import deque
from loguru import logger
import numpy as np
import os
import sys

from terasim.envs.template_complete import EnvTemplateComplete
from terasim.overlay import traci
from terasim.params import AgentType
from terasim_nde_nade.utils.base.types import CommandType
import terasim.utils as utils


class NDE(EnvTemplateComplete):
    def __init__(
        self,
        vehicle_factory,
        vru_factory,
        info_extractor,
        warmup_time_lb=900,
        warmup_time_ub=1200,
        run_time=300,
        log_flag=False,
        log_dir=None,
        configuration=None,
        continue_on_collision=False,
        cnde_diag_logs=True,
        *args,
        **kwargs,
    ):
        """Initialize the NDE environment.

        Args:
            vehicle_factory (VehicleFactory): Vehicle factory.
            vru_factory (VulnerableRoadUserFactory): Vulnerable road user factory.
            info_extractor (InfoExtractor): Information extractor.
            warmup_time_lb (int, optional): Lower bound of warmup time. Defaults to 900.
            warmup_time_ub (int, optional): Upper bound of warmup time. Defaults to 1200.
            run_time (int, optional): Running time. Defaults to 300.
            log_flag (bool, optional): Log flag. Defaults to False.
            log_dir (str, optional): Log directory. Defaults to None.
            configuration (dict, optional): Configuration. Defaults to None.
            cnde_diag_logs (bool, optional): Write the per-step CNDE diagnostic jsonl logs
                (heading tracker, pre-executeMove audit). Defaults to True.
            continue_on_collision (bool, optional): If True, do NOT terminate the
                simulation on collisions; only stop on run_time timeout. Useful
                for no-AV BV-vs-BV runs where we want to accumulate multiple
                collisions across the full run_time. Defaults to False (legacy
                behavior: stop on first collision).
        """
        rng = np.random.default_rng()
        self.warmup_time = int(rng.integers(low=warmup_time_lb, high=warmup_time_ub))
        self.run_time = run_time
        logger.info(f"warmup_time: {self.warmup_time}, run_time: {self.run_time}")
        self.final_log = None
        self.log_dir = log_dir
        if self.log_dir:
            os.makedirs(self.log_dir, exist_ok=True)
        self.log_flag = log_flag
        self.tls_info_cache = {}
        self.history_length = 10
        self.record = Dict()
        self.step_epsilon = 1.0
        self.step_weight = 1.0
        self.excluded_agent_set = set()
        self.configuration = configuration
        self.continue_on_collision = bool(continue_on_collision)
        # Per-step CNDE diagnostics (output/heading_tracker.jsonl, output/cnde_audit.jsonl and, in
        # NNDE, output/cnde_trajectory_log.jsonl): several GB per long run, written for replay /
        # verification tooling. Kept on by default; evaluation pipelines switch them off.
        self.cnde_diag_logs = bool(cnde_diag_logs)
        if self.continue_on_collision:
            logger.info("NDE: continue_on_collision=True -> collisions will NOT terminate simulation")
        # Cumulative counters for collisions seen during a run (only meaningful
        # when continue_on_collision is True; otherwise the run ends on the
        # first collision and these stay at 0/1 respectively).
        self._collision_count_vehicle_vehicle = 0
        self._collision_count_vehicle_vru = 0
        # Per-step latch so we count a collision event once (SUMO can report
        # the same colliding pair across consecutive ticks).
        self._last_collision_signature = None
        super().__init__(vehicle_factory, vru_factory, info_extractor, *args, **kwargs)

    def on_start(self, ctx):
        """Start the simulation of NDE including simulation warm up.

        Args:
            ctx (dict): Simulation context information.

        Returns:
            bool: Flag to indicate if the simulation is started successfully.
        """
        self.sumo_warmup(self.warmup_time)
        self._remove_congested_vehicles()
        return super().on_start(ctx)

    def _remove_congested_vehicles(self, waiting_threshold=180.0):
        """Remove vehicles that have been waiting (speed~0) for too long.

        Called once after warmup to clear initial congestion.
        Vehicles in excluded_agent_set (e.g., AV) are never removed.

        Args:
            waiting_threshold (float): Seconds of accumulated waiting time
                before a vehicle is removed. Defaults to 180.
        """
        removed = []
        for veh_id in traci.vehicle.getIDList():
            if veh_id in self.excluded_agent_set:
                continue
            if traci.vehicle.getAccumulatedWaitingTime(veh_id) > waiting_threshold:
                try:
                    traci.vehicle.remove(veh_id)
                    removed.append(veh_id)
                except Exception:
                    pass
        if removed:
            logger.info(f"Removed {len(removed)} congested vehicles after warmup")

    def get_env_observation(self, ctx):
        """Get the observation of the environment.

        Returns:
            dict: Observation of the environment.
        """
        env_observation = {
            AgentType.VEHICLE: {},
            AgentType.VULNERABLE_ROAD_USER: {},
            AgentType.TRAFFIC_LIGHT: {},
        }
        if "terasim_controlled_vehicle_ids" in ctx:
            env_observation[AgentType.VEHICLE] = {
                veh.id: veh.observation
                for veh in self.vehicle_list.values()
                if veh.id in ctx["terasim_controlled_vehicle_ids"]
            }
        else:
            env_observation[AgentType.VEHICLE] = {
                veh.id: veh.observation for veh in self.vehicle_list.values()
            }
        if "terasim_controlled_vulnerable_road_user_ids" in ctx:
            env_observation[AgentType.VULNERABLE_ROAD_USER] = {
                vru.id: vru.observation
                for vru in self.vulnerable_road_user_list.values()
                if vru.id in ctx["terasim_controlled_vulnerable_road_user_ids"]
            }
        else:
            env_observation[AgentType.VULNERABLE_ROAD_USER] = {
                vru.id: vru.observation for vru in self.vulnerable_road_user_list.values()
            }
        env_observation[AgentType.TRAFFIC_LIGHT] = {
            tls_id: self.tls_info_cache[tls_id][-1]
            for tls_id in self.tls_info_cache.keys()
        }
        return env_observation

    def _executing_trajectory(self, veh_id):
        """True while ``veh_id``'s controller is executing a (CNDE) trajectory command."""
        if veh_id not in self.vehicle_list:
            return False
        controller = self.vehicle_list[veh_id].controller
        return bool(
            getattr(controller, 'is_busy', False)
            and getattr(controller, 'cached_control_command', None) is not None
            and hasattr(controller.cached_control_command, 'cached_command')
            and controller.cached_control_command.cached_command.command_type
            in (CommandType.TRAJECTORY, CommandType.CNDE_TRAJECTORY)
        )

    def _remediate_trajectory_vehicles(self, trajectory_veh_ids):
        """The remediation of the pre-executeMove audit, without the audit record.

        Same checks, same order, same actions as the audit block of :meth:`executeMove`:
        a trajectory vehicle inside a junction gets speed mode 31 back, and one whose route
        index ran past its route, or that sits on an edge outside its route, is removed; a
        TraCI error on the vehicle removes it as well.
        """
        for vid in list(trajectory_veh_ids):
            if vid not in self._cached_sumo_ids:
                continue
            try:
                road = traci.vehicle.getRoadID(vid)
                smode = traci.vehicle.getSpeedMode(vid)
                route = traci.vehicle.getRoute(vid)
                ridx = traci.vehicle.getRouteIndex(vid)
                is_junc = road.startswith(':')
                if is_junc and smode != 31:
                    traci.vehicle.setSpeedMode(vid, 31)
                if route and ridx >= len(route):
                    traci.vehicle.remove(vid)
                    continue
                if not is_junc and route and road not in route:
                    traci.vehicle.remove(vid)
                    continue
            except Exception:
                try:
                    traci.vehicle.remove(vid)
                except Exception:
                    pass

    def executeMove(self, ctx, env_command_information=None, env_observation=None):
        """Execute the move of the environment, i.e., moving forward the SUMO simulation by half step and updating all the vehicles and vrus.
        Some of the vehicles and vrus may leave or enter the simulation.

        Args:
            ctx (dict): Simulation context information.
            env_command_information (dict, optional): Command information of the environment. Defaults to None.
            env_observation (dict, optional): Observation of the environment. Defaults to None.

        Returns:
            dict: Updated command information of the environment.
            dict: Updated observation of the environment.
            bool: Flag to indicate if the simulation should continue.
        """
        # Cache vehicle ID list once per step to avoid repeated getIDList() calls
        # over TraCI TCP (each call is a socket round-trip). Used by AUDIT and
        # can be accessed by controllers via env reference.
        self._cached_sumo_ids = set(traci.vehicle.getIDList())

        # Move half step forward, update all vehicles and vrus (some of them may leave or enter the simulation)
        if self.cnde_enabled:
            # CNDE mode: collect trajectory-controlled vehicles (already have moveToXY + setPreviousSpeed set;
            # calling setSpeed(-1) on them would create contradictory state and cause segfault).
            trajectory_veh_ids = set()
            if env_command_information and AgentType.VEHICLE in env_command_information:
                for vid, cmd in env_command_information[AgentType.VEHICLE].items():
                    if hasattr(cmd, 'command_type') and cmd.command_type in (CommandType.TRAJECTORY, CommandType.CNDE_TRAJECTORY):
                        trajectory_veh_ids.add(vid)

            # Also identify vehicles still under CNDE trajectory control from the
            # previous step. These have SpeedMode=32, LaneChangeMode=0, and were
            # positioned by moveToXY(keepRoute=2). Calling setSpeed(-1) on them
            # before executeMove() creates contradictory SUMO internal state
            # (speed computation on a non-route edge with modified safety checks)
            # that corrupts C++ leader/follower data and causes segfault.
            for veh in self.vehicle_list.values():
                controller = veh.controller
                if (hasattr(controller, 'is_busy') and controller.is_busy
                        and controller.cached_control_command is not None
                        and hasattr(controller.cached_control_command, 'cached_command')
                        and controller.cached_control_command.cached_command.command_type in (CommandType.TRAJECTORY, CommandType.CNDE_TRAJECTORY)):
                    trajectory_veh_ids.add(veh.id)

            # Force-release vehicles that are still under CNDE trajectory control
            # (is_busy + CNDE_TRAJECTORY) but have been excluded from cnde_control_ids
            # by the filter. Without this, the cached trajectory continues executing
            # for up to 0.5s with smode=32, potentially pushing the vehicle through
            # a stop bar or into a junction without safety checks.
            cnde_control_ids = set(self.control_ls.get('cnde_control_ids', []))
            released_veh_ids = set()
            for veh in self.vehicle_list.values():
                controller = veh.controller
                if (hasattr(controller, 'is_busy') and controller.is_busy
                        and controller.cached_control_command is not None
                        and hasattr(controller.cached_control_command, 'cached_command')
                        and controller.cached_control_command.cached_command.command_type in (CommandType.TRAJECTORY, CommandType.CNDE_TRAJECTORY)
                        and veh.id not in cnde_control_ids
                        and veh.id not in self.excluded_agent_set):
                    controller.is_busy = False
                    controller.cached_control_command = None
                    controller.all_checks_on(veh.id)
                    # Released: SUMO drives it from here on, with the default speed mode
                    # all_checks_on just set. The junction right-of-way block below must
                    # skip it, or it gets mode 56 for this move and then the CNDE mode 32
                    # "restored" -- leaving a SUMO-driven vehicle a few metres before the
                    # stop line at mode 32 for good: no red-light braking, no right of
                    # way, no speed limit (SUMO 1.23).
                    released_veh_ids.add(veh.id)

            # Only touch vehicles that need speed intervention.
            # DO NOT call setSpeed(-1) every step on all control_radius vehicles —
            # it disrupts SUMO's internal junction yielding logic (deceleration
            # curves get reset each step, so left-turn vehicles never slow down
            # to yield to opposing through traffic).
            control_radius_ids = set(self.control_ls.get('control_radius_vehicle_ids', []))
            static_adv_ids = set(ctx.get("static_adversarial_object_id_list", []))
            for veh_id in control_radius_ids:
                if veh_id in static_adv_ids:
                    traci.vehicle.setSpeed(veh_id, 0)
        # NDE mode: no CNDE trajectory commands were issued, so no speed resets are needed.
        # SUMO controls all vehicles naturally.

        _exec_time = traci.simulation.getTime()
        _n_traj = len(trajectory_veh_ids) if self.cnde_enabled else 0

        # Pre-executeMove audit: dump full TraCI state to crash-safe JSONL.
        # Only checks trajectory-controlled vehicles (typically 1-10).
        if self.cnde_enabled and trajectory_veh_ids and self.cnde_diag_logs:
            import json as _json
            if not hasattr(self, '_audit_file'):
                _audit_path = getattr(self, 'log_dir', None) or 'output'
                from pathlib import Path
                _audit_path = Path(_audit_path) / "cnde_audit.jsonl"
                _audit_path.parent.mkdir(parents=True, exist_ok=True)
                self._audit_file = open(_audit_path, "w")

            for vid in list(trajectory_veh_ids):
                if vid not in self._cached_sumo_ids:
                    self._audit_file.write(_json.dumps({"t": _exec_time, "vid": vid, "status": "GONE"}) + "\n")
                    continue
                try:
                    pos = traci.vehicle.getPosition(vid)
                    spd = traci.vehicle.getSpeed(vid)
                    angle = traci.vehicle.getAngle(vid)
                    accel = traci.vehicle.getAcceleration(vid)
                    road = traci.vehicle.getRoadID(vid)
                    lane = traci.vehicle.getLaneID(vid)
                    lanepos = traci.vehicle.getLanePosition(vid)
                    smode = traci.vehicle.getSpeedMode(vid)
                    lcmode = traci.vehicle.getLaneChangeMode(vid)
                    route = traci.vehicle.getRoute(vid)
                    ridx = traci.vehicle.getRouteIndex(vid)
                    is_junc = road.startswith(':')

                    lane_len = -1
                    try:
                        lane_len = traci.lane.getLength(lane)
                    except Exception:
                        pass
                    edge_end_dist = lane_len - lanepos if lane_len > 0 else -1

                    # Leader: who's directly ahead and gap distance
                    leader_id, leader_gap = "", -1
                    try:
                        leader_info = traci.vehicle.getLeader(vid, dist=100)
                        if leader_info and leader_info[0]:
                            leader_id, leader_gap = leader_info[0], round(leader_info[1], 2)
                    except Exception:
                        pass

                    on_route = road in route if route and not is_junc else "junc" if is_junc else "no_route"

                    rec = {
                        "t": round(_exec_time, 1), "vid": vid,
                        "x": round(pos[0], 2), "y": round(pos[1], 2),
                        "spd": round(spd, 2), "ang": round(angle, 2), "acc": round(accel, 2),
                        "road": road, "lane": lane,
                        "lpos": round(lanepos, 1), "lane_len": round(lane_len, 1),
                        "edge_end": round(edge_end_dist, 1),
                        "smode": smode, "lcmode": lcmode,
                        "ridx": ridx, "route_len": len(route),
                        "on_route": on_route, "junc": is_junc,
                        "leader": leader_id, "leader_gap": leader_gap,
                    }
                    self._audit_file.write(_json.dumps(rec) + "\n")
                    self._audit_file.flush()

                    # Remediation: fix dangerous states before executeMove
                    if is_junc and smode != 31:
                        traci.vehicle.setSpeedMode(vid, 31)
                        self._audit_file.write(_json.dumps({"t": _exec_time, "vid": vid, "fix": "junc_smode_reset"}) + "\n")

                    if route and ridx >= len(route):
                        self._audit_file.write(_json.dumps({"t": _exec_time, "vid": vid, "fix": "routeIdx_OOB_remove"}) + "\n")
                        traci.vehicle.remove(vid)
                        continue

                    if not is_junc and route and road not in route:
                        self._audit_file.write(_json.dumps({"t": _exec_time, "vid": vid, "fix": "off_route_remove"}) + "\n")
                        traci.vehicle.remove(vid)
                        continue

                except Exception as e:
                    self._audit_file.write(_json.dumps({"t": _exec_time, "vid": vid, "error": str(e)}) + "\n")
                    self._audit_file.flush()
                    try:
                        traci.vehicle.remove(vid)
                    except Exception:
                        pass
        elif self.cnde_enabled and trajectory_veh_ids:
            # Diagnostic logs off: no audit record, but the same remediation -- it changes the
            # simulation, so it must not depend on whether the logs are written.
            self._remediate_trajectory_vehicles(trajectory_veh_ids)

        # Junction right-of-way enforcement for CNDE-controlled vehicles.
        # smode=32 disables all SUMO safety checks including right-of-way.
        # For vehicles approaching a junction, temporarily enable right-of-way
        # + red light checks before executeMove.
        JUNCTION_ROW_DISTANCE = 20.0
        _junction_row_restored = []
        if self.cnde_enabled and trajectory_veh_ids:
            for vid in list(trajectory_veh_ids):
                if vid not in self._cached_sumo_ids or vid in released_veh_ids:
                    continue
                try:
                    road = traci.vehicle.getRoadID(vid)
                    if road.startswith(':'):
                        continue
                    lane = traci.vehicle.getLaneID(vid)
                    lane_pos = traci.vehicle.getLanePosition(vid)
                    lane_len = traci.lane.getLength(lane)
                    dist_to_end = lane_len - lane_pos
                    if dist_to_end < JUNCTION_ROW_DISTANCE:
                        traci.vehicle.setSpeedMode(vid, 56)
                        _junction_row_restored.append(vid)
                except Exception:
                    pass

        # executeMove error handling
        import re
        _max_retries = 5
        for _retry in range(_max_retries):
            try:
                traci.simulation.executeMove()
                break
            except Exception as e:
                msg = str(e)
                etype = type(e).__name__

                if "has no valid route" in msg:
                    m = re.search(r"Vehicle '([^']+)'", msg)
                    if m:
                        bad_veh = m.group(1)
                        logger.warning(f"[executeMove] Removing vehicle with broken route: {bad_veh}")
                        try:
                            traci.vehicle.remove(bad_veh)
                        except Exception:
                            pass
                        continue

                # FatalTraCIError = SUMO subprocess crashed (TCP mode only).
                # Log the crash context and remove all CNDE-controlled vehicles
                # to clear the corrupted state, then retry.
                if "Fatal" in etype or "fatal" in msg.lower() or "connection" in msg.lower():
                    logger.error(
                        f"[executeMove CRASH] {etype}: {msg} (retry {_retry+1}/{_max_retries})")

                    # Dump AUDIT state to file if available
                    if hasattr(self, '_audit_file'):
                        import json as _json
                        self._audit_file.write(_json.dumps({
                            "t": _exec_time,
                            "event": "EXECUTEMOVE_CRASH",
                            "error": f"{etype}: {msg}",
                            "retry": _retry + 1,
                            "trajectory_veh_ids": list(trajectory_veh_ids) if trajectory_veh_ids else [],
                        }) + "\n")
                        self._audit_file.flush()

                    # Remove all trajectory-controlled vehicles to clear corrupted state
                    if trajectory_veh_ids:
                        for vid in list(trajectory_veh_ids):
                            try:
                                traci.vehicle.remove(vid)
                                logger.warning(f"[executeMove CRASH] Removed trajectory vehicle {vid}")
                            except Exception:
                                pass
                        trajectory_veh_ids.clear() if hasattr(trajectory_veh_ids, 'clear') else None
                    continue

                # Unknown error — log + try-and-recover instead of crashing.
                # Earlier runs hit a libsumo executeMove segfault that the OS
                # could not recover from, but Python-side exceptions are common
                # too (e.g. corrupted vehicle state from CNDE moveToXY). Removing
                # trajectory-controlled vehicles often clears SUMO's internal
                # state and lets the sim continue rather than ending the run.
                logger.error(
                    f"[executeMove UNKNOWN] {etype}: {msg} (retry {_retry+1}/{_max_retries})")
                if trajectory_veh_ids:
                    for vid in list(trajectory_veh_ids):
                        try:
                            traci.vehicle.remove(vid)
                        except Exception: pass
                    trajectory_veh_ids.clear() if hasattr(trajectory_veh_ids, 'clear') else None
                continue
        else:
            logger.error(f"[executeMove] Failed after {_max_retries} retries, giving up")

        # Restore CNDE speed mode for vehicles that had right-of-way temporarily enabled.
        # Only a vehicle still executing a trajectory goes back to the CNDE mode; any other
        # (no longer busy) gets SUMO's default 31 rather than a mode nobody resets.
        if self.cnde_enabled:
            for vid in _junction_row_restored:
                try:
                    if vid in traci.vehicle.getIDList():
                        traci.vehicle.setSpeedMode(
                            vid, 32 if self._executing_trajectory(vid) else 31
                        )  # 32 = CNDE_SPEED_MODE
                except Exception:
                    pass

        self._maintain_all_vehicles(ctx)
        self._maintain_all_vulnerable_road_users(ctx)

        # --- Heading tracker: log angle/speed for control_radius vehicles ---
        # Detects sudden heading flips (>30° in one step = 0.1s)
        if self.cnde_diag_logs:
            self._track_vehicle_headings(_exec_time)

        existing_vehicle_list = traci.vehicle.getIDList()
        existing_vru_list = traci.person.getIDList()

        env_command_information = {
            AgentType.VEHICLE: (
                {
                    veh_id: env_command_information[AgentType.VEHICLE][veh_id]
                    for veh_id in env_command_information[AgentType.VEHICLE]
                    if veh_id in existing_vehicle_list
                }
                if env_command_information[AgentType.VEHICLE]
                else {}
            ),
            AgentType.VULNERABLE_ROAD_USER: (
                {
                    vru_id: env_command_information[AgentType.VULNERABLE_ROAD_USER][vru_id]
                    for vru_id in env_command_information[AgentType.VULNERABLE_ROAD_USER]
                    if vru_id in existing_vru_list
                }
                if env_command_information[AgentType.VULNERABLE_ROAD_USER]
                else {}
            ),
        }
        env_observation = {
            AgentType.VEHICLE: (
                {
                    veh_id: env_observation[AgentType.VEHICLE][veh_id]
                    for veh_id in env_observation[AgentType.VEHICLE]
                    if veh_id in existing_vehicle_list
                }
                if env_observation[AgentType.VEHICLE]
                else {}
            ),
            AgentType.VULNERABLE_ROAD_USER: (
                {
                    vru_id: env_observation[AgentType.VULNERABLE_ROAD_USER][vru_id]
                    for vru_id in env_observation[AgentType.VULNERABLE_ROAD_USER]
                    if vru_id in existing_vru_list
                }
                if env_observation[AgentType.VULNERABLE_ROAD_USER]
                else {}
            ),
            AgentType.TRAFFIC_LIGHT: env_observation[AgentType.TRAFFIC_LIGHT],
        }
        return env_command_information, env_observation, self.should_continue_simulation()

    def _track_vehicle_headings(self, sim_time):
        """Track heading/speed/mode of all control_radius vehicles every step.

        Logs to output/heading_tracker.jsonl. Records full state for every
        vehicle, with special annotations for:
        - mode_switch: CNDE↔SUMO control transition detected
        - angle_jump: heading changed >30° in one step (0.1s)
        """
        import json as _json

        if not hasattr(self, '_heading_tracker_file'):
            _path = getattr(self, 'log_dir', None) or 'output'
            from pathlib import Path
            _path = Path(_path) / "heading_tracker.jsonl"
            _path.parent.mkdir(parents=True, exist_ok=True)
            self._heading_tracker_file = open(_path, "w")
            self._heading_prev = {}  # veh_id -> {angle, x, y, speed, mode}

        # Track ALL vehicles in cache_radius (large circle), not just control_radius
        tracked_ids = set(self.control_ls.get('terasim_controlled_vehicle_ids', []))
        if not tracked_ids:
            return

        # --- Visual heading smoothing (merge_demo_aa BV junction jitter fix) ---
        # Compute per-tick rate-limited smoothed angles for all tracked vehicles.
        # When SUMO's internal junction lane geometry causes |Δangle|>15° in one
        # 0.1s step (real-world Ann Arbor OSM has degenerate 0.20m connector
        # edges between overlapping junctions, e.g. :29314517_3_1 where bv_ego's
        # x bounces ~7m back-and-forth over consecutive ticks producing 150°+
        # angle flips), apply EMA-style rate-limiting capped at 15°/tick.
        # max_step_deg=15° smooths junction-snap artifacts without throttling
        # intentional lane-change steering (which never exceeds ~10°/tick at
        # 0.1s timestep for SUMO sublane motion).
        # SUMO TraCI has no setAngle and moveToXY is silently ignored on
        # internal junction lanes, so smoothing is applied at the log layer:
        # heading_tracker.jsonl (consumed by heading_tracker_to_fcd.py for
        # replay GUI and by all downstream analytics) records the smoothed
        # value. SUMO physics / collision detection are untouched.
        from ..vehicle.nde_controller import NDEController as _NDEC
        _smoothed = {}
        for veh_id in tracked_ids:
            sa = _NDEC._smooth_angle(veh_id, max_step_deg=15.0)
            if sa is not None:
                _smoothed[veh_id] = sa

        for veh_id in tracked_ids:
            try:
                pos = traci.vehicle.getPosition(veh_id)
                # Use smoothed angle (falls back to raw if smoother unavailable)
                angle = _smoothed.get(veh_id, traci.vehicle.getAngle(veh_id))
                speed = traci.vehicle.getSpeed(veh_id)
                smode = traci.vehicle.getSpeedMode(veh_id)
                road = traci.vehicle.getRoadID(veh_id)

                mode = "cnde" if smode == 32 else "sumo"

                lcmode = traci.vehicle.getLaneChangeMode(veh_id)
                lane_id = traci.vehicle.getLaneID(veh_id)

                # Compare with previous step
                angle_jump = None
                mode_switch = None
                prev = self._heading_prev.get(veh_id)
                if prev is not None:
                    diff = abs(angle - prev["angle"]) % 360
                    if diff > 180:
                        diff = 360 - diff
                    if diff > 10:
                        angle_jump = round(diff, 1)
                    if mode != prev["mode"]:
                        mode_switch = f"{prev['mode']}->{mode}"

                self._heading_prev[veh_id] = {
                    "angle": angle, "x": pos[0], "y": pos[1],
                    "speed": speed, "mode": mode,
                }

                rec = {
                    "t": round(sim_time, 1),
                    "vid": veh_id,
                    "x": round(pos[0], 2),
                    "y": round(pos[1], 2),
                    "angle": round(angle, 2),
                    "speed": round(speed, 2),
                    "mode": mode,
                    "smode": smode,
                    "lcmode": lcmode,
                    "road": road,
                    "lane": lane_id,
                }
                if angle_jump is not None:
                    rec["angle_jump"] = angle_jump
                if mode_switch is not None:
                    rec["mode_switch"] = mode_switch

                self._heading_tracker_file.write(_json.dumps(rec) + "\n")
            except Exception:
                continue

        # --- Also log pedestrians (persons) near the AV so the VRU adversities
        # (jaywalking / running-red-light / stop-crossing) are visible in the
        # replay.  The vehicle loop above records vehicles only, so peds were
        # previously absent from heading_tracker.jsonl and thus invisible in the
        # replay GUI even though their adversities committed during the run.
        try:
            _av_pos = None
            for _cand in ("AV", "CAV"):
                if _cand in traci.vehicle.getIDList():
                    _av_pos = traci.vehicle.getPosition(_cand)
                    break
            for _pid in traci.person.getIDList():
                try:
                    _pp = traci.person.getPosition(_pid)
                    if _av_pos is not None and (
                        (_pp[0] - _av_pos[0]) ** 2 + (_pp[1] - _av_pos[1]) ** 2 > 150.0 ** 2
                    ):
                        continue
                    self._heading_tracker_file.write(_json.dumps({
                        "t": round(sim_time, 1),
                        "vid": _pid,
                        "x": round(_pp[0], 2),
                        "y": round(_pp[1], 2),
                        "angle": round(traci.person.getAngle(_pid), 2),
                        "speed": round(traci.person.getSpeed(_pid), 2),
                        "ptype": "ped",
                        "road": traci.person.getRoadID(_pid),
                    }) + "\n")
                except Exception:
                    continue
        except Exception:
            pass

        # Flush periodically (every 1s = every 10 steps)
        if int(sim_time * 10) % 10 == 0:
            self._heading_tracker_file.flush()

        # Cleanup stale entries
        self._heading_prev = {k: v for k, v in self._heading_prev.items() if k in tracked_ids}

    def cache_history_tls_data(self, focus_tls_ids=None):
        """Cache the history traffic light state data.

        Args:
            focus_tls_ids (list, optional): List of traffic light ids to focus on. Defaults to None.
        """
        if not focus_tls_ids:
            focus_tls_ids = traci.trafficlight.getIDList()

        # _tls_static_cache holds (program_id, control_link_list, logic) per TLS.
        # getControlledLinks and getCompleteRedYellowGreenDefinition return static data
        # (topology never changes mid-run). Only re-fetch if the program ID switches.
        if not hasattr(self, '_tls_static_cache'):
            self._tls_static_cache = {}

        for tls_id in focus_tls_ids:
            if tls_id not in self.tls_info_cache:
                self.tls_info_cache[tls_id] = deque(maxlen=self.history_length + 1)
            current_time = traci.simulation.getTime()
            tls_state = traci.trafficlight.getRedYellowGreenState(tls_id)
            tl_phase = traci.trafficlight.getPhase(tls_id)
            tl_spent_duration = traci.trafficlight.getSpentDuration(tls_id)
            tl_program = traci.trafficlight.getProgram(tls_id)

            cached = self._tls_static_cache.get(tls_id)
            if cached is None or cached[0] != tl_program:
                # First call or program switched — fetch the two static queries
                tl_control_link_list = traci.trafficlight.getControlledLinks(tls_id)
                tl_logic = None
                for tl_logic_candidate in traci.trafficlight.getCompleteRedYellowGreenDefinition(tls_id):
                    if tl_logic_candidate.programID == tl_program:
                        tl_logic = tl_logic_candidate
                        break
                if tl_logic is None:
                    raise ValueError(f"No logic found for program {tl_program} of traffic light {tls_id}")
                self._tls_static_cache[tls_id] = (tl_program, tl_control_link_list, tl_logic)
            else:
                tl_control_link_list = cached[1]
                tl_logic = cached[2]

            self.tls_info_cache[tls_id].append((current_time, tls_state, tl_control_link_list, tl_phase, tl_logic, tl_spent_duration))

    def sumo_warmup(self, warmup_time):
        """Warm up the SUMO simulation.

        Args:
            warmup_time (float): Warm up time.
        """
        # TODO: change vehicle type during the warmup time (might make warmup time longer)
        while True:
            while True:
                traci.simulationStep()
                if traci.simulation.getTime() > warmup_time:
                    break
            if traci.vehicle.getIDCount() > 2500:
                logger.warning(
                    f"Too many vehicles in the simulation: {traci.vehicle.getIDCount()}, Restarting..."
                )
                traci.load(self.simulator.sumo_cmd[1:])
            else:
                break
        self.record.warmup_vehicle_num = traci.vehicle.getIDCount()
        self._vehicle_in_env_distance("before")

    def on_step(self, ctx):
        """Step the simulation of NDE.

        Args:
            ctx (dict): Simulation context information.

        Returns:
            bool: Flag to indicate if the simulation should continue.
        """
        self._fix_wrong_lane_vehicles()
        control_cmds, infos = self.make_decisions(ctx)
        self.refresh_control_commands_state()
        self.execute_control_commands(control_cmds)
        return self.should_continue_simulation()

    def _fix_wrong_lane_vehicles(self, check_interval=50):
        """Periodically fix vehicles stuck on wrong lanes.

        Checks every check_interval steps (~5s). For each vehicle in
        vehicle_list, if getBestLanes says allowsContinuation=False:
        1. Reroute to find a new valid path from current edge
        2. If still on wrong lane after reroute, moveTo the best lane
           so the vehicle can actually follow the new route

        Vehicles in excluded_agent_set (e.g., AV) are skipped.

        Args:
            check_interval: run every N simulation steps.
        """
        if not hasattr(self, '_wrong_lane_counter'):
            self._wrong_lane_counter = 0
        self._wrong_lane_counter += 1
        if self._wrong_lane_counter % check_interval != 0:
            return

        fixed = 0
        for veh_id in list(self.vehicle_list.keys()):
            if veh_id in self.excluded_agent_set:
                continue
            try:
                lane_id = traci.vehicle.getLaneID(veh_id)
                if lane_id.startswith(':'):
                    continue

                best_lanes = traci.vehicle.getBestLanes(veh_id)
                current_ok = True
                for bl in best_lanes:
                    if bl[0] == lane_id:
                        if not bl[4]:  # allowsContinuation = False
                            current_ok = False
                        break

                if current_ok:
                    continue

                # Step 1: Reroute to find a new path from current edge
                traci.vehicle.rerouteTraveltime(veh_id)

                # Step 2: Check if reroute fixed it; if not, move to best lane
                best_lanes = traci.vehicle.getBestLanes(veh_id)
                best_lane_id = None
                best_length = -1
                for bl in best_lanes:
                    if bl[4] and bl[3] > best_length:  # allowsContinuation and longest usable distance
                        best_lane_id = bl[0]
                        best_length = bl[3]

                if best_lane_id and best_lane_id != lane_id:
                    lane_pos = traci.vehicle.getLanePosition(veh_id)
                    traci.vehicle.moveTo(veh_id, best_lane_id, lane_pos)

                fixed += 1
            except Exception:
                continue

        if fixed > 0:
            logger.info(f"[Wrong Lane Fix] Fixed {fixed} vehicles (reroute + lane move)")

    def refresh_control_commands_state(self):
        """Refresh the controlling status of all agents.
        """
        current_time = traci.simulation.getTime()
        for veh_id in self.vehicle_list.keys():
            self.vehicle_list[veh_id].controller._update_controller_status(
                veh_id, current_time
            )
        for vru_id in self.vulnerable_road_user_list.keys():
            self.vulnerable_road_user_list[vru_id].controller._update_controller_status(
                vru_id, current_time
            )

    def _vehicle_in_env_distance(self, mode):
        """Record the distance of all vehicles in the environment.

        Args:
            mode (str): Mode of the recording.
        """
        veh_id_list = traci.vehicle.getIDList()
        distance_dist = self._get_distance(veh_id_list)

    def _get_distance(self, veh_id_list):
        """Get the distance of all vehicles in the environment.

        Args:
            veh_id_list (list): List of vehicle ids.

        Returns:
            dict: Distance of all vehicles in the environment.
        """
        distance_dist = {veh_id: utils.get_distance(veh_id) for veh_id in veh_id_list}
        return distance_dist

    def should_continue_simulation(self):
        """Determine if the simulation should continue.

        Default behavior (continue_on_collision=False): stop on first collision
        or when run_time is exceeded.

        When continue_on_collision=True: NEVER terminate on collisions. Each
        observed collision still updates self.record (last collision wins) and
        increments self._collision_count_*; the run ends only when the warmup
        + run_time budget is exhausted. Used by the no-AV BV-vs-BV runner so
        many collisions can accumulate in a single episode.

        Returns:
            bool: Flag to indicate if the simulation should continue.
        """
        num_colliding_vehicles = self.simulator.get_colliding_vehicle_number()
        if num_colliding_vehicles >= 2: # collision happens between two vehicles.
            colliding_vehicles = self.simulator.get_colliding_vehicles()
            veh_1_id = colliding_vehicles[0]
            veh_2_id = colliding_vehicles[1]
            self.record.update(
                {
                    "veh_1_id": veh_1_id,
                    "veh_1_obs": self.vehicle_list[veh_1_id].observation if veh_1_id in self.vehicle_list else None,
                    "veh_2_id": veh_2_id,
                    "veh_2_obs": self.vehicle_list[veh_2_id].observation if veh_2_id in self.vehicle_list else None,
                    "warmup_time": self.warmup_time,
                    "run_time": self.run_time,
                    "finish_reason": "collision",
                }
            )
            if getattr(self, "continue_on_collision", False):
                # Count this collision once per unique (sorted) pair across a
                # contiguous run of SUMO ticks reporting the same colliders.
                sig = ("vv", tuple(sorted((veh_1_id, veh_2_id))))
                if sig != self._last_collision_signature:
                    self._collision_count_vehicle_vehicle += 1
                    self._last_collision_signature = sig
                    logger.info(
                        f"[NDE] collision (V-V) #{self._collision_count_vehicle_vehicle}: "
                        f"{veh_1_id} <-> {veh_2_id} (continue_on_collision)"
                    )
                # fall through to timeout check so run keeps going
            else:
                return False
        elif num_colliding_vehicles == 1: # collision happens between a vehicle and a vru.
            colliding_vehicles = self.simulator.get_colliding_vehicles()
            veh_1_id = colliding_vehicles[0]
            collision_objects = traci.simulation.getCollisions()
            if veh_1_id == collision_objects[0].collider:
                vru_1_id = collision_objects[0].victim
            elif veh_1_id == collision_objects[0].victim:
                vru_1_id = collision_objects[0].collider
            else:
                vru_1_id = None
            self.record.update(
                {
                    "veh_1_id": veh_1_id,
                    "veh_1_obs": self.vehicle_list[veh_1_id].observation if veh_1_id in self.vehicle_list else None,
                    "vru_1_id": vru_1_id,
                    "warmup_time": self.warmup_time,
                    "run_time": self.run_time,
                    "finish_reason": "collision",
                }
            )
            if getattr(self, "continue_on_collision", False):
                sig = ("vvru", tuple(sorted((str(veh_1_id), str(vru_1_id)))))
                if sig != self._last_collision_signature:
                    self._collision_count_vehicle_vru += 1
                    self._last_collision_signature = sig
                    logger.info(
                        f"[NDE] collision (V-VRU) #{self._collision_count_vehicle_vru}: "
                        f"{veh_1_id} <-> {vru_1_id} (continue_on_collision)"
                    )
                # fall through to timeout check
            else:
                return False
        else:
            # No colliding agents this tick; reset the latch so the next
            # distinct collision is counted as a fresh event.
            self._last_collision_signature = None

        if utils.get_time() >= self.warmup_time + self.run_time: # running time exceeds the limit.
            # When continue_on_collision is True we may have logged collisions
            # during the run; surface that in finish_reason for downstream
            # analysis. Otherwise, fall through with the default no-collision
            # timeout record.
            cum_vv = getattr(self, "_collision_count_vehicle_vehicle", 0)
            cum_vvru = getattr(self, "_collision_count_vehicle_vru", 0)
            if getattr(self, "continue_on_collision", False) and (cum_vv or cum_vvru):
                self.record.update(
                    {
                        "warmup_time": self.warmup_time,
                        "run_time": self.run_time,
                        "finish_reason": "timeout_with_collisions",
                        "collision_count_vv": cum_vv,
                        "collision_count_vvru": cum_vvru,
                    }
                )
                logger.info(
                    f"[NDE] run_time reached with cumulative collisions: "
                    f"V-V={cum_vv}, V-VRU={cum_vvru}"
                )
            else:
                self.record.update(
                    {
                        "veh_1_id": None,
                        "veh_2_id": None,
                        "warmup_time": self.warmup_time,
                        "run_time": self.run_time,
                        "finish_reason": "timeout",
                    }
                )
            return False
        return True
