from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger
from terasim.overlay import traci

from terasim_nde_nade.utils import AbstractStaticAdversity


# -----------------------------------------------------------------------------
# Minimal helpers: define three light-weight object types (cone / barrier / sign)
# -----------------------------------------------------------------------------

def _ensure_type(type_id: str, length: float, width: float, height: float, color_rgba: Tuple[int,int,int,int]) -> str:
    if type_id not in traci.vehicletype.getIDList():
        # Use DEFAULT_VEHTYPE as a template and then shrink to a static-like object
        traci.vehicletype.copy("DEFAULT_VEHTYPE", type_id)
        traci.vehicletype.setVehicleClass(type_id, "passenger")
        traci.vehicletype.setShapeClass(type_id, "passenger")
        traci.vehicletype.setLength(type_id, length)
        traci.vehicletype.setWidth(type_id, width)
        traci.vehicletype.setHeight(type_id, height)
        traci.vehicletype.setMinGap(type_id, 0.1)
        traci.vehicletype.setColor(type_id, color_rgba)
    return type_id


def cone_type_id() -> str:
    return _ensure_type("CONSTRUCTION_CONE_MIN", 0.3, 0.3, 0.7, (255, 140, 0, 255))


def barrier_type_id() -> str:
    return _ensure_type("CONSTRUCTION_BARRIER_MIN", 1.5, 0.6, 1.0, (255, 255, 0, 255))


def sign_type_id() -> str:
    return _ensure_type("CONSTRUCTION_SIGN_MIN", 0.8, 0.3, 1.5, (255, 0, 0, 255))


