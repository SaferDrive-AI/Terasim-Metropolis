import os
import time

from loguru import logger

from terasim.envs.base import BaseEnv
from terasim.overlay import traci
from terasim.vulnerable_road_user.vulnerable_road_user import VulnerableRoadUserList


class BaseEnvComplete(BaseEnv):
    def __init__(self, vehicle_factory, vulnerable_road_user_factory, info_extractor):
        """Initialize the complete and base testing environment.

        Args:
            vehicle_factory (VehicleFactory): The vehicle factory.
            vulnerable_road_user_factory (VulnerableRoadUserFactory): The vulnerable road user factory.
            info_extractor (InfoExtractor): The info extractor.
        """
        super().__init__(vehicle_factory, info_extractor)
        self.vulnerable_road_user_list = VulnerableRoadUserList({})
        self.vulnerable_road_user_factory = vulnerable_road_user_factory
        self.control_ls = {}  # Store controlled vehicle IDs between steps
        # Subclass can set these values for two-circle filtering
        self.cache_radius = None  # Large circle: for observation recording
        self.control_radius = None  # Small circle: for NNDE control
        self._post_load_cleanup_done = False
        # When False, skip radius filtering/coloring and let NDE control all vehicles.
        # Honor TERASIM_CNDE_ENABLED env var as the default — subclasses may still
        # override (e.g. NADEWithAV reads the same env var explicitly).
        self.cnde_enabled = os.environ.get("TERASIM_CNDE_ENABLED", "true").lower() == "true"
        self._original_vehicle_colors = {}  # {veh_id: (r, g, b, a)} saved before coloring

    def _should_include_in_cnde_control(self, veh_id: str) -> tuple:
        """Determine if a vehicle should be included in CNDE control list.

        A vehicle is excluded if:
        1. It is inside a junction (on an internal lane), OR
        2. Its time-to-stop-bar is less than the safety threshold, OR
        3. It just exited a junction and is the lead vehicle on its edge, OR
        4. Its current speed is below LOW_SPEED_THRESHOLD.
        Otherwise, it is included.

        Args:
            veh_id (str): The vehicle ID to check.

        Returns:
            tuple: (bool, str) - (included, reason). reason is "" if included,
                   otherwise one of "junction", "approaching_edge_end",
                   "first_after_junction", "low_speed", "error".
        """
        LOW_SPEED_THRESHOLD = 3  # m/s (~10.8 km/h)
        TIME_TO_STOP_BAR_THRESHOLD = 3.0  # seconds — must have ≥3s before reaching stop bar for yield
        EDGE_END_MARGIN = 5.0   # meters — minimum distance even for slow vehicles

        try:
            veh_speed = traci.vehicle.getSpeed(veh_id)
            veh_lane_id = traci.vehicle.getLaneID(veh_id)

            # Filter 1: Already inside junction
            if veh_lane_id.startswith(':'):
                return False, "junction"

            # Filter 2: Time-to-stop-bar check
            # Use time = distance / speed instead of absolute distance, so fast
            # vehicles are excluded earlier and slow vehicles aren't over-filtered.
            # Also enforce a minimum absolute distance for near-zero speed cases.
            veh_lane_pos = traci.vehicle.getLanePosition(veh_id)
            lane_length = traci.lane.getLength(veh_lane_id)
            dist_to_edge_end = lane_length - veh_lane_pos

            if dist_to_edge_end < EDGE_END_MARGIN:
                return False, "approaching_edge_end"
            if veh_speed > 0.1:
                time_to_stop_bar = dist_to_edge_end / veh_speed
                if time_to_stop_bar < TIME_TO_STOP_BAR_THRESHOLD:
                    return False, "approaching_edge_end"

            # Filter 3: First vehicle after junction — just exited junction and
            # is the lead vehicle on this edge. It may still be interacting with
            # junction traffic (e.g. left-turn yielding to through traffic).
            # Let SUMO handle it until it clears the junction influence zone.
            leader_info = traci.vehicle.getLeader(veh_id)
            if leader_info is None or leader_info[0] == '':
                # No leader on this lane — this is the first vehicle
                # Check if we're near the start of the edge (just exited junction)
                if veh_lane_pos < EDGE_END_MARGIN:
                    return False, "first_after_junction"
                if veh_speed > 0.1 and veh_lane_pos / veh_speed < TIME_TO_STOP_BAR_THRESHOLD:
                    return False, "first_after_junction"

            # Filter 4: Low speed — let SUMO handle
            if veh_speed < LOW_SPEED_THRESHOLD:
                return False, "low_speed"

            # Filter 5: Lane mismatch cooldown — vehicle was recently excluded
            # because CNDE trajectory targeted a different lane. Let SUMO settle
            # it back on a proper lane before re-entering CNDE control.
            if veh_id in self.vehicle_list:
                ctrl = self.vehicle_list[veh_id].controller
                if hasattr(ctrl, 'cnde_cooldown_until'):
                    sim_time = traci.simulation.getTime()
                    if sim_time < ctrl.cnde_cooldown_until:
                        return False, "lane_mismatch_cooldown"

            # Filter 6: TWLTL center lane — vehicle is on a two-way left-turn
            # lane. CNDE's moveToXY would conflict with TWLTL manager's
            # changeLane, causing the vehicle to oscillate between the two
            # controllers. Let SUMO + TWLTL manager handle it.
            if hasattr(self, '_twltl_manager') and self._twltl_manager is not None:
                edge_id = traci.vehicle.getRoadID(veh_id)
                if edge_id in self._twltl_manager._twltl_edges:
                    center_idx = self._twltl_manager._twltl_edges[edge_id]
                    current_idx = int(veh_lane_id.split('_')[-1])
                    if current_idx == center_idx:
                        return False, "twltl_center_lane"

            return True, ""

        except Exception as e:
            logger.debug(f"Error checking CNDE control inclusion for {veh_id}: {e}")
            return True, "error"

    def get_context_for_control(self):
        """Get controlled vehicle IDs using SUMO context subscription and real-time position.

        When cnde_enabled is True, filters vehicles into two circles centered on AV:
        - cache_radius (large circle): All vehicles for observation recording
        - control_radius (small circle): Vehicles for NNDE control

        When cnde_enabled is False, still computes radius-based lists (NADE needs them for
        adversarial decisions), but skips the cnde_control_ids filter and coloring.

        Returns:
            dict: Context with 'terasim_controlled_vehicle_ids' (cache_radius)
                  and 'control_radius_vehicle_ids' (control_radius) keys.
        """
        import numpy as np
        AV_ID = "AV"

        if AV_ID not in traci.vehicle.getIDList():
            logger.debug("AV not in simulation yet")
            return {}

        # Get vehicles within cache_radius using context subscription
        context_results = traci.vehicle.getContextSubscriptionResults(AV_ID)
        cache_radius_vehicle_ids = []
        if context_results:
            cache_radius_vehicle_ids = list(context_results.keys())

        # Calculate control_radius vehicles using real-time traci position
        # Include AV in control_radius_vehicle_ids for observation/sync purposes
        control_radius_vehicle_ids = []
        if self.control_radius is not None and cache_radius_vehicle_ids:
            try:
                av_pos = np.array(traci.vehicle.getPosition(AV_ID))
                for veh_id in cache_radius_vehicle_ids:
                    if veh_id == AV_ID:
                        continue  # Skip AV in loop, will add it separately
                    try:
                        veh_pos = np.array(traci.vehicle.getPosition(veh_id))
                        distance = np.linalg.norm(veh_pos - av_pos)
                        if distance <= self.control_radius:
                            control_radius_vehicle_ids.append(veh_id)
                    except Exception:
                        # Vehicle may have left simulation
                        continue
                # Always include AV in control_radius_vehicle_ids for CNDE observation/sync
                control_radius_vehicle_ids.append(AV_ID)
            except Exception:
                # AV may have left simulation
                pass

        # Filter control_radius vehicles for CNDE control (only needed in CNDE mode).
        # Only recompute every CNDE_FILTER_INTERVAL steps (aligned with CNDE inference
        # cycle) to avoid time-sync issues: the same cnde_control_ids list applies for
        # the entire 0.5s trajectory window, preventing mid-trajectory filter changes.
        CNDE_FILTER_INTERVAL = 5  # steps (= 0.5s = one CNDE inference cycle)
        if not hasattr(self, '_cnde_filter_counter'):
            self._cnde_filter_counter = 0
        if not hasattr(self, '_cached_cnde_control_ids'):
            self._cached_cnde_control_ids = []

        cnde_control_ids = self._cached_cnde_control_ids
        if self.cnde_enabled:
            self._cnde_filter_counter += 1
            should_recompute = (self._cnde_filter_counter % CNDE_FILTER_INTERVAL == 1
                                or not self._cached_cnde_control_ids)

            if should_recompute:
                cnde_control_ids = []
                filter_reasons = {"junction": 0, "first_in_queue": 0, "low_speed": 0, "is_av": 0}
                for veh_id in control_radius_vehicle_ids:
                    if veh_id == AV_ID:
                        filter_reasons["is_av"] += 1
                        continue
                    included, reason = self._should_include_in_cnde_control(veh_id)
                    if included:
                        cnde_control_ids.append(veh_id)
                    else:
                        filter_reasons[reason] = filter_reasons.get(reason, 0) + 1
                self._cached_cnde_control_ids = cnde_control_ids

                sim_time = traci.simulation.getTime()
                total_sumo = len(traci.vehicle.getIDList())
                active_reasons = {k: v for k, v in filter_reasons.items() if v > 0 and k != "is_av"}
                reason_str = ", ".join(f"{k}={v}" for k, v in active_reasons.items()) if active_reasons else "none"
                logger.info(
                    f"[CNDE] t={sim_time:.1f} sumo={total_sumo} "
                    f"cache={len(cache_radius_vehicle_ids)} "
                    f"control={len(cnde_control_ids)} "
                    f"filtered({reason_str})"
                )

        self.control_ls = {
            'terasim_controlled_vehicle_ids': cache_radius_vehicle_ids,
            'control_radius_vehicle_ids': control_radius_vehicle_ids,
            'cnde_control_ids': cnde_control_ids,
        }
        return self.control_ls

    def _step(self, simulator, ctx) -> bool:
        """The main step function of the environment.

        Args:
            simulator (Simulator): The simulator object.
            ctx (dict): The context dictionary.

        Returns:
            bool: True if the simulation should continue.
        """
        # Early cleanup: detect vehicles removed via GUI while simulation was paused.
        # GUI remove modifies SUMO internal state without going through TraCI's normal
        # departure flow. We must detect and clean these up BEFORE any control logic
        # to prevent segfaults from TraCI commands on ghost vehicles.
        sumo_veh_ids = set(traci.vehicle.getIDList())
        stale_veh_ids = [
            veh_id for veh_id in self.vehicle_list.keys()
            if veh_id not in sumo_veh_ids
        ]
        if stale_veh_ids:
            logger.warning(f"[GUI Cleanup] {len(stale_veh_ids)} vehicles removed outside TeraSim: {stale_veh_ids}")
            for veh_id in stale_veh_ids:
                if veh_id in self.vehicle_list:
                    self.vehicle_list.pop(veh_id)._uninstall()
            # Also update control_ls to remove stale references
            for key in self.control_ls:
                if isinstance(self.control_ls[key], list):
                    self.control_ls[key] = [v for v in self.control_ls[key] if v not in stale_veh_ids]

        # One-time cleanup after state file load: fix stale turn signals and
        # reroute vehicles stuck on wrong lanes (allowsContinuation=False).
        # State files can contain vehicles with residual signals from abandoned
        # lane change attempts, and vehicles on lanes incompatible with their route.
        if not self._post_load_cleanup_done:
            self._post_load_cleanup_done = True
            self._cleanup_after_state_load(sumo_veh_ids)

        # =============012126YW update
        # In large network, terasim cannot manage all vehicles in the network, need a filter
        prev_ids = set(self.control_ls.get('terasim_controlled_vehicle_ids', []))
        ctx = self.get_context_for_control()
        control_radius_ids = set(ctx.get('control_radius_vehicle_ids', []))

        # Visual feedback: color vehicles based on distance from AV (CNDE mode only)
        if self.cnde_enabled and getattr(self, 'cnde_vehicle_coloring', True):
            current_ids = set(ctx.get('terasim_controlled_vehicle_ids', []))
            leaving_ids = prev_ids - current_ids
            for veh_id in leaving_ids:
                if veh_id in traci.vehicle.getIDList():
                    original = self._original_vehicle_colors.pop(veh_id, (255, 255, 0, 255))
                    traci.vehicle.setColor(veh_id, original)
            for veh_id in current_ids:
                if veh_id != "AV" and veh_id in traci.vehicle.getIDList():
                    if veh_id not in self._original_vehicle_colors:
                        self._original_vehicle_colors[veh_id] = traci.vehicle.getColor(veh_id)
                    if veh_id in control_radius_ids:
                        traci.vehicle.setColor(veh_id, (255, 0, 0, 255))  # Red for control_radius
                    else:
                        traci.vehicle.setColor(veh_id, (0, 0, 255, 255))  # Blue for cache_radius only

        # First synchronize the vehicle list
        self._maintain_all_vehicles(ctx)
        self._maintain_all_vulnerable_road_users(ctx)

        # Then call custom env defined step
        step_result = self.on_step(ctx)

        # If custom env requested to stop, log some of the information
        if isinstance(step_result, bool):
            if step_result:
                return True
            else:
                self._request_termination("Simulation ends normally", None)
                return False
        elif isinstance(step_result, dict):
            self._request_termination(step_result["reason"], step_result["info"])
            return False
        else:
            raise TypeError("The output of a step should be a boolean or a dictionary")

    def _cleanup_after_state_load(self, sumo_veh_ids):
        """One-time cleanup after loading a SUMO state file.

        Fixes three issues caused by state loading:
        1. Stale turn signals from abandoned lane change attempts.
        2. Wrong-lane vehicles (allowsContinuation=False) — rerouted first,
           then force-moved to a valid lane if reroute alone doesn't help.
        3. Dead-lane vehicles (lanes with no outgoing connections) — moved to
           the nearest lane that has a connection to their route's next edge.
        """
        signals_reset = 0
        rerouted = 0

        for veh_id in sumo_veh_ids:
            try:
                # Reset all turn signals to let SUMO recompute from scratch
                traci.vehicle.setSignals(veh_id, -1)
                signals_reset += 1

                lane_id = traci.vehicle.getLaneID(veh_id)
                if lane_id.startswith(':'):
                    continue  # Skip vehicles in junctions

                best_lanes = traci.vehicle.getBestLanes(veh_id)
                allows_continuation = True
                for bl in best_lanes:
                    if bl[0] == lane_id:
                        allows_continuation = bl[4]
                        break

                if allows_continuation:
                    continue  # Vehicle is on a valid lane

                # Reroute to match current position — this is safe and lets SUMO
                # update the vehicle's route without teleporting it.
                # moveTo() was removed: forcibly moving 700+ vehicles after they've
                # been running for 200 warmup steps corrupts SUMO's internal C++
                # leader/follower state and causes a segfault in executeMove() 2-3
                # steps later. The unified vehicle type (LC2013) fixes the root cause.
                traci.vehicle.rerouteTraveltime(veh_id)
                rerouted += 1

            except Exception:
                continue

        logger.info(
            f"[State Load Cleanup] signals_reset={signals_reset}, rerouted={rerouted}"
        )

    ########## Other private utility functions that should not be directly called by custom env

    def _maintain_all_vulnerable_road_users(self, ctx):
        """Maintain the vulnerable road user list based on the departed list and arrived list.
        """
        if "terasim_controlled_vulnerable_road_user_ids" in ctx:
            terasim_controlled_vulnerable_road_user_ids = (
                ctx["terasim_controlled_vulnerable_road_user_ids"]
                if isinstance(ctx["terasim_controlled_vulnerable_road_user_ids"], list)
                else [ctx["terasim_controlled_vulnerable_road_user_ids"]]
            )
            logger.trace("Using the controlled vulnerable road user list from ctx")
            controlled_vruIDs = set(terasim_controlled_vulnerable_road_user_ids)
            realtime_vruIDs = [
                vruID for vruID in self.simulator.get_vruID_list() if vruID in controlled_vruIDs
            ]
        else:
            realtime_vruIDs = list(self.simulator.get_vruID_list())
        realtime_vruID_set = set(realtime_vruIDs)

        vruID_set = set(self.vulnerable_road_user_list.keys())
        # log the difference between the two sets
        logger.trace(f"Realtime vehID set: {realtime_vruID_set}")
        logger.trace(f"Current vehID set: {vruID_set}")
        if vruID_set != realtime_vruID_set:
            # SUMO's id-list order, not set order (see BaseEnv._maintain_all_vehicles).
            for vruID in realtime_vruIDs:
                if vruID not in vruID_set:
                    vru = self._add_vulnerbale_road_user_to_env(vruID)
                    vru._install()
            for vruID in list(self.vulnerable_road_user_list.keys()):
                if vruID not in realtime_vruID_set:
                    self._remove_vulnerable_road_user_from_env(vruID)

    def _add_vulnerbale_road_user_to_env(self, vru_id_list):
        """Add VRUs from vru_id_list.

        Args:
            vru_id_list (list(str)): List of vru IDs needed to be inserted.

        Raises:
            ValueError: If one vehicle is neither "BV" nor "AV", it should not enter the network.
        """
        single_input = not isinstance(vru_id_list, list)
        if single_input:
            vru_id_list = [vru_id_list]

        output = []
        for vru_id in vru_id_list:
            vru = self.vulnerable_road_user_factory.create_vulnerable_road_user(
                vru_id, self.simulator
            )
            self.vulnerable_road_user_list.add_vulnerable_road_users(vru)
            output.append(vru)
        return output[0] if single_input else output

    def _remove_vulnerable_road_user_from_env(self, vru_id_list):
        """Delete vulnerable road users in vru_id_list.

        Args:
            vru_id_list (list(str)): List of vulnerable road user IDs needed to be deleted.

        Raises:
            ValueError: If the vulnerable road user is neither "BV" nor "AV", it shouldn't enter the network.
        """
        if not isinstance(vru_id_list, list):
            vru_id_list = [vru_id_list]
        for vru_id in vru_id_list:
            if vru_id in self.vulnerable_road_user_list:
                self.vulnerable_road_user_list.pop(vru_id)._uninstall()
