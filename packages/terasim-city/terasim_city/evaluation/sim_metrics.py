"""Simulated signal-performance measures for a finished corridor optimization run.

:func:`compute_sim_metrics` re-runs the run's baseline trial and its optimized trial in
SUMO with virtual detectors on every signalized approach and returns a
``corridor_ui.sim_metrics.v1`` document (:func:`write_sim_metrics` writes it the way the
console's tooling always has):

  per approach   volume, arrivals on green / red, green occupancy, red occupancy
                 (first 5 s), split failures, queue length (censored flag),
                 approach delay, stops, phase utilization, degree of saturation
  per signal     the same, aggregated (volume-weighted; queue = worst approach)
  per corridor   mainline arrivals on green / red, worst degree of saturation,
                 end-to-end mainline travel time

All values are simulated estimates of the plan under the run's demand, not field
measurements. Definitions:
  - approach region: each controlled lane extended upstream along straight
    connections to 150 m (never past the upstream signal)
  - stop-bar zone: last 12 m before the stop line (may span two lanes)
  - arrival on green: a vehicle that crosses the stop line on a lane carrying the
    approach's primary movement, classified by the signal state at its projected
    free-flow arrival time (entry time + distance / speed limit, as advance-detector
    actuations are projected); yellow counts as red
  - green occupancy: share of green time with a vehicle in the stop-bar zone;
    red occupancy: same over the first 5 s of red; split failure: both > 80 %
  - queue: largest number of jammed vehicles in the approach region per cycle,
    averaged over cycles; censored when the jam reaches the region's upstream end
  - approach delay / stops: time loss accrued and stops made inside the region by
    vehicles that crossed the stop line
  - phase utilization: green needed (until a 3 s gap in stop-bar occupancy after
    the 5 s minimum green) / green scheduled
  - degree of saturation: primary-movement demand (vehicles entering the region,
    crossed or still queued) / (saturation flow x lanes x green / cycle); the
    saturation flow is measured from discharge during split-failure cycles when
    there are at least two, otherwise 1800 veh/h/lane

The re-runs reuse each trial's own files (``simulation.sumocfg``, its plan and the
run's scenario) and run headless through libsumo with ``--no-step-log``, so call this
in a process of its own (the service does): libsumo holds one simulation per process.
The measured horizon is the trial's own ``end``; ``deadline`` bounds the wall time.

Ported from the console's ``sim_metrics.py`` tool; the document is byte-compatible
with it. Differences, all of them no-ops for the runs that tool was used on: the
signals come from the resolved corridor when the run configured none (a from/to
corridor), the "optimized" plan is the exported one (the refine winner when the
refine stage picked it), trial directories recorded under another output root are
found inside the run directory, each trial's own program is made active if another
one is, the warm-up is capped at half the horizon, the scratch files are removed,
and the corridor id may be given (the tool had none to record).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

SIM_METRICS_SCHEMA = "corridor_ui.sim_metrics.v1"
RESULT_FILENAME = "corridor_optimization_result.json"
TRIAL_SUMOCFG = "simulation.sumocfg"
PLAN_TLS_FILENAME = "plan.tls.xml"
#: Plan labels of the document, in the order the tool writes them.
PLAN_LABELS = ("optimized", "baseline")

DEFAULT_WARMUP_S = 300.0
DEFAULT_SAMPLE_S = 0.5

SB_ZONE_M = 12.0
REGION_M = 150.0
SAT_FLOW = 1800.0
GAP_S = 3.0
MIN_GREEN_S = 5.0
RED_WINDOW_S = 5.0
OCC_THRESHOLD = 0.8

#: How many simulation steps pass between two wall-clock deadline checks.
_DEADLINE_CHECK_STEPS = 50


class SimMetricsUnavailable(ValueError):
    """The run has nothing the metrics can be computed from (the message says why)."""


def bearing(p0, p1):
    return math.degrees(math.atan2(p1[0] - p0[0], p1[1] - p0[1])) % 360


def dir_of(b):
    return ["NB", "EB", "SB", "WB"][int(((b + 45) % 360) // 90)]


def cfg_options(cfg_path: Path) -> dict:
    root = ET.parse(cfg_path).getroot()
    opts = {}
    for section in root:
        for opt in section:
            if "value" in opt.attrib:
                opts[opt.tag] = opt.attrib["value"]
    return opts


def resolve_files(value: str, base: Path) -> list[str]:
    out = []
    for item in value.split(","):
        item = item.strip()
        if item:
            p = Path(item)
            out.append(str(p if p.is_absolute() else (base / p).resolve()))
    return out


class Approach:
    def __init__(self, tls: str, direction: str):
        self.tls, self.dir = tls, direction
        self.lanes: list[str] = []          # every controlled incoming lane
        self.primary_lanes: list[str] = []  # lanes carrying the primary movement
        self.primary_links: list[int] = []
        self.lane_len: dict[str, float] = {}
        self.chains: dict[str, list] = {}       # controlled lane -> [lane objects, upstream first]
        self.region: list[str] = []             # every lane of the approach region
        self.down_dist: dict[str, float] = {}   # region lane -> distance from its end to the stop line
        self.speed: dict[str, float] = {}       # region lane -> speed limit (m/s)
        self.chain_len: dict[str, float] = {}
        self.edges: set[str] = set()              # controlled incoming edges
        self.region_edges: set[str] = set()       # edges of the region lanes
        self.tls_prefixes: tuple = ()             # internal-lane prefixes of the signal's junction(s)
        self.primary_pairs: set[tuple] = set()   # (incoming edge, outgoing edge) of the primary movement
        # time series (one entry per sample after warm-up)
        self.green: list[bool] = []
        self.yellow: list[bool] = []
        self.occ: list[bool] = []
        self.jam: list[int] = []
        self.full: list[bool] = []
        self.arrivals = 0
        self.arrivals_green = 0
        self.arrival_times: list[float] = []
        self.demand = 0                  # vehicles entering the region after warm-up, bound for the stop line
        self.demand_primary = 0          # ... whose route uses the primary movement
        self.cross_times: list[float] = []  # primary-movement stop-line crossings
        # per-vehicle tracking
        self.on: dict[str, dict] = {}
        self.delays: list[float] = []
        self.stops: list[int] = []


def _upstream_chain(lane, tls_nodes):
    """[upstream ..., lane]: follow straight predecessor lanes to REGION_M, stop at signals."""
    chain = [lane]
    total = lane.getLength()
    cur = lane
    while total < REGION_M and len(chain) < 12:
        start_node = cur.getEdge().getFromNode()
        if start_node.getID() in tls_nodes:
            break  # never extend past an upstream signal
        preds = []
        for pl in cur.getIncoming():
            if pl.getEdge().getFunction() == "internal":
                continue
            d = "s"
            for c in pl.getOutgoing():
                if c.getToLane() is cur:
                    d = c.getDirection()
            preds.append((0 if d == "s" else 1, pl.getID(), pl))
        if not preds:
            break
        preds.sort(key=lambda x: (x[0], x[1]))
        nxt = preds[0][2]
        if any(x is nxt for x in chain):
            break
        chain.insert(0, nxt)
        total += nxt.getLength()
        cur = nxt
    return chain


def build_approaches(net, tls_ids):
    tls_nodes = set()
    for tid in tls_ids:
        for c in net.getTLS(tid).getConnections():
            tls_nodes.add(c[0].getEdge().getToNode().getID())
    approaches = {}
    for tid in tls_ids:
        tls = net.getTLS(tid)
        per_dir: dict[str, Approach] = {}
        lane_moves: dict[str, list[tuple[str, int]]] = {}
        pair_links: dict[tuple, set] = {}
        lane_obj = {}
        for in_lane, out_lane, link in tls.getConnections():
            edge = in_lane.getEdge()
            if edge.getFunction() == "internal":
                continue
            shp = in_lane.getShape()
            d = dir_of(bearing(shp[-2], shp[-1]))
            ap = per_dir.setdefault(d, Approach(tid, d))
            lid = in_lane.getID()
            lane_obj[lid] = in_lane
            if lid not in ap.lanes:
                ap.lanes.append(lid)
                ap.lane_len[lid] = in_lane.getLength()
            conn = None
            for c in in_lane.getOutgoing():
                if c.getToLane() is out_lane:
                    conn = c
                    break
            move = conn.getDirection() if conn is not None else "s"
            lane_moves.setdefault(lid, []).append((move, link))
            pair_links.setdefault((edge.getID(), out_lane.getEdge().getID()), set()).add(link)
        for ap in per_dir.values():
            moves = {}
            for lid in ap.lanes:
                for move, link in lane_moves.get(lid, []):
                    key = "s" if move == "s" else ("l" if move in ("l", "L", "t") else "r")
                    moves.setdefault(key, []).append((lid, link))
            primary = "s" if "s" in moves else max(moves, key=lambda k: len(moves[k]))
            ap.primary_links = sorted({link for _, link in moves[primary]})
            ap.primary_lanes = sorted({lid for lid, _ in moves[primary]})
            ap.edges = {lane_obj[lid].getEdge().getID() for lid in ap.lanes}
            ap.primary_pairs = {pair for pair, links in pair_links.items()
                                if pair[0] in ap.edges and links & set(ap.primary_links)}
            ap.tls_prefixes = tuple(sorted({f":{lane_obj[lid].getEdge().getToNode().getID()}_" for lid in ap.lanes}))
            for lid in ap.lanes:
                chain = _upstream_chain(lane_obj[lid], tls_nodes - {lane_obj[lid].getEdge().getToNode().getID()})
                ap.chains[lid] = chain
                ap.chain_len[lid] = sum(x.getLength() for x in chain)
                down = 0.0
                for x in reversed(chain):
                    xid = x.getID()
                    ap.down_dist[xid] = min(ap.down_dist.get(xid, 1e9), down)
                    ap.speed[xid] = x.getSpeed()
                    if xid not in ap.region:
                        ap.region.append(xid)
                        ap.region_edges.add(x.getEdge().getID())
                    down += x.getLength()
        approaches[tid] = per_dir
    return approaches


def _span(chain, length_m):
    """Suffix of a lane chain covering the last length_m metres: (lane ids, pos on first lane)."""
    lanes, need = [], length_m
    for x in reversed(chain):
        lanes.insert(0, x)
        if x.getLength() >= need:
            return [y.getID() for y in lanes], x.getLength() - need
        need -= x.getLength()
    return [y.getID() for y in lanes], 0.0


def write_detectors(approaches, path: Path, out_file: str):
    root = ET.Element("additional")
    for per_dir in approaches.values():
        for ap in per_dir.values():
            for lid, chain in ap.chains.items():
                for kind, zone in (("sb", SB_ZONE_M), ("q", REGION_M * 10)):
                    ids, pos = _span(chain, zone)
                    attrs = {"id": f"{kind}|{lid}", "pos": f"{pos:.2f}",
                             "endPos": f"{chain[-1].getLength():.2f}", "period": "3600", "file": out_file}
                    if len(ids) == 1:
                        attrs["lane"] = ids[0]
                    else:
                        attrs["lanes"] = " ".join(ids)
                    ET.SubElement(root, "laneAreaDetector", **attrs)
    ET.ElementTree(root).write(path)


def _route_at_signal(ts, vid, ap):
    """(will the vehicle reach this stop line?, on the primary movement?) from its route."""
    try:
        route = ts.vehicle.getRoute(vid)
        idx = ts.vehicle.getRouteIndex(vid)
    except Exception:
        return False, False
    for j in range(max(0, idx), len(route)):
        if route[j] in ap.edges:
            nxt = route[j + 1] if j + 1 < len(route) else None
            return True, (route[j], nxt) in ap.primary_pairs
    return False, False


def _crossed(ts, vid, ap, rec):
    """(crossed the stop line?, on the primary movement?) for a vehicle that left the region."""
    try:
        road = ts.vehicle.getRoadID(vid)
        route = ts.vehicle.getRoute(vid)
        idx = ts.vehicle.getRouteIndex(vid)
    except Exception:  # already left the network: fall back to the last lane seen
        return rec.get("lane") in ap.lanes, rec.get("lane") in ap.primary_lanes
    # On an internal (junction) edge the route index still points at the incoming edge.
    passed = route[: idx + 1] if road.startswith(":") else route[:idx]
    for j in range(len(passed) - 1, -1, -1):
        if passed[j] in ap.edges:
            nxt = route[j + 1] if j + 1 < len(route) else None
            return True, (passed[j], nxt) in ap.primary_pairs
    return False, False


def _trial_programs(trial_dir: Path) -> dict[str, str]:
    """``{tls_id: programID}`` of the plan file the trial loaded last (empty when absent)."""
    path = trial_dir / PLAN_TLS_FILENAME
    if not path.is_file():
        return {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {}
    return {
        tl.attrib["id"]: tl.attrib["programID"]
        for tl in root.iter("tlLogic")
        if tl.attrib.get("id") and tl.attrib.get("programID")
    }


def _activate_trial_programs(ts, trial_dir: Path, tls_ids) -> None:
    """Run the trial's own program at every signal, as its evaluation did.

    The plan file comes last among the trial's additional files, so its program is
    normally the active one already; anything else is switched, like the evaluation's
    ``setProgram``.
    """
    for tid, program in _trial_programs(trial_dir).items():
        if tid in tls_ids and ts.trafficlight.getProgram(tid) != program:
            ts.trafficlight.setProgram(tid, program)


def simulate(trial_dir: Path, net, tls_ids, warmup: float, sample_s: float, *,
             scratch: Path, deadline: Optional[float] = None):
    import libsumo as ts

    cfg = trial_dir / TRIAL_SUMOCFG
    opts = cfg_options(cfg)
    base = cfg.parent
    approaches = build_approaches(net, tls_ids)
    tmp = scratch
    tmp.mkdir(parents=True, exist_ok=True)
    det = tmp / "detectors.add.xml"
    write_detectors(approaches, det, str(tmp / "e2.xml"))
    adds = resolve_files(opts.get("additional-files", ""), base) + [str(det)]
    cmd = ["sumo", "-c", str(cfg), "--additional-files", ",".join(adds),
           "--no-step-log", "true", "--no-warnings", "true", "--duration-log.disable", "true"]
    for key in list(opts):
        if key.endswith("-output") or key in ("statistic-output", "log", "error-log", "message-log"):
            cmd += [f"--{key}", str(tmp / f"{key}.xml")]
    end = float(opts.get("end", "1800"))
    step = float(opts.get("step-length", "1"))
    every = max(1, round(sample_s / step))
    dt_s = every * step
    ts.start(cmd)
    crossings: dict[str, list[tuple[str, str, float]]] = {}
    phase0: dict[str, list[float]] = {t: [] for t in tls_ids}
    last_phase = {t: None for t in tls_ids}
    times: list[float] = []
    k = 0
    t = ts.simulation.getTime()
    try:
        _activate_trial_programs(ts, trial_dir, set(tls_ids))
        while t < end and ts.simulation.getMinExpectedNumber() > 0:
            ts.simulationStep()
            k += 1
            t = ts.simulation.getTime()
            if deadline is not None and k % _DEADLINE_CHECK_STEPS == 0 and time.monotonic() > deadline:
                raise TimeoutError(
                    f"re-simulating {trial_dir.name} ran out of wall time at t={t:g} s of {end:g} s"
                )
            if k % every:
                continue
            if t >= warmup:
                times.append(t)
            for tid in tls_ids:
                ph = ts.trafficlight.getPhase(tid)
                if ph == 0 and last_phase[tid] not in (None, 0):
                    phase0[tid].append(t)
                last_phase[tid] = ph
                state = ts.trafficlight.getRedYellowGreenState(tid)
                for ap in approaches[tid].values():
                    chars = [state[i] for i in ap.primary_links if i < len(state)]
                    green = any(c in "Gg" for c in chars)
                    yellow = (not green) and any(c in "yY" for c in chars)
                    # vehicles that entered the approach region
                    for lid in ap.region:
                        for vid in ts.lane.getLastStepVehicleIDs(lid):
                            rec = ap.on.get(vid)
                            if rec is None:
                                loss = ts.vehicle.getTimeLoss(vid)
                                dist = ap.down_dist.get(lid, 0.0) + max(0.0, ts.lane.getLength(lid) - ts.vehicle.getLanePosition(vid))
                                rec = ap.on[vid] = {"t0": t, "loss0": loss, "loss": loss, "stops": 0, "halted": False,
                                                    "t_arr": t + dist / max(1.0, ap.speed.get(lid, 13.9))}
                                if t >= warmup:
                                    reaches, primary = _route_at_signal(ts, vid, ap)
                                    ap.demand += int(reaches)
                                    ap.demand_primary += int(primary)
                            rec["lane"] = lid
                    # follow tracked vehicles until they cross the stop line or leave the region
                    for vid in list(ap.on):
                        rec = ap.on[vid]
                        try:
                            road = ts.vehicle.getRoadID(vid)
                        except Exception:
                            road = None
                        on_signal = road is not None and road.startswith(ap.tls_prefixes)
                        inside = road is not None and not on_signal and (
                            road in ap.region_edges or road.startswith(":"))
                        if inside:
                            loss = ts.vehicle.getTimeLoss(vid)
                            speed = ts.vehicle.getSpeed(vid)
                            rec["loss"] = loss
                            if speed < 0.1 and not rec["halted"]:
                                rec["stops"] += 1
                                rec["halted"] = True
                            elif speed > 1.0:
                                rec["halted"] = False
                            continue
                        ap.on.pop(vid)
                        if road is None:
                            crossed, primary = rec.get("lane") in ap.lanes, rec.get("lane") in ap.primary_lanes
                        else:
                            crossed, primary = _crossed(ts, vid, ap, rec)
                        if not crossed:
                            continue  # turned off upstream without reaching the stop line
                        crossings.setdefault(vid, []).append((tid, ap.dir, t))
                        if t < warmup:
                            continue
                        loss = ts.vehicle.getTimeLoss(vid) if road is not None else rec["loss"]
                        ap.delays.append(max(0.0, loss - rec["loss0"]))
                        ap.stops.append(rec["stops"])
                        if primary:
                            ap.arrival_times.append(min(rec["t_arr"], t))
                            ap.cross_times.append(t)
                    if t < warmup:
                        continue
                    occ = any(ts.lanearea.getLastStepVehicleNumber(f"sb|{lid}") > 0 for lid in ap.primary_lanes)
                    jam = 0
                    full = False
                    for lid in ap.lanes:
                        jam += ts.lanearea.getJamLengthVehicle(f"q|{lid}")
                        if ts.lanearea.getJamLengthMeters(f"q|{lid}") >= ap.chain_len[lid] - 7.5:
                            full = True
                    ap.green.append(green)
                    ap.yellow.append(yellow)
                    ap.occ.append(occ)
                    ap.jam.append(jam)
                    ap.full.append(full)
    finally:
        ts.close()
    # classify projected arrivals against the recorded signal timeline
    for per_dir in approaches.values():
        for ap in per_dir.values():
            for ta in ap.arrival_times:
                if ta < warmup or not times:
                    continue
                i = min(len(times) - 1, max(0, int(round((ta - times[0]) / dt_s))))
                ap.arrivals += 1
                if ap.green[i]:
                    ap.arrivals_green += 1
    return approaches, crossings, phase0, {"end": end, "step": step, "sample_s": dt_s, "times": times}


def cycles_of(ap: Approach, dt_s: float, times: list[float]):
    starts = [i for i in range(1, len(ap.green)) if ap.green[i] and not ap.green[i - 1]]
    out = []
    for a, b in zip(starts, starts[1:]):
        g = [i for i in range(a, b) if ap.green[i]]
        if not g:
            continue
        g_end = g[-1] + 1
        t_a, t_g = times[a], times[min(g_end, len(times) - 1)]
        discharged = sum(1 for x in ap.cross_times if t_a <= x < t_g + 3.0)
        red = [i for i in range(g_end, b) if not ap.green[i] and not ap.yellow[i]]
        red5 = red[: max(1, int(round(RED_WINDOW_S / dt_s)))]
        gor = sum(ap.occ[i] for i in g) / len(g)
        ror = sum(ap.occ[i] for i in red5) / len(red5) if red5 else 0.0
        # virtual gap-out: first 3 s gap in stop-bar occupancy after the minimum green
        needed = len(g) * dt_s
        last_occ = a
        for i in g:
            if ap.occ[i]:
                last_occ = i
            elapsed = (i - a + 1) * dt_s
            if elapsed >= MIN_GREEN_S and (i - last_occ) * dt_s >= GAP_S:
                needed = max(MIN_GREEN_S, (last_occ - a + 1) * dt_s)
                break
        out.append({
            "green_s": len(g) * dt_s, "cycle_s": (b - a) * dt_s, "gor": gor, "ror5": ror,
            "split_failure": gor > OCC_THRESHOLD and ror > OCC_THRESHOLD,
            "queue": max(ap.jam[a:b]) if b > a else 0, "censored": any(ap.full[a:b]),
            "discharged": discharged,
            "util": min(1.0, needed / (len(g) * dt_s)),
        })
    return out


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def r(x, n=1):
    return None if x is None else round(x, n)


def summarize(approaches, crossings, meta, warmup, net, tls_ids, mainline):
    hours = max(1e-9, (meta["end"] - warmup) / 3600.0)
    dt_s = meta["sample_s"]
    intersections = {}
    corridor_arr = corridor_green = 0
    for tid in tls_ids:
        aps = {}
        for d, ap in approaches[tid].items():
            cyc = cycles_of(ap, dt_s, meta["times"])
            vol = len(ap.delays) / hours                 # throughput: vehicles crossing the stop line
            demand_primary = ap.demand_primary / hours   # demand on the primary movement
            g = mean([c["green_s"] for c in cyc])
            c_len = mean([c["cycle_s"] for c in cyc])
            lanes = max(1, len(ap.primary_lanes))
            failing = sorted(c["discharged"] / c["green_s"] * 3600.0 / lanes for c in cyc if c["split_failure"] and c["green_s"] > 0)
            if len(failing) >= 2:
                sat = min(2000.0, max(600.0, failing[len(failing) // 2]))
                sat_src = "measured"
            else:
                sat, sat_src = SAT_FLOW, "assumed"
            cap = sat * lanes * (g / c_len) if g and c_len else None
            sf = sum(1 for c in cyc if c["split_failure"])
            aps[d] = {
                "lanes": len(ap.primary_lanes),
                "volume_veh_h": r(vol, 0),
                "arrivals": ap.arrivals,
                "aog_pct": r(100.0 * ap.arrivals_green / ap.arrivals) if ap.arrivals else None,
                "aor_pct": r(100.0 * (ap.arrivals - ap.arrivals_green) / ap.arrivals) if ap.arrivals else None,
                "gor_pct": r(100.0 * mean([c["gor"] for c in cyc])) if cyc else None,
                "ror5_pct": r(100.0 * mean([c["ror5"] for c in cyc])) if cyc else None,
                "split_failures": sf,
                "split_failures_per_hr": r(sf / hours),
                "cycles": len(cyc),
                "queue_veh": r(mean([c["queue"] for c in cyc])) if cyc else None,
                "queue_censored": any(c["censored"] for c in cyc),
                "phase_util_pct": r(100.0 * mean([c["util"] for c in cyc])) if cyc else None,
                "dos": r(demand_primary / cap, 2) if cap else None,
                "demand_veh_h": r(ap.demand / hours, 0),
                "sat_flow_veh_h_lane": r(sat, 0), "sat_flow_source": sat_src,
                "control_delay_s": r(mean(ap.delays)),
                "stops_per_veh": r(mean(ap.stops), 2),
                "vehicles": len(ap.delays),
                "green_s": r(g), "cycle_s": r(c_len),
            }
            if d in mainline:
                corridor_arr += ap.arrivals
                corridor_green += ap.arrivals_green
        w = {d: (a["vehicles"] or 0) for d, a in aps.items()}
        tot = sum(w.values()) or 1

        def wmean(field, digits=1):
            vals = [(aps[d][field], w[d]) for d in aps if aps[d][field] is not None]
            den = sum(x for _, x in vals)
            return r(sum(v * x for v, x in vals) / den, digits) if den else None

        dos_vals = [a["dos"] for a in aps.values() if a["dos"] is not None]
        q_vals = [a["queue_veh"] for a in aps.values() if a["queue_veh"] is not None]
        arr = sum(a["arrivals"] for a in aps.values())
        arr_g = sum(approaches[tid][d].arrivals_green for d in aps)
        intersections[tid] = {
            "control_delay_s": wmean("control_delay_s"),
            "stops_per_veh": wmean("stops_per_veh", 2),
            "volume_veh_h": r(sum((a["volume_veh_h"] or 0) for a in aps.values()), 0),
            "dos": max(dos_vals) if dos_vals else None,
            "aog_pct": r(100.0 * arr_g / arr) if arr else None,
            "aor_pct": r(100.0 * (arr - arr_g) / arr) if arr else None,
            "gor_pct": wmean("gor_pct"),
            "ror5_pct": wmean("ror5_pct"),
            "split_failures_per_hr": r(sum(a["split_failures"] for a in aps.values()) / hours),
            "queue_veh": max(q_vals) if q_vals else None,
            "queue_censored": any(a["queue_censored"] for a in aps.values()),
            "phase_util_pct": wmean("phase_util_pct"),
            "vehicles": tot,
            "approaches": aps,
        }
    # mainline travel time: sum of mean segment times between consecutive signals
    order = tls_ids
    tt, tt_n = {}, {}
    for d in mainline:
        seq = list(reversed(order)) if d in ("NB", "WB") else list(order)
        total, n_min = 0.0, None
        for a, b in zip(seq, seq[1:]):
            seg = []
            for vid, xs in crossings.items():
                ta = [t for tid, dd, t in xs if tid == a and dd == d and t >= warmup]
                tb = [t for tid, dd, t in xs if tid == b and dd == d]
                if ta:
                    later = [x for x in tb if x > ta[0]]
                    if later:
                        seg.append(later[0] - ta[0])
            if len(seg) < 5:
                total = None
                break
            total += sum(seg) / len(seg)
            n_min = len(seg) if n_min is None else min(n_min, len(seg))
        tt[d] = r(total)
        tt_n[d] = n_min or 0
    dos_all = [(v["dos"], tid) for tid, v in intersections.items() if v["dos"] is not None]
    worst = max(dos_all) if dos_all else (None, None)
    corridor = {
        "aog_pct": r(100.0 * corridor_green / corridor_arr) if corridor_arr else None,
        "aor_pct": r(100.0 * (corridor_arr - corridor_green) / corridor_arr) if corridor_arr else None,
        "dos_max": worst[0], "dos_max_tls": worst[1],
        "travel_time_s": tt,
        "travel_time_min_samples": tt_n,
    }
    return corridor, intersections


# ---------------------------------------------------------------------------
# The run's trials
# ---------------------------------------------------------------------------

def _corridor_tls_ids(result: dict) -> list[str]:
    """The run's signals: the configured TLS ids, else those of the resolved corridor."""
    configured = [str(t) for t in ((result.get("config") or {}).get("tls_ids") or [])]
    if configured:
        return configured
    tls_ids: list[str] = []
    for item in result.get("resolved_corridor") or []:
        tls_id = item.get("tls_id") if isinstance(item, dict) else None
        if tls_id and str(tls_id) not in tls_ids:
            tls_ids.append(str(tls_id))
    return tls_ids