# -----------------------------------------------------------------------------
# UrbanConstructionZone (no dependency on ConstructionAdversity)
# -----------------------------------------------------------------------------
class UrbanConstructionZone(AbstractStaticAdversity):
    """
    Minimal "urban construction zone" generator (placing cones/barriers/signs per lane), with single responsibility:
      - Only places objects based on pre-computed external results (lane_plans); does not calculate start/end points or zone lengths at runtime.
      - lane_plans are computed offline by the config generator (generate_config.py) and written to config; this class only deploys objects by segment and spacing.
      - Still ensures at least `min_open_lanes` lanes remain passable; defaults to closing rightmost lane index 0 (more can be specified via `lanes_to_close`).

    This implementation uses TraCI to place stationary "vehicles" to represent construction objects.
    """

    def __init__(
        self,
        *,
        edges: Sequence[str],
        lanes_to_close: Optional[Sequence[int]] = None,
        start_position: Optional[float] = None,
        end_position: Optional[float] = None,
        start_time: float = 0.0,
        end_time: float = -1.0,
        # Basic parameters
        min_open_lanes: int = 1,
        min_lane_length: float = 30.0,
        work_zone_offset: float = 0.0,  # Lateral offset from lane center (m), work zone maintains this offset
        spacing: float = 20.0,          # Default spacing for non-warning zones (m)
        use_dynamic_spacing: bool = False,
        speed_limit_mph: Optional[float] = None,
        construction_type: str = "cone",  # "cone" | "barrier" | "mixed"
        allow_exceed_lane_boundary: bool = False,  # Whether to allow work zone to exceed lane boundary
        lane_plans: Optional[Dict[str, Dict[str, Any]]] = None,
        connection_management: Optional[Dict] = None,  # Connection management configuration
    ) -> None:
        super().__init__(start_time=start_time, end_time=end_time)
        self._edges = [e for e in (edges or []) if e]
        self._lanes_to_close = self._normalize_indices(lanes_to_close or [0])
        self._min_open = max(0, int(min_open_lanes))
        self._min_len = float(min_lane_length)
        self._start_override = start_position
        self._end_override = end_position
        self._wz_offset = float(work_zone_offset)
        self._spacing = float(spacing)
        self._dyn_spacing = bool(use_dynamic_spacing)
        self._speed_limit_mph = speed_limit_mph
        self._ctype = construction_type if construction_type in {"cone", "barrier", "mixed"} else "cone"
        self._allow_exceed_boundary = bool(allow_exceed_lane_boundary)
        self._lane_plans: Dict[str, Dict[str, Any]] = dict(lane_plans) if lane_plans else {}

        self._lane_mapping: Dict[str, List[int]] = {}
        self._objects_by_lane: Dict[str, List[str]] = {}
        self._active = False

        # ========== Initialize Connection Controller ==========
        self.connection_controller = None
        self._connection_enabled = False

        if connection_management and connection_management.get("enabled"):
            try:
                from .connection_manager import (
                    RuntimeConnectionController,
                    JunctionModification,
                    BlockedConnection,
                    EmergencyConnection
                )

                self.connection_controller = RuntimeConnectionController()

                # Deserialize modifications from config
                modifications = self._deserialize_modifications(
                    connection_management.get("modifications", [])
                )

                if modifications:
                    self.connection_controller.register_modifications(modifications)
                    self._connection_enabled = True
                    logger.info(f"Connection management initialized for {len(modifications)} junctions")
            except Exception as e:
                logger.warning(f"Failed to initialize connection management: {e}")

    # ------------------------------------------------------------------
    # Deserialize modifications
    # ------------------------------------------------------------------
    def _deserialize_modifications(self, mods_cfg: List[Dict]) -> List:
        """Deserialize JunctionModification objects from config."""
        from .connection_manager import (
            JunctionModification,
            BlockedConnection,
            EmergencyConnection
        )

        modifications = []
        for mod_cfg in mods_cfg:
            try:
                junction_id = mod_cfg["junction_id"]

                # Rebuild BlockedConnection list
                blocked = [
                    BlockedConnection(
                        from_edge=b["from_edge"],
                        to_edge=b["to_edge"],
                        from_lane=int(b["from_lane"]),
                        to_lane=int(b["to_lane"]),
                        junction_id=junction_id,
                        turn_direction=b.get("direction", ""),
                        internal_lane=b.get("internal_lane")
                    )
                    for b in mod_cfg.get("blocked_connections", [])
                ]

                # Rebuild EmergencyConnection list
                emergency = []
                for e_cfg in mod_cfg.get("emergency_connections", []):
                    # Find corresponding original blocked connection
                    original_from_lane = int(e_cfg["original_from_lane"])
                    original_blocked = next(
                        (b for b in blocked if b.from_lane == original_from_lane),
                        blocked[0] if blocked else None
                    )

                    if original_blocked:
                        emergency.append(EmergencyConnection(
                            from_edge=e_cfg["from_edge"],
                            to_edge=e_cfg["to_edge"],
                            from_lane=int(e_cfg["from_lane"]),
                            to_lane=int(e_cfg["to_lane"]),
                            junction_id=junction_id,
                            original_blocked=original_blocked,
                            disallow="all"  # Initially disabled
                        ))

                modifications.append(JunctionModification(
                    junction_id=junction_id,
                    blocked_connections=blocked,
                    emergency_connections=emergency
                ))
            except Exception as e:
                logger.warning(f"Failed to deserialize modification: {e}")

        return modifications

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def is_effective(self) -> bool:
        if not self._edges:
            logger.warning("No edges provided.")
            return False

        try:
            known = set(traci.edge.getIDList())
        except Exception as exc:
            logger.error(f"Failed to query edges: {exc}")
            return False

        mapping: Dict[str, List[int]] = {}
        for e in self._edges:
            if e not in known:
                logger.warning(f"Edge {e} not found in SUMO network.")
                continue
            try:
                nlanes = int(traci.edge.getLaneNumber(e))
            except Exception:
                logger.warning(f"Cannot read lane count for {e}.")
                continue
            if nlanes < 2:
                logger.debug(f"Skip {e}: lane_count={nlanes} < 2.")
                continue

            candidates = [i for i in self._lanes_to_close if i < nlanes] or [0]
            # Ensure at least self._min_open lanes remain open
            allow_close = max(0, nlanes - self._min_open)
            candidates = sorted(candidates)[:allow_close]
            if candidates and 0 not in candidates:
                candidates = sorted(set([0] + candidates))

            # At least one closable lane with sufficient length
            valid: List[int] = []
            for li in candidates:
                lane_id = f"{e}_{li}"
                try:
                    L = float(traci.lane.getLength(lane_id))
                except Exception:
                    logger.debug(f"Skip {lane_id}: cannot read length.")
                    continue
                if L >= self._min_len:
                    valid.append(li)
            if valid:
                mapping[e] = valid

        # A fixed mapping such as {'<edge_id>': [1]} restricts the work zone to
        # lane 1 of that edge (keeping it off a sidewalk lane at index 0).
        if not mapping:
            logger.warning("No eligible lanes after validation.")
            return False

        self._lane_mapping = mapping
        logger.info("Simple workzone targets: %s", ", ".join(f"{e}:{v}" for e, v in mapping.items()))
        logger.info(f"Target edges configured: {self._edges}")
        logger.info(f"Target lanes to close: {self._lanes_to_close}")
        return True

    def initialize(self, time: float) -> None:
        if not self._lane_mapping:
            assert self.is_effective(), "UrbanConstructionZone configuration invalid."
        self._objects_by_lane.clear()

        # Add debug info
        logger.info(f"Available lane plans: {list(self._lane_plans.keys())}")
        logger.info(f"Expected edges: {list(self._lane_mapping.keys())}")

        for edge, lanes in self._lane_mapping.items():
            for li in lanes:
                lane_id = f"{edge}_{li}"
                try:
                    L = float(traci.lane.getLength(lane_id))
                    vmax = float(traci.lane.getMaxSpeed(lane_id))  # m/s
                    w = float(traci.lane.getWidth(lane_id))
                except Exception:
                    logger.debug(f"Skip {lane_id}: cannot read lane attributes.")
                    continue

                plan = self._lane_plans.get(lane_id)
                if not plan:
                    logger.debug(f"Skip {lane_id}: no precomputed lane plan provided.")
                    continue

                try:
                    s = float(plan.get("start", 0.0))
                    e = float(plan.get("end", 0.0))
                    seg = {
                        "warning": float(plan.get("warning", 0.0)),
                        "taper_in": float(plan.get("taper_in", 0.0)),
                        "work": float(plan.get("work", 0.0)),
                        "taper_out": float(plan.get("taper_out", 0.0)),
                    }
                    zone_spacing = plan.get("zone_spacing", {}) or {}
                    wz_offset = float(plan.get("work_zone_offset", self._wz_offset))
                except Exception as exc:
                    logger.debug(f"Skip {lane_id}: invalid lane plan format: {exc}")
                    continue

                if e - s < 1.0:
                    logger.debug(f"Skip {lane_id}: too short effective interval per plan.")
                    continue

                obj_ids = self._deploy_lane_zone(lane_id, w, s, e, seg, zone_spacing, wz_offset)
                if obj_ids:
                    self._objects_by_lane[lane_id] = obj_ids

        if not self._objects_by_lane:
            raise AssertionError("No construction objects were created.")

        self._active = True

        # ========== Apply Connection Rerouting ==========
        if self._connection_enabled and self.connection_controller:
            try:
                self.connection_controller.apply_construction_rerouting(traci)
                logger.info("Construction zone connection rerouting applied successfully")
            except Exception as e:
                logger.error(f"Failed to apply connection rerouting: {e}")
                # Not fatal - traffic cones are still effective

        logger.info(
            "Initialized workzones on %d lanes, total objects: %d",
            len(self._objects_by_lane),
            sum(len(v) for v in self._objects_by_lane.values()),
        )

    def update(self, time: float) -> None:
        if not self._active:
            return
        # keep all objects stationary
        for ids in self._objects_by_lane.values():
            for oid in ids:
                if oid in traci.vehicle.getIDList():
                    try:
                        traci.vehicle.setSpeed(oid, 0)
                    except Exception:
                        pass
        # timed removal
        if self.end_time != -1 and time >= self.end_time:
            for ids in self._objects_by_lane.values():
                for oid in ids:
                    try:
                        traci.vehicle.remove(oid)
                    except Exception:
                        pass
            self._active = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_indices(indices: Sequence[int]) -> List[int]:
        out: List[int] = []
        for v in indices:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if i >= 0:
                out.append(i)
        return sorted(set(out))

    # No longer contains any runtime zone length/spacing calculation logic; only places objects based on lane_plans.

    def _deploy_lane_zone(
        self,
        lane_id: str,
        lane_width: float,
        start: float,
        end: float,
        seg: Dict[str, float],
        zone_spacing: Dict[str, float],
        wz_offset: float,
    ) -> List[str]:
        edge_id = traci.lane.getEdgeID(lane_id)
        route_id = f"r_wz_{lane_id}"
        if route_id not in traci.route.getIDList():
            traci.route.add(route_id, [edge_id])

        cone = cone_type_id()
        barrier = barrier_type_id()
        sign = sign_type_id()

        # Calculate right boundary position of the rightmost lane of entire edge
        try:
            num_lanes = traci.edge.getLaneNumber(edge_id)
            rightmost_lane_id = f"{edge_id}_0"  # Index 0 is the rightmost lane
            rightmost_lane_width = traci.lane.getWidth(rightmost_lane_id)
            # Place warning sign 0.5m to the right of rightmost lane
            warning_sign_offset = -(rightmost_lane_width / 2 + 0.5)
        except Exception:
            # If unable to get info, use current lane's right boundary
            warning_sign_offset = -(lane_width / 2 + 0.5)

        # Calculate zone intervals: start is taper_in start; warning starts from (start - warning)
        zones: Dict[str, Tuple[float, float]] = {}
        wlen = float(seg.get("warning", 0.0))
        tin = float(seg.get("taper_in", 0.0))
        wlen_work = float(seg.get("work", 0.0))
        tout = float(seg.get("taper_out", 0.0))

        # Warning zone starts at least from 5m, leave 5m offset
        warning_min_offset = 5.0
        w0 = max(warning_min_offset, start - wlen)

        if wlen > 0:
            zones["warning"] = (w0, start)
        if tin > 0:
            zones["taper_in"] = (start, start + tin)
        if wlen_work > 0:
            zones["work"] = (start + tin, start + tin + wlen_work)
        if tout > 0:
            zones["taper_out"] = (start + tin + wlen_work, start + tin + wlen_work + tout)

        # Clip to [warning_min_offset, end], ensure all zones start at least from 5m
        for k, (a, b) in list(zones.items()):
            if k == "warning":
                # Warning zone special handling: ensure starts at least from 5m
                zones[k] = (max(warning_min_offset, a), min(end, b))
            else:
                zones[k] = (max(0.0, a), min(end, b))

        ids: List[str] = []
        right_edge = -(lane_width / 2 - 0.3)
        max_left = (lane_width / 2 - 0.3)

        def clamp_off(x: float) -> float:
            if self._allow_exceed_boundary:
                return x
            else:
                return max(-max_left, min(x, max_left))

        # Unified spacing: taper_in, work, taper_out use same spacing for consistency
        unified_spacing = float(zone_spacing.get("work", self._spacing))

        # Process warning zone first (separate spacing)
        if "warning" in zones:
            wa, wb = zones["warning"]
            if wb > wa:
                warning_sp = float(zone_spacing.get("warning", self._spacing))
                pos = wa
                while pos < wb:
                    oid = f"WZ_{lane_id}_warning_{len(ids)}"
                    try:
                        try:
                            num_lanes = traci.edge.getLaneNumber(edge_id)
                            warning_lane_id = f"{edge_id}_0"
                            current_lane_index = int(lane_id.split('_')[-1])
                            if current_lane_index == 0 and num_lanes > 1:
                                warning_lane_id = f"{edge_id}_1"

                            traci.vehicle.add(oid, routeID=route_id, typeID=sign)
                            traci.vehicle.setSpeedMode(oid, 0)
                            traci.vehicle.setLaneChangeMode(oid, 0)
                            traci.vehicle.moveTo(oid, warning_lane_id, pos)

                            warning_lane_width = traci.lane.getWidth(warning_lane_id)
                            warning_offset = -(warning_lane_width / 2 + 0.5) 
                            traci.vehicle.changeSublane(oid, warning_offset)
                        except Exception:
                            traci.vehicle.add(oid, routeID=route_id, typeID=sign)
                            traci.vehicle.setSpeedMode(oid, 0)
                            traci.vehicle.setLaneChangeMode(oid, 0)
                            traci.vehicle.moveTo(oid, lane_id, pos)
                            traci.vehicle.changeSublane(oid, warning_sign_offset)

                        traci.vehicle.setSpeed(oid, 0)
                        ids.append(oid)
                    except Exception as exc:
                        logger.debug(f"Place {oid} failed: {exc}")
                    pos += warning_sp

        # Continuously place taper_in, work, taper_out (unified spacing)
        cone_zones = ["taper_in", "work", "taper_out"]
        cone_zone_ranges = [(z, zones[z]) for z in cone_zones if z in zones and zones[z][1] > zones[z][0]]

        if cone_zone_ranges:
            overall_start = cone_zone_ranges[0][1][0]
            overall_end = cone_zone_ranges[-1][1][1]

            pos = overall_start
            idx = 0
            while pos < overall_end:
                # Determine which zone current position belongs to
                current_zone = None
                zone_start = 0.0
                zone_end = 0.0
                for z, (a, b) in cone_zone_ranges:
                    if a <= pos < b:
                        current_zone = z
                        zone_start = a
                        zone_end = b
                        break

                if current_zone is None:
                    pos += unified_spacing
                    continue

                # Calculate lateral position based on zone type
                if current_zone == "taper_in":
                    type_id = cone
                    t = (pos - zone_start) / max(1e-6, (zone_end - zone_start))
                    lat = clamp_off(right_edge + t * (wz_offset - right_edge))
                elif current_zone == "work":
                    if self._ctype == "barrier":
                        type_id = barrier
                    elif self._ctype == "mixed":
                        type_id = barrier if (idx % 3 == 2) else cone
                    else:
                        type_id = cone
                    lat = clamp_off(wz_offset)
                else:  # taper_out
                    type_id = cone
                    t = (pos - zone_start) / max(1e-6, (zone_end - zone_start))
                    lat = clamp_off(wz_offset + t * (right_edge - wz_offset))

                oid = f"WZ_{lane_id}_{current_zone}_{len(ids)}"
                try:
                    traci.vehicle.add(oid, routeID=route_id, typeID=type_id)
                    traci.vehicle.setSpeedMode(oid, 0)
                    traci.vehicle.setLaneChangeMode(oid, 0)
                    traci.vehicle.moveTo(oid, lane_id, pos)
                    if lat != 0.0:
                        try:
                            traci.vehicle.changeSublane(oid, lat)
                        except Exception:
                            pass
                    traci.vehicle.setSpeed(oid, 0)
                    ids.append(oid)
                except Exception as exc:
                    logger.debug(f"Place {oid} failed: {exc}")

                pos += unified_spacing
                idx += 1

        # Output range of each zone
        wi = zones.get("warning", (0.0, 0.0))
        ti = zones.get("taper_in", (0.0, 0.0))
        wk = zones.get("work", (0.0, 0.0))
        to = zones.get("taper_out", (0.0, 0.0))
        logger.info(
            "Lane %s: warning %0.1f~%0.1f | taper_in %0.1f~%0.1f | work %0.1f~%0.1f | taper_out %0.1f~%0.1f -> %d objs",
            lane_id, wi[0], wi[1], ti[0], ti[1], wk[0], wk[1], to[0], to[1], len(ids)
        )
        return ids
