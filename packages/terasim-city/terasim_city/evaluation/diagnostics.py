"""Evaluation-support helpers shared by trial evaluation and orchestration.

Neutral home for input parsing and per-run diagnostics that both the trial
evaluator (:mod:`terasim_city.optimize.evaluator`) and the orchestration layer
consume: incident (stalled-vehicle) input loading, SUMO warning counting for
the quality gate, end-of-run backlog / incident report blocks,
and the teleport-disable scenario patch used for incident runs.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Sequence


def load_stalls(path: Path | None) -> list[dict]:
    """Load optional stalled-vehicle incident data, failing loud on bad input.

    A ``None`` path means "no incident". A path that *is* given must point at an
    existing JSON object carrying a ``stalled_vehicles`` list: a missing file, a
    missing/renamed key, or a malformed entry raises rather than silently
    degrading into a no-incident run (which would hand back a "best plan" for the
    wrong scenario). The only two legal ways to express "no incident" are a
    ``None`` path or a file whose ``stalled_vehicles`` is an empty list.
    """
    if path is None:
        return []
    if not path.exists():
        raise FileNotFoundError(f"stall data file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(
            f"{path}: expected a JSON object, got {type(payload).__name__}"
        )
    if "stalled_vehicles" not in payload:
        raise ValueError(
            f"{path}: missing required 'stalled_vehicles' key. Express 'no "
            'incident\' as {"stalled_vehicles": []} or pass no stall file.'
        )
    raw_stalls = payload["stalled_vehicles"]
    if not isinstance(raw_stalls, list):
        raise ValueError(f"{path}: 'stalled_vehicles' must be a list")
    stalls: list[dict] = []
    for index, item in enumerate(raw_stalls):
        if not isinstance(item, dict):
            raise ValueError(
                f"{path}: stalled_vehicles[{index}] must be an object with "
                f"'latitude'/'longitude', got {type(item).__name__}"
            )
        stalls.append(
            {
                "latitude": _require_coordinate(
                    item, "latitude", -90.0, 90.0, path=path, index=index
                ),
                "longitude": _require_coordinate(
                    item, "longitude", -180.0, 180.0, path=path, index=index
                ),
            }
        )
    return stalls


def _require_coordinate(
    item: dict, field: str, lo: float, hi: float, *, path: Path, index: int
) -> float:
    """Validate one stall coordinate: present, a real number, finite, in range.

    External incident payloads are untrusted: a bool, a non-finite value, or an
    out-of-range coordinate is rejected here rather than silently flowing into
    lat/lon -> SUMO-edge conversion (which would mis-place or drop the stall).
    """
    if field not in item:
        raise ValueError(
            f"{path}: stalled_vehicles[{index}] missing required field '{field}'"
        )
    value = item[field]
    if isinstance(value, bool):
        raise ValueError(
            f"{path}: stalled_vehicles[{index}] {field} must be a number, not a bool"
        )
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{path}: stalled_vehicles[{index}] has non-numeric {field} ({value!r})"
        ) from exc
    if not math.isfinite(num):
        raise ValueError(
            f"{path}: stalled_vehicles[{index}] {field} must be finite, got {value!r}"
        )
    if not lo <= num <= hi:
        raise ValueError(
            f"{path}: stalled_vehicles[{index}] {field}={num} out of range [{lo}, {hi}]"
        )
    return num


def count_warnings(run_log: Path) -> dict[str, int]:
    """Count SUMO warning categories from ``run.log``."""
    if not run_log.exists():
        raise FileNotFoundError(f"run.log not found at {run_log!s}")
    lower = run_log.read_text(encoding="utf-8", errors="replace").lower()
    return {
        "emergency_stops": lower.count("emergency stop"),
        "teleports": lower.count("teleporting"),
        "collisions": lower.count("collision"),
    }


def build_backlog(env) -> dict:
    """Build end-of-run backlog counters from the TeraSim env."""
    stalled = int(env.end_stalled_objects_present)
    unfinished_total = int(env.end_running)
    return {
        "inserted_vehicles": int(env.cum_inserted),
        "arrived_vehicles": int(env.cum_arrived),
        "unfinished_vehicles": unfinished_total,
        "stalled_objects": stalled,
        "unfinished_background_vehicles": max(unfinished_total - stalled, 0),
        "pending_departures": int(env.end_pending),
        "halting_on_stall_edge": int(env.end_halting_on_stall_edge),
    }



def build_incident(env, stalls: Sequence[dict]) -> dict:
    """Build incident metadata for KPI output."""
    if len(env.stalled_edge_ids) != len(env.stalled_lane_ids):
        raise ValueError(
            "stalled edge/lane count mismatch: "
            f"{len(env.stalled_edge_ids)} edge(s), {len(env.stalled_lane_ids)} lane(s)"
        )
    if len(stalls) != len(env.stalled_edge_ids):
        # Every placed stall edge must correspond to a source stall geometry.
        # Refuse to emit incident records with fabricated (0.0, 0.0) coordinates.
        raise ValueError(
            "stall geometry count mismatch: "
            f"{len(stalls)} stall(s) vs {len(env.stalled_edge_ids)} placed edge(s)"
        )

    items = []
    for index, (edge, lane) in enumerate(zip(env.stalled_edge_ids, env.stalled_lane_ids)):
        geo = stalls[index]
        items.append(
            {
                "edge": edge,
                "lane": lane,
                "lat": float(geo["latitude"]),
                "lon": float(geo["longitude"]),
            }
        )
    return {
        "present": bool(items),
        "count": len(items),
        "items": items,
    }


def disable_teleport(sumocfg_path: Path, *, sim_end: float, seed: int) -> None:
    """Rewrite a scenario sumocfg so congestion is measured, never erased.

    * ``time-to-teleport=-1``: SUMO normally resolves gridlock by teleporting
      stuck vehicles away after 300 s — which silently deletes exactly the
      congestion a signal-plan evaluation must measure. With teleport off, a
      jammed vehicle stays jammed and its delay lands in the MOE.
    * ``collision.action=warn``: with jam-teleports off, collisions (rare;
      lane-change edge cases and stalled-vehicle scenarios) must not fall
      back to teleport-removal either — they are logged as warnings and the
      vehicles drive on, so demand is never quietly dropped.

    Applied to EVERY optimization scenario (not only incident runs).
    """
    from terasim_city.scenario.sumocfg_io import parse_sumocfg, write_sumocfg

    info = parse_sumocfg(sumocfg_path)
    processing = dict(info.processing_params)
    processing["time-to-teleport"] = "-1"
    processing["collision.action"] = "warn"
    write_sumocfg(
        sumocfg_path,
        net_file=info.net_file,
        route_file=info.route_files[0],
        additional_files=info.additional_files,
        gui_settings_file=info.gui_settings_file,
        processing_params=processing,
        routing_params=info.routing_params,
        report_params=info.report_params,
        sim_end=sim_end,
        seed=seed,
        step_length=info.step_length,
    )


__all__ = [
    "build_backlog",
    "build_incident",
    "count_warnings",
    "disable_teleport",
    "load_stalls",
    "snap_stalls_to_vehicle_lanes",
]


def snap_stalls_to_vehicle_lanes(net_file, stalls: list[dict]) -> list[dict]:
    """Snap stall coordinates onto real (non-internal) vehicle lanes.

    ``convertRoad`` maps a point near a junction onto an INTERNAL connector
    lane (edge id starting with ``:``); a vehicle cannot be added there, which
    would kill the whole run. Each stall is projected onto the nearest regular
    lane within ``radius`` metres instead; a stall with no such lane raises
    (an incident run without its incident answers the wrong question).

    Returns a new list (same dict shape) with snapped ``latitude``/``longitude``.
    """
    import sumolib

    radius = 25.0
    net = sumolib.net.readNet(str(net_file), withInternal=False)
    kept: list[dict] = []
    for stall in stalls:
        lat, lon = float(stall["latitude"]), float(stall["longitude"])
        x, y = net.convertLonLat2XY(lon, lat)
        neighbors = net.getNeighboringLanes(x, y, radius)
        candidates = [
            (dist, lane)
            for lane, dist in neighbors
            if not lane.getEdge().getID().startswith(":")
            and lane.allows("passenger")
        ]
        if not candidates:
            raise ValueError(
                f"stalled-vehicle coordinate ({lat:.5f}, {lon:.5f}) has no "
                f"drivable lane within {radius:g} m of the network — fix the "
                "incident coordinate; refusing to run an incident scenario "
                "without its incident"
            )
        _, lane = min(candidates, key=lambda c: c[0])
        sx, sy = sumolib.geomhelper.positionAtShapeOffset(
            lane.getShape(),
            sumolib.geomhelper.polygonOffsetWithMinimumDistanceToPoint(
                (x, y), lane.getShape()
            ),
        )
        s_lon, s_lat = net.convertXY2LonLat(sx, sy)
        kept.append({**stall, "latitude": s_lat, "longitude": s_lon})
    return kept