def _locate_trial(run_dir: Path, recorded) -> Optional[Path]:
    """A trial directory as recorded, else the same trial inside ``run_dir``.

    Trial directories are recorded as absolute paths; a run directory read under
    another root (a moved volume) still holds them under ``trials/`` or
    ``trials_refine/``.
    """
    if not recorded:
        return None
    path = Path(str(recorded))
    if (path / TRIAL_SUMOCFG).is_file():
        return path
    parts = path.parts
    for anchor in ("trials", "trials_refine"):
        if anchor in parts:
            candidate = run_dir.joinpath(*parts[len(parts) - parts[::-1].index(anchor) - 1:])
            if (candidate / TRIAL_SUMOCFG).is_file():
                return candidate
    return None


def _optimized_trial(result: dict) -> tuple[Optional[str], Optional[str]]:
    """(plan id, recorded trial dir) of the exported plan: the refine winner, else ``best``."""
    if result.get("export_source") == "refine":
        refine = result.get("refine") or {}
        winner = refine.get("winner_plan_id")
        for entry in (refine.get("baseline"), *(refine.get("evaluations") or ())):
            if isinstance(entry, dict) and entry.get("plan_id") == winner:
                return winner, entry.get("trial_dir")
        raise SimMetricsUnavailable(
            f"the refine winner {winner!r} is not among the refine stage's evaluations"
        )
    best = result.get("best") or {}
    return best.get("plan_id"), best.get("trial_dir")


def _baseline_trial(result: dict) -> tuple[Optional[str], Optional[str]]:
    """(plan id, recorded trial dir) of the baseline: its first (coarse) ledger record."""
    plan_id = (result.get("baseline") or {}).get("plan_id")
    for record in result.get("trials") or []:
        if record.get("plan_id") == plan_id and "stage" not in record:
            return plan_id, record.get("trial_dir")
    return plan_id, None


def _plan_trials(result: dict, run_dir: Path, plans: Sequence[str]) -> dict[str, tuple[str, Path]]:
    """``{label: (plan_id, trial_dir)}`` for the requested labels, in the tool's order."""
    unknown = set(plans) - set(PLAN_LABELS)
    if unknown:
        raise ValueError(f"unknown plan label(s) {sorted(unknown)}; choose from {PLAN_LABELS}")
    chosen: dict[str, tuple[str, Path]] = {}
    for label in PLAN_LABELS:
        if label not in plans:
            continue
        plan_id, recorded = _optimized_trial(result) if label == "optimized" else _baseline_trial(result)
        trial_dir = _locate_trial(run_dir, recorded)
        if plan_id is None or trial_dir is None:
            raise SimMetricsUnavailable(
                f"the {label} plan {plan_id!r} has no trial with a {TRIAL_SUMOCFG} in {run_dir}"
            )
        chosen[label] = (str(plan_id), trial_dir)
    if not chosen:
        raise ValueError("no plan to simulate")
    return chosen


def _tmc_corridor_id(result: dict) -> Optional[str]:
    """The corridor id the run's TMC file names (best effort)."""
    tmc_json = (result.get("config") or {}).get("tmc_json")
    if not tmc_json:
        return None
    try:
        value = json.loads(Path(tmc_json).read_text(encoding="utf-8")).get("corridor_id")
    except Exception:  # noqa: BLE001 - informative only
        return None
    return value if isinstance(value, str) and value else None


def _signal_coords(net, tls_id: str) -> tuple[float, float]:
    """Where a signal is: the mean position of the junctions it controls."""
    try:
        connections = net.getTLS(tls_id).getConnections()
    except KeyError as exc:
        raise SimMetricsUnavailable(f"signal {tls_id!r} is not in the run's network") from exc
    nodes = {c[0].getEdge().getToNode().getID(): c[0].getEdge().getToNode() for c in connections}
    if not nodes:
        raise SimMetricsUnavailable(f"signal {tls_id!r} controls no connection")
    coords = [nodes[node_id].getCoord() for node_id in sorted(nodes)]
    return (sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def compute_sim_metrics(
    run_dir,
    *,
    plans: Sequence[str] = PLAN_LABELS,
    run_id: Optional[str] = None,
    corridor_id: Optional[str] = None,
    warmup_s: float = DEFAULT_WARMUP_S,
    sample_s: float = DEFAULT_SAMPLE_S,
    deadline: Optional[float] = None,
) -> dict:
    """The ``corridor_ui.sim_metrics.v1`` document of the run in ``run_dir``.

    ``plans`` picks the re-runs ("optimized": the exported plan, "baseline": the run's
    baseline); ``run_id`` defaults to the directory name and ``corridor_id`` to the one
    the run's TMC file names. ``deadline`` is a ``time.monotonic()`` value past which a
    re-run stops with ``TimeoutError``. Raises :class:`SimMetricsUnavailable` when the
    run lacks what the re-runs need (its result, a trial, its signals).
    """
    import sumolib

    run_dir = Path(run_dir)
    result_path = run_dir / RESULT_FILENAME
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SimMetricsUnavailable(f"the run has no {RESULT_FILENAME}") from exc
    except ValueError as exc:
        raise SimMetricsUnavailable(f"{RESULT_FILENAME} is not valid JSON: {exc}") from exc
    tls_ids = _corridor_tls_ids(result)
    if not tls_ids:
        raise SimMetricsUnavailable("the run names no signal")
    resolved = {rc["tls_id"]: rc for rc in result.get("resolved_corridor", []) if rc.get("tls_id")}
    plan_dirs = _plan_trials(result, run_dir, plans)

    first_cfg = next(iter(plan_dirs.values()))[1] / TRIAL_SUMOCFG
    opts = cfg_options(first_cfg)
    if "net-file" not in opts:
        raise SimMetricsUnavailable(f"{first_cfg} names no net-file")
    net_file = resolve_files(opts["net-file"], first_cfg.parent)[0]
    net = sumolib.net.readNet(net_file, withPrograms=True)
    # corridor order and mainline from the signal coordinates (as resolved by the run)
    coords = {tid: _signal_coords(net, tid) for tid in tls_ids}
    xs = [coords[t][0] for t in tls_ids]
    ys = [coords[t][1] for t in tls_ids]
    ns = (max(ys) - min(ys)) >= (max(xs) - min(xs))
    order = sorted(tls_ids, key=lambda t: -coords[t][1] if ns else coords[t][0])
    mainline = ["NB", "SB"] if ns else ["EB", "WB"]
    # A short run still gets a measured window: the warm-up takes at most half of it.
    end_s = float(opts.get("end", "1800"))
    warmup = min(float(warmup_s), end_s / 2.0)

    plans_doc = {}
    sim_meta = None
    with tempfile.TemporaryDirectory(prefix=".sim_metrics_", dir=run_dir) as scratch_root:
        for label, (plan_id, trial_dir) in plan_dirs.items():
            approaches, crossings, phase0, sim_meta = simulate(
                trial_dir, net, order, warmup, sample_s,
                scratch=Path(scratch_root) / label, deadline=deadline,
            )
            corridor, inters = summarize(approaches, crossings, sim_meta, warmup, net, order, mainline)
            for tid, v in inters.items():
                if tid in resolved:
                    v["intersection_id"] = resolved[tid].get("intersection_id") or resolved[tid].get("ref")
            plans_doc[label] = {"plan_id": plan_id, "corridor": corridor, "intersections": inters,
                                "phase0_starts_s": {t: v[:3] for t, v in phase0.items()}}
            logger.info(
                "%s: %s aog=%s dos_max=%s tt=%s",
                label, plan_id, corridor["aog_pct"], corridor["dos_max"], corridor["travel_time_s"],
            )

    return {
        "schema": SIM_METRICS_SCHEMA,
        "corridor_id": corridor_id or result.get("corridor_id")
        or (result.get("config") or {}).get("corridor_id") or _tmc_corridor_id(result),
        "run_id": run_id or run_dir.name,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": {
            "description": "SUMO re-simulation of the run's baseline and best trial with virtual lane-area detectors",
            "warmup_s": warmup, "end_s": sim_meta["end"], "sample_s": sim_meta["sample_s"],
            "stop_bar_zone_m": SB_ZONE_M, "approach_region_m": REGION_M, "arrival": "projected at the speed limit from region entry",
            "saturation_flow_veh_h_lane": "measured in split-failure cycles, else 1800", "gap_out_s": GAP_S, "min_green_s": MIN_GREEN_S,
            "red_window_s": RED_WINDOW_S, "split_failure_occupancy": OCC_THRESHOLD,
        },
        "corridor_order": order,
        "mainline": mainline,
        "plans": plans_doc,
    }


def write_sim_metrics(doc: dict, path) -> Path:
    """Write ``doc`` as the tool does (one-space indent, trailing newline), atomically."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    shutil.move(str(tmp), str(path))
    return path


__all__ = [
    "PLAN_LABELS",
    "SIM_METRICS_SCHEMA",
    "SimMetricsUnavailable",
    "compute_sim_metrics",
    "write_sim_metrics",
]
