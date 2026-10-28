#!/usr/bin/env python3
"""TeraSim Metropolis v0.1.0 - end-to-end acceptance run on synthetic data.

Exercises the Phase 1 acceptance chain exactly the way a VSS agent drives it, through the
public interfaces only (MCP tools, the HTTP API, the TMC collector):

  input data -> environment bring-up (map + timing-plan upload, OpenDRIVE ingest)
             -> TMC collector pulls VSS behavior objects and injects traffic snapshots
             -> On-Demand mode: one optimization tool call
             -> Scheduled mode: the same tool invoked on a fixed cadence
                (plus the optional built-in scheduler)
             -> KPI outputs (control delay, stops, weighted cost, simulated signal metrics)
             -> NEMA timing plan + PDF timing report
  an eight-signal corridor with 32 cameras defined by one camera inventory
  (apps/deploy/city/camera-config) and optimized as a whole,
  a short run of the parameterized adversarial driving environment on the demo corridor,
  and an optimization with the Neural NDE re-evaluation stage switched on by deployment
  settings (behavior models, A1).

Every input is synthetic: the demo corridor in examples/corridor_demo (three signals on a
generated arterial), a generated TMC spec, generated VSS behavior objects (served by a small
local stand-in for the VSS behavior API) and a generated NEMA timing plan.

Local mode (default) starts everything on free localhost ports from the installed packages:
redis-server, terasim-service, the MCP server (terasim-agent-skill), the TMC collector
(terasim-tmc-service) and the VSS behavior stand-in.

    python examples/acceptance/run_acceptance.py --out /tmp/terasim-acceptance

Remote mode checks an existing deployment that runs the VSS behavior stub (for example the
compose "stub" profile); it uses the map baked into the image:

    python examples/acceptance/run_acceptance.py --remote \
        --service http://HOST:8100 --mcp http://HOST:9911/mcp --tmc http://HOST:8200

Neural NDE profile: with the Neural NDE runtime module and its checkpoint installed (both are
installed separately from this repository), --neural-nde runs the re-evaluation stage on the
GPU (criterion A1-3) and an optimize tool call that asks for Neural NDE traffic with
traffic_model="cnde" (criterion A1-4), on top of the environment and health checks:

    python examples/acceptance/run_acceptance.py --neural-nde --out /tmp/terasim-acceptance-nnde

Results: <out>/acceptance_report.json and <out>/acceptance_report.md (one row per criterion,
with the measured evidence). The exit code is 0 only when no criterion failed.

Requirements: Python 3.10-3.12 with the release packages installed (terasim, terasim-nde-nade,
terasim-city, terasim-service, terasim-agent-skill[mcp,report], terasim-tmc-service), SUMO
1.23.1 (the eclipse-sumo wheel), and redis-server on PATH for local mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
ADVERSITY_DIR = REPO_ROOT / "configs" / "adversities"
CAMERA_CONFIG_DIR = REPO_ROOT / "apps" / "deploy" / "city" / "camera-config"
CORRIDOR_INVENTORY = CAMERA_CONFIG_DIR / "examples" / "arterial_32.yaml"

CORRIDOR_ID = "demo_arterial"
HD_CORRIDOR_ID = "demo_arterial_hd"
NNDE_CORRIDOR_ID = "demo_arterial_nnde"  # the run with the Neural NDE re-evaluation stage
DEFAULT_WEIGHTS = {"control_delay": 0.7, "stops": 0.3}
CUSTOM_WEIGHTS = {"control_delay": 0.5, "stops": 0.5}
NEMA_TIMING_SCHEMA = "terasim.nema_timing.v1"
EXPECTED_TOOLS = {
    "upload_traffic_snapshot", "upload_signal_plan", "upload_map",
    "optimize_intersection", "optimize_intersection_by_id", "optimize_corridor",
    "register_intersection", "list_intersections", "optimize_registered_intersection",
}
MAP_FILES = ("demo.sumocfg", "demo.net.xml", "demo.tls.xml", "demo.rou.xml")

# The three synthetic intersections of the demo corridor (signals J1 / J2 / J3).
INTERSECTIONS = [
    {"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St", "lat": 37.335,
     "lon": -121.90339, "tls_id": "J1", "offset_s": 0},
    {"intersection_id": "i2", "name": "Arterial Ave & 2nd Cross St", "lat": 37.335,
     "lon": -121.9, "tls_id": "J2", "offset_s": 20},
    {"intersection_id": "i3", "name": "Arterial Ave & 3rd Cross St", "lat": 37.335,
     "lon": -121.89661, "tls_id": "J3", "offset_s": 40},
]

# Road segments of the demo network around each signal: travel direction -> (in, out).
APPROACH_EDGES = {
    "i1": {"East": ("EB_1", "EB_2"), "West": ("WB_3", "WB_4"),
           "North": ("NB_J1_1", "NB_J1_2"), "South": ("SB_J1_1", "SB_J1_2")},
    "i2": {"East": ("EB_2", "EB_3"), "West": ("WB_2", "WB_3"),
           "North": ("NB_J2_1", "NB_J2_2"), "South": ("SB_J2_1", "SB_J2_2")},
    "i3": {"East": ("EB_3", "EB_4"), "West": ("WB_1", "WB_2"),
           "North": ("NB_J3_1", "NB_J3_2"), "South": ("SB_J3_1", "SB_J3_2")},
}
# Turning movement for (entry travel direction, exit travel direction), right-hand traffic.
MOVEMENTS = {
    "East - East": "Straight", "East - North": "Left", "East - South": "Right",
    "West - West": "Straight", "West - South": "Left", "West - North": "Right",
    "North - North": "Straight", "North - West": "Left", "North - East": "Right",
    "South - South": "Straight", "South - East": "Left", "South - West": "Right",
}
TURN_EXIT = {  # entry direction -> {movement: exit direction}
    entry: {move: path.split(" - ")[1] for path, move in MOVEMENTS.items()
            if path.startswith(entry + " ")}
    for entry in ("East", "West", "North", "South")
}
OBSERVATION_INTERVAL = "1m"  # short windows keep the run short; production uses 10 minutes


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@dataclass
class Criterion:
    cid: str
    component: str
    title: str
    status: str = "NOT RUN"
    evidence: dict = field(default_factory=dict)
    detail: str = ""
    seconds: float = 0.0


class Report:
    def __init__(self) -> None:
        self.criteria: dict[str, Criterion] = {}
        self.started = datetime.now(timezone.utc)
        self.meta: dict[str, Any] = {}

    def define(self, cid: str, component: str, title: str) -> None:
        self.criteria[cid] = Criterion(cid, component, title)

    def record(self, cid: str, ok: bool, evidence: dict | None = None, detail: str = "",
               seconds: float = 0.0, skipped: bool = False) -> None:
        crit = self.criteria[cid]
        crit.status = "SKIP" if skipped else ("PASS" if ok else "FAIL")
        crit.evidence = evidence or {}
        crit.detail = detail
        crit.seconds = round(seconds, 1)
        mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[crit.status]
        print(f"[{mark}] {cid} {crit.title}" + (f" - {detail}" if detail else ""), flush=True)

    @property
    def failed(self) -> list[Criterion]:
        return [c for c in self.criteria.values() if c.status in ("FAIL", "NOT RUN")]

    def write(self, out_dir: Path) -> None:
        finished = datetime.now(timezone.utc)
        counts: dict[str, int] = {}
        for crit in self.criteria.values():
            counts[crit.status] = counts.get(crit.status, 0) + 1
        doc = {
            "schema": "terasim.acceptance_report.v1",
            "started_at": self.started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_s": round((finished - self.started).total_seconds(), 1),
            "summary": counts,
            "result": "PASS" if not self.failed else "FAIL",
            "meta": self.meta,
            "criteria": [crit.__dict__ for crit in self.criteria.values()],
        }
        (out_dir / "acceptance_report.json").write_text(
            json.dumps(doc, indent=2, default=str), encoding="utf-8")
        if self.meta.get("profile") == "neural-nde":
            horizon = (f"Neural NDE profile ({self.meta.get('cnde_mode', 'default')} mode); "
                       f"simulated horizon per plan: "
                       f"{self.meta.get('nnde_sim_end_s')} s (A1-2, A1-3), "
                       f"{self.meta.get('cnde_sim_end_s')} s (A1-4)")
        else:
            horizon = f"simulated horizon per candidate: {self.meta.get('sim_end_s')} s"
        lines = [
            "# TeraSim Metropolis acceptance run",
            "",
            f"- Result: **{doc['result']}** ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))})",
            f"- Started {doc['started_at']}, duration {doc['duration_s']} s",
            f"- Mode: {self.meta.get('mode')}; {horizon}; data: synthetic demo corridor",
            "",
            "| ID | Component | Criterion | Status | Evidence |",
            "| --- | --- | --- | --- | --- |",
        ]
        for crit in self.criteria.values():
            evidence = "; ".join(f"{k}={_short(v)}" for k, v in crit.evidence.items())
            if crit.detail:
                evidence = f"{crit.detail}. {evidence}" if evidence else crit.detail
            lines.append(f"| {crit.cid} | {crit.component} | {crit.title} | {crit.status} | "
                         f"{evidence.replace('|', '/')} |")
        lines.append("")
        (out_dir / "acceptance_report.md").write_text("\n".join(lines), encoding="utf-8")


def _short(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    text = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= 160 else text[:157] + "..."


CRITERIA = [
    ("ENV-1", "Runtime", "Python, release packages, SUMO 1.23.1 and Redis are available"),
    ("A3-1", "A3 Simulation core", "Service, MCP server and TMC collector start and report healthy; MCP exposes the 9 tools"),
    ("A4-1", "A4 Map adapter", "Map upload skill registers a SUMO map from the asset volume (3 signals found)"),
    ("A4-2", "A4 Map adapter", "OpenDRIVE map is converted on upload: geo-referenced, 3 signalized junctions, fully connected"),
    ("A4-3", "A4 Map adapter", "OpenDRIVE without geo-reference is refused with guidance and accepted with a proj override"),
    ("A4-4", "A4 Map adapter", "The converted HD map feeds the simulation core: an optimization runs on it and returns NEMA + KPIs"),
    ("C1-1", "C1 Baseline", "Timing-plan upload skill stores the corridor's current NEMA plan for every intersection"),
    ("TMC-1", "Traffic state", "TMC collector pulls VSS behavior objects for registered intersections and injects snapshots"),
    ("OD-1", "On-Demand mode", "One optimization tool call returns a NEMA plan, KPIs, a ready-to-show summary and download links"),
    ("B1-1", "B1 Control delay", "Control delay (s/veh) reported for the current and the optimized plan"),
    ("B2-1", "B2 Stops", "Stops per vehicle reported for the current and the optimized plan"),
    ("B-3", "B KPI weights", "Weighted cost = sum(w * metric / baseline metric); weights echoed; city-defined weights honored"),
    ("B-4", "B KPI menu", "Simulated signal metrics (per-approach measures) are produced for a completed run"),
    ("C1-2", "C1 Baseline", "The uploaded current plan is the baseline and the seed of the candidate search"),
    ("C2-1", "C2 Optimization", "Candidates simulated for the configured horizon (1,800 s), scored on the KPIs, best plan selected"),
    ("C3-1", "C3 Coordination", "The exported plan coordinates every corridor intersection (common cycle, per-signal offsets)"),
    ("C3-2", "C3 Coordination", "An eight-signal corridor with 32 cameras, defined by one camera inventory: the generated TMC spec is accepted and one optimization coordinates all eight signals"),
    ("C4-1", "C4 NEMA output", "NEMA timing plan (terasim.nema_timing.v1) and PDF timing report are served for the run"),
    ("SCH-1", "Scheduled mode", "The same optimization tool invoked on a fixed cadence: every invocation returns NEMA + KPIs on the latest data"),
    ("SCH-2", "Scheduled mode", "Optional built-in scheduler: a scheduler-triggered run completes with NEMA + KPIs"),
    ("A2-1", "A2 Adversarial environment", "Parameterized intersection adversities inject safety-critical events on the corridor"),
    ("A2-2", "A2 Adversarial environment", "Adversities are selected by configuration: only the configured types fire, none configured none fire"),
    ("A2-3", "A2 Adversarial environment", "Every adversity parameter file in configs/adversities resolves to an adversity class"),
    ("A1-1", "A1 Behavior models", "NADE naturalistic behavior drives the background traffic; the Neural NDE behavior slot is registered"),
    ("A1-2", "A1 Behavior models", "Neural NDE inference interface: deployment settings switch on the re-evaluation stage (behavior cnde); an optimization on a stored map runs it and returns NEMA + KPIs"),
]
NEURAL_NDE_CRITERIA = [
    ("A1-3", "A1 Behavior models", "With the Neural NDE runtime and checkpoint installed, the baseline and the top candidates are re-evaluated under Neural NDE on the GPU and the exported plan comes from that stage"),
    ("A1-4", "A1 Behavior models", "Per request: an optimize tool call with traffic_model=\"cnde\" simulates every candidate plan under Neural NDE on the GPU, within the deployment's CNDE limits, and is answered and recorded as a CNDE run; the same call without it keeps SUMO traffic"),
]
# A1-4: the corridor entry's own budget, which the deployment's CNDE limits cut down for a
# traffic_model="cnde" request only.
CNDE_ENTRY_TRIALS = 6
CNDE_ENTRY_WORKERS = 4
CNDE_FACTS_LINE = ("Traffic model: CNDE (Neural NDE, learned driver behavior) simulated every "
                   "candidate plan.")


# ---------------------------------------------------------------------------
# Synthetic inputs
# ---------------------------------------------------------------------------

def sensor_id(intersection_id: str, direction: str) -> str:
    return f"{intersection_id}-cam-{direction[0].lower()}b"


def make_tmc_spec() -> dict:
    """A TMC spec (Metadata.json contract) for the three synthetic intersections."""
    intersections = []
    for inter in INTERSECTIONS:
        iid = inter["intersection_id"]
        headings = {}
        for direction, (edge_in, edge_out) in APPROACH_EDGES[iid].items():
            headings[edge_in] = direction
            headings[edge_out] = direction
        intersections.append({
            "intersection_id": iid,
            "lat": inter["lat"],
            "lon": inter["lon"],
            "Headings": headings,
            "Movements": dict(MOVEMENTS),
            "Assignments": {d: sensor_id(iid, d) for d in ("East", "West", "North", "South")},
        })
    return {"corridor_id": CORRIDOR_ID, "observation_interval": OBSERVATION_INTERVAL,
            "intersections": intersections}


def make_nema_plan() -> dict:
    """The corridor's current timing plan (terasim.nema_plan.v1): 90 s, four phases."""
    def phase(number: int, direction: str, min_green: int) -> dict:
        return {"phase": number,
                "movements": [f"{direction}-through", f"{direction}-right", f"{direction}-left"],
                "min_green_s": min_green, "yellow_s": 3.0, "all_red_s": 1.0}

    intersections = []
    for inter in INTERSECTIONS:
        intersections.append({
            "intersection_id": inter["intersection_id"],
            "name": inter["name"],
            "lat": inter["lat"],
            "lon": inter["lon"],
            "phases": [phase(2, "WB", 10), phase(4, "NB", 8), phase(6, "EB", 10),
                       phase(8, "SB", 8)],
            "rings": {"ring1": [2, 4], "ring2": [6, 8], "barrier_phases": [4, 8],
                      "barrier2_phases": [2, 6], "coordinated_phases": [2, 6]},
            "patterns": [{"pattern_id": "p90", "description": "Coordinated 90 s plan",
                          "cycle_s": 90, "offset_s": inter["offset_s"],
                          "splits": {"2": 52, "4": 38, "6": 52, "8": 38}}],
            "schedule": [{"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                          "start": "00:00", "pattern_id": "p90"}],
        })
    return {"schema": "terasim.nema_plan.v1", "corridor_id": CORRIDOR_ID,
            "intersections": intersections,
            "provenance": {"source_files": [], "generated_by": "acceptance run (synthetic)",
                           "handoff_ready": True, "gaps": []}}


def load_demand() -> list[dict]:
    """10-minute turning-movement counts per snapshot from the demo corridor."""
    snapshots = []
    for path in sorted((DEMO_DIR / "snapshots").glob("snap_*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        snapshots.append({item["intersection_id"]: item["counts"] for item in doc["intersections"]})
    if not snapshots:
        raise SystemExit(f"no demand snapshots found under {DEMO_DIR / 'snapshots'}")
    return snapshots


def snapshot_for_hd(timestamp: datetime, corridor_id: str = HD_CORRIDOR_ID) -> dict:
    """A 10-minute TMC snapshot for a corridor on a stored map (pushed through the MCP tool)."""
    demand = load_demand()[0]
    return {
        "corridor_id": corridor_id,
        "observation_interval": "10m",
        "timestamp_start": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "intersections": [
            {"intersection_id": inter["intersection_id"], "name": inter["name"],
             "lat": inter["lat"], "lon": inter["lon"], "counts": demand[inter["intersection_id"]]}
            for inter in INTERSECTIONS
        ],
    }


class VssBehaviorStub:
    """Local stand-in for the VSS behavior API (GET /behavior), synthetic objects only.

    Each 1-minute window serves one tenth of the demo corridor's 10-minute counts (an exact
    split over ten consecutive windows), cycling through the six demo snapshots.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.demand = load_demand()
        self.requests = 0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                url = urlparse(self.path)
                if url.path != "/behavior":
                    self.send_response(404)
                    self.end_headers()
                    return
                params = parse_qs(url.query)
                body = json.dumps({"behaviors": stub.behaviors(
                    params.get("sensorId", [""])[0], params.get("queryString", [""])[0])}).encode()
                stub.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # silence per-request logging
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "VssBehaviorStub":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def behaviors(self, sensor: str, query: str) -> list[dict]:
        match = re.match(r"^(i\d)-cam-([ewns])b$", sensor)
        if not match:
            return []
        iid, letter = match.groups()
        direction = {"e": "East", "w": "West", "n": "North", "s": "South"}[letter]
        end_text = query.split(" TO ")[-1].rstrip("]") if " TO " in query else ""
        try:
            end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
        except ValueError:
            end = datetime.now(timezone.utc)
        minute = int(end.timestamp() // 60)
        counts = self.demand[(minute // 10) % len(self.demand)][iid][direction]
        k = minute % 10
        edge_in, _ = APPROACH_EDGES[iid][direction]
        objects = []
        for movement, total in counts.items():
            number = (total * (k + 1)) // 10 - (total * k) // 10
            exit_direction = TURN_EXIT[direction][movement]
            edge_out = APPROACH_EDGES[iid][exit_direction][1]
            for n in range(number):
                objects.append({
                    "sensor": {"id": sensor},
                    "object": {"id": f"{sensor}-{minute}-{movement[0]}{n}", "type": "Vehicle"},
                    "edges": [edge_in, edge_out],
                    "end": end_text or end.isoformat(),
                })
        return objects


def write_opendrive_inputs(work: Path, map_dir: Path) -> tuple[Path, Path, str]:
    """Export the demo network to OpenDRIVE (geo-referenced) plus a copy without geo-reference.

    Returns (xodr, xodr_without_georeference, proj string describing the latter's x/y).
    """
    hd_dir = work / "input" / "hd"
    hd_dir.mkdir(parents=True, exist_ok=True)
    xodr = hd_dir / "demo_corridor.xodr"
    subprocess.run(
        ["netconvert", "--sumo-net-file", str(map_dir / "demo.net.xml"),
         "--opendrive-output", str(xodr)],
        check=True, capture_output=True, text=True,
    )
    text = xodr.read_text(encoding="utf-8")
    offset = re.search(r'<offset x="([-0-9.]+)" y="([-0-9.]+)"', text)
    if offset is None:
        raise RuntimeError("netconvert wrote no <offset> into the OpenDRIVE header")
    easting, northing = float(offset.group(1)), float(offset.group(2))
    local = re.sub(r"<geoReference>.*?</geoReference>", "", text, flags=re.S)
    local = re.sub(r"\s*<offset [^>]*/>", "", local)
    xodr_local = hd_dir / "demo_corridor_local.xodr"
    xodr_local.write_text(local, encoding="utf-8")
    # "UTM minus an origin (E0, N0)": the documented proj form for local metric coordinates.
    proj = (f"+proj=tmerc +lat_0=0 +lon_0=-123 +k=0.9996 +x_0={500000.0 - easting:.2f} "
            f"+y_0={-northing:.2f} +datum=WGS84 +units=m +no_defs")
    return xodr, xodr_local, proj


def write_osm_input(work: Path) -> Path:
    """The demo corridor as an OpenStreetMap file (synthetic): stored like an OSM extract.

    Nodes and road names come from the demo network's plain-XML inputs; the arterial and the
    cross streets are two-way roads and the three junctions carry traffic signals.
    """
    from xml.etree import ElementTree as ET

    nodes = {n.get("id"): (float(n.get("y")), float(n.get("x")))
             for n in ET.parse(DEMO_DIR / "demo.nod.xml").getroot().iter("node")}
    osm_ids = {name: 1000 + i for i, name in enumerate(sorted(nodes))}
    root = ET.Element("osm", version="0.6", generator="terasim-acceptance (synthetic)")
    for name in sorted(nodes):
        lat, lon = nodes[name]
        node = ET.SubElement(root, "node", id=str(osm_ids[name]), lat=f"{lat:.7f}",
                             lon=f"{lon:.7f}", version="1")
        if name in DEMO_SIGNALS:
            ET.SubElement(node, "tag", k="highway", v="traffic_signals")
    # Like surveyed OpenStreetMap data, every road segment carries shape points (here every
    # 10 m along the straight segment), so the converted lanes have a detailed geometry.
    next_id = [10000]

    def shape_points(a: str, b: str) -> list[int]:
        (lat_a, lon_a), (lat_b, lon_b) = nodes[a], nodes[b]
        length_m = math.hypot((lat_b - lat_a) * 110_950.0, (lon_b - lon_a) * 88_500.0)
        count = max(int(length_m // 10.0), 1)
        ids = []
        for k in range(1, count):
            f = k / count
            ids.append(next_id[0])
            ET.SubElement(root, "node", id=str(next_id[0]), version="1",
                          lat=f"{lat_a + f * (lat_b - lat_a):.7f}",
                          lon=f"{lon_a + f * (lon_b - lon_a):.7f}")
            next_id[0] += 1
        return ids

    ways: list[ET.Element] = []  # written after every node: OSM files list nodes first

    side_streets: list[tuple[int, tuple[float, float]]] = []

    def way(way_id: int, chain: list[str], tags: dict[str, str]) -> None:
        refs = [osm_ids[chain[0]]]
        for a, b in zip(chain, chain[1:]):
            points = shape_points(a, b)
            # Minor side streets every 100 m, as in a city block grid: the roads are split
            # into block-length segments.
            for k in (len(points) // 3, 2 * len(points) // 3):
                if 0 < k < len(points):
                    side_streets.append((points[k - 1], (nodes[a], nodes[b])))
            refs += points + [osm_ids[b]]
        element = ET.Element("way", id=str(way_id), version="1")
        for ref in refs:
            ET.SubElement(element, "nd", ref=str(ref))
        for key, value in tags.items():
            ET.SubElement(element, "tag", k=key, v=value)
        ways.append(element)

    way(5000, ["W2", "W1", "J1", "J2", "J3", "E1", "E2"],
        {"highway": "secondary", "name": "Arterial Ave", "lanes": "4", "maxspeed": "50"})
    for k, signal in enumerate(sorted(DEMO_SIGNALS), start=1):
        way(5000 + k, [f"{signal}_S2", f"{signal}_S1", signal, f"{signal}_N1", f"{signal}_N2"],
            {"highway": "tertiary", "name": f"Cross St {k}", "lanes": "2", "maxspeed": "40"})
    positions = {int(n.get("id")): (float(n.get("lat")), float(n.get("lon")))
                 for n in root.iter("node")}
    for number, (ref, ((lat_a, lon_a), (lat_b, lon_b))) in enumerate(side_streets, start=1):
        lat, lon = positions[ref]
        # 60 m long, perpendicular to the road it joins, alternating sides
        d_north, d_east = (lon_b - lon_a) * 88_500.0, -(lat_b - lat_a) * 110_950.0
        norm = math.hypot(d_north, d_east) or 1.0
        side = 1 if number % 2 else -1
        end = (lat + side * 60.0 * d_north / norm / 110_950.0,
               lon + side * 60.0 * d_east / norm / 88_500.0)
        ET.SubElement(root, "node", id=str(next_id[0]), version="1",
                      lat=f"{end[0]:.7f}", lon=f"{end[1]:.7f}")
        element = ET.Element("way", id=str(6000 + number), version="1")
        ET.SubElement(element, "nd", ref=str(ref))
        ET.SubElement(element, "nd", ref=str(next_id[0]))
        next_id[0] += 1
        for key, value in {"highway": "residential", "name": f"Side St {number}"}.items():
            ET.SubElement(element, "tag", k=key, v=value)
        ways.append(element)
    root.extend(ways)
    path = work / "input" / "osm" / "demo_corridor.osm"
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def prepare_inputs(work: Path, args: argparse.Namespace) -> dict:
    """Write every synthetic input the local stack reads; returns their paths."""
    inputs = work / "input"
    map_dir = inputs / "map"
    map_dir.mkdir(parents=True, exist_ok=True)
    for name in MAP_FILES:
        shutil.copy2(DEMO_DIR / name, map_dir / name)
    (inputs / "tmc").mkdir(parents=True, exist_ok=True)
    spec_path = inputs / "tmc" / "Metadata.json"
    spec_path.write_text(json.dumps(make_tmc_spec(), indent=2), encoding="utf-8")
    plan_path = inputs / "current_plan.nema_plan.json"
    plan_path.write_text(json.dumps(make_nema_plan(), indent=2), encoding="utf-8")
    scheduled = inputs / "scheduled_config.yaml"
    scheduled.write_text(
        "corridors:\n"
        f"  {CORRIDOR_ID}:\n"
        f"    base_sumocfg: {map_dir / 'demo.sumocfg'}\n"
        "    corridor:\n"
        "      tls_ids: [J1, J2, J3]\n"
        f"    sim_end: {args.sim_end}\n"
        f"    max_trials: {args.max_trials}\n"
        f"    patience: {args.max_trials}\n"
        f"    n_workers: {args.n_workers}\n",
        encoding="utf-8",
    )
    xodr, xodr_local, proj = write_opendrive_inputs(work, map_dir)
    return {"map_dir": map_dir, "sumocfg": map_dir / "demo.sumocfg", "spec": spec_path,
            "plan": plan_path, "scheduled": scheduled, "xodr": xodr,
            "xodr_local": xodr_local, "proj": proj, "osm": write_osm_input(work)}


# ---------------------------------------------------------------------------
# Local stack
# ---------------------------------------------------------------------------

SERVICE_LAUNCHER = """\
# Launches terasim-service exactly like its console script, on a chosen host/port.
import sys

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from terasim_service.api import create_app
from terasim_service.city.common import city_log_config

if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=int(sys.argv[1]),
                log_config=city_log_config(LOGGING_CONFIG))
"""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalStack:
    """redis-server + terasim-service + MCP server + TMC collector + VSS behavior stub."""

    def __init__(self, work: Path, inputs: dict, args: argparse.Namespace) -> None:
        self.work = work
        self.inputs = inputs
        self.args = args
        self.ports = {name: free_port() for name in ("redis", "service", "mcp", "tmc", "vss")}
        self.procs: dict[str, subprocess.Popen] = {}
        self.logs = work / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.output_root = work / "output"
        self.data_root = self.output_root / "store"
        self.stub: VssBehaviorStub | None = None
        self.service_url = f"http://127.0.0.1:{self.ports['service']}"
        self.mcp_url = f"http://127.0.0.1:{self.ports['mcp']}/mcp"
        self.tmc_url = f"http://127.0.0.1:{self.ports['tmc']}"

    def _spawn(self, name: str, cmd: list[str], env: dict[str, str]) -> None:
        log = open(self.logs / f"{name}.log", "ab")
        full_env = dict(os.environ)
        full_env.update(env)
        self.procs[name] = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, env=full_env, cwd=str(self.work),
            start_new_session=True)

    def service_env(self, *, scheduler: bool, refine: bool = False, cnde_traffic: bool = False,
                    scheduled: Path | None = None) -> dict[str, str]:
        env = {
            "TERASIM_ENABLE_CITY_API": "true",
            "TERASIM_CITY_INPUT_ROOT": str(self.work / "input"),
            "TERASIM_CITY_OUTPUT_ROOT": str(self.output_root),
            "TERASIM_CITY_DATA_ROOT": str(self.data_root),
            "TERASIM_CITY_SCHEDULED_CONFIG": str(scheduled or self.inputs["scheduled"]),
            "TERASIM_CITY_WINDOW_S": "3600",
            "TERASIM_CITY_MAX_CONCURRENCY": str(max(8, 2 * self.args.n_workers)),
            "TERASIM_CITY_OSM_DOWNLOAD": "0",  # fully offline: maps only from the store
            "TERASIM_CITY_SIM_METRICS": "1",
            "TERASIM_REDIS_HOST": "127.0.0.1",
            "TERASIM_REDIS_PORT": str(self.ports["redis"]),
        }
        if scheduler:
            env["TERASIM_CITY_SCHEDULER_ENABLED"] = "true"
            env["TERASIM_CITY_CADENCE_S"] = str(self.args.builtin_cadence_s)
        if refine:
            # The Neural NDE re-evaluation stage is a deployment setting (callers cannot
            # request it): the baseline plus the top-K coarse candidates are re-evaluated
            # under the "cnde" behavior, one after another in one process (one GPU context).
            env["TERASIM_CITY_REFINE_TOP_K"] = str(self.args.refine_top_k)
            env["TERASIM_CITY_REFINE_BEHAVIOR"] = "cnde"
            env["TERASIM_CITY_REFINE_N_WORKERS"] = "1"
            env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
        if cnde_traffic:
            # Per-request Neural NDE traffic (traffic_model="cnde") stays a caller's choice; the
            # deployment only bounds such a run: trials on the GPU at once, Bayesian trials.
            env["TERASIM_CITY_CNDE_MAX_WORKERS"] = str(self.args.cnde_max_workers)
            env["TERASIM_CITY_CNDE_MAX_TRIALS"] = str(self.args.cnde_max_trials)
            env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
        if (refine or cnde_traffic) and getattr(self.args, "cnde_fast", False):
            # Fast mode: the model predicts only the vehicles it may control, and each trial
            # process keeps to two math-library threads.
            env["TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY"] = "1"
            for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
                env[name] = "2"
        return env

    def start_service(self, *, scheduler: bool = False, refine: bool = False,
                      cnde_traffic: bool = False, scheduled: Path | None = None) -> None:
        launcher = self.work / "launch_service.py"
        launcher.write_text(SERVICE_LAUNCHER, encoding="utf-8")
        name = ("service-scheduler" if scheduler else "service-refine" if refine
                else "service-cnde" if cnde_traffic else "service")
        self._spawn(name, [sys.executable, str(launcher), str(self.ports["service"])],
                    self.service_env(scheduler=scheduler, refine=refine,
                                     cnde_traffic=cnde_traffic, scheduled=scheduled))
        wait_http(f"{self.service_url}/openapi.json", timeout_s=120)

    def stop_process(self, name: str) -> None:
        """Stop a component and everything it started (its whole process group).

        The service forks optimization workers into its process group; they may outlive a
        graceful shutdown of the server process, so the group is killed after the grace
        period whatever the server did.
        """
        proc = self.procs.pop(name, None)
        if proc is None:
            return
        pgid = proc.pid  # start_new_session=True: the component leads its own group
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)  # any member of the group left?
            except ProcessLookupError:
                return
            time.sleep(0.5)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def start(self, *, refine: bool = False) -> "LocalStack":
        (self.work / "redis").mkdir(exist_ok=True)
        (self.work / "tmc").mkdir(exist_ok=True)
        (self.work / "artifacts").mkdir(exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._spawn("redis", ["redis-server", "--port", str(self.ports["redis"]),
                              "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
                              "--dir", str(self.work / "redis")], {})
        wait_tcp(self.ports["redis"], timeout_s=30)
        self.stub = VssBehaviorStub(self.ports["vss"]).start()
        # The operator registers the corridor's intersections before the collector starts
        # (the registry file is the collector's persisted registry), so the first window it
        # injects already covers the whole corridor. The MCP registration tool is exercised
        # again in TMC-1.
        registered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.work / "tmc" / "tmc_registry.json").write_text(json.dumps({"intersections": [
            {**public(inter), "corridor_id": CORRIDOR_ID, "registered_at": registered_at}
            for inter in INTERSECTIONS]}, indent=2), encoding="utf-8")
        self.start_service(scheduler=False, refine=refine)
        self._spawn("tmc", [sys.executable, "-m", "terasim_tmc_service.app"], {
            "TMC_SPEC_FILE": str(self.inputs["spec"]),
            "VSS_BEHAVIOR_URL": f"http://127.0.0.1:{self.ports['vss']}",
            "TERASIM_CITY_URL": self.service_url,
            "TMC_REGISTRY_FILE": str(self.work / "tmc" / "tmc_registry.json"),
            "TMC_SETTLE_LAG_S": "0",
            "TMC_AUTOSTART": "1",
            "TMC_HOST": "127.0.0.1",
            "TMC_PORT": str(self.ports["tmc"]),
        })
        wait_http(f"{self.tmc_url}/v1/tmc/health", timeout_s=60)
        self.initial_tick = self._wait_injected_tick(timeout_s=60)
        self._spawn("mcp", [sys.executable, "-m", "terasim_agent_skill.server"], {
            "TERASIM_MCP_TRANSPORT": "streamable-http",
            "TERASIM_MCP_HOST": "127.0.0.1",
            "TERASIM_MCP_PORT": str(self.ports["mcp"]),
            "TERASIM_CITY_URL": self.service_url,
            "TERASIM_TMC_URL": self.tmc_url,
            "TERASIM_ARTIFACT_ROOT": str(self.work / "artifacts"),
            "TERASIM_MCP_PUBLIC_URL": f"http://127.0.0.1:{self.ports['mcp']}",
            "TERASIM_CITY_DATA_ROOT": str(self.data_root),
        })
        wait_tcp(self.ports["mcp"], timeout_s=60)
        return self

    def _wait_injected_tick(self, *, timeout_s: float) -> dict:
        """The collector's first periodic tick (it starts with the registered corridor)."""
        import httpx

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            tick = (httpx.get(f"{self.tmc_url}/v1/tmc/health", timeout=10).json()
                    .get("last_tick") or {})
            if tick.get("status") == "injected":
                return tick
            time.sleep(1)
        return {}

    def stop(self) -> None:
        for name in list(self.procs):
            if name != "redis":
                self.stop_process(name)
        self.stop_process("redis")
        if self.stub is not None:
            self.stub.stop()


def wait_tcp(port: int, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"nothing listens on 127.0.0.1:{port} after {timeout_s:.0f} s")


def wait_http(url: str, *, timeout_s: float) -> None:
    import httpx

    deadline = time.monotonic() + timeout_s
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            reply = httpx.get(url, timeout=5.0)
            if reply.status_code < 500:
                return
            last = f"HTTP {reply.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(0.5)
    raise RuntimeError(f"{url} not ready after {timeout_s:.0f} s ({last})")


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------

class McpCaller:
    """Runs one MCP tool call per session, the way an agent framework does."""

    def __init__(self, url: str, *, timeout_s: float) -> None:
        self.url = url
        self.timeout_s = timeout_s

    def list_tools(self) -> list[str]:
        async def go():
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(self.url, timeout=60) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return [tool.name for tool in (await session.list_tools()).tools]
        return asyncio.run(go())

    def call(self, tool: str, arguments: dict) -> tuple[bool, list[str], Any]:
        """Returns (is_error, text blocks, structured content)."""
        async def go():
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(
                self.url, timeout=60, sse_read_timeout=self.timeout_s
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool(
                        tool, arguments, read_timeout_seconds=timedelta(seconds=self.timeout_s))
        result = asyncio.run(go())
        texts = [getattr(item, "text", "") for item in result.content
                 if getattr(item, "text", None)]
        return bool(result.isError), texts, getattr(result, "structuredContent", None)

    def call_json(self, tool: str, arguments: dict) -> tuple[dict, list[str]]:
        is_error, texts, structured = self.call(tool, arguments)
        if is_error:
            raise ToolError(tool, " ".join(texts))
        if isinstance(structured, dict) and structured:
            payload = structured.get("result", structured)
            if isinstance(payload, dict):
                return payload, texts
        for text in reversed(texts):
            try:
                document = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(document, dict):
                return document, texts
        raise ToolError(tool, f"no JSON content in the answer: {texts!r}"[:400])


class ToolError(RuntimeError):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def expected_cost(metrics: dict, baseline: dict, weights: dict) -> float:
    keys = {"control_delay": "control_delay_s_per_veh", "stops": "stops_per_veh"}
    return sum(w * float(metrics[keys[name]]) / max(float(baseline[keys[name]]), 1e-6)
               for name, w in weights.items())


def kpi_findings(kpis: dict, weights: dict) -> tuple[list[str], dict]:
    """Problems with an optimize answer's KPI block (empty list = all good) + evidence."""
    problems: list[str] = []
    base = (kpis.get("baseline") or {})
    opt = (kpis.get("optimized") or {})
    bm, om = base.get("metrics") or {}, opt.get("metrics") or {}
    for label, metrics in (("baseline", bm), ("optimized", om)):
        for key in ("control_delay_s_per_veh", "stops_per_veh"):
            if not finite(metrics.get(key)) or metrics.get(key) < 0:
                problems.append(f"{label}.{key} missing or not a finite non-negative number")
    if kpis.get("cost_weights") != weights:
        problems.append(f"cost_weights {kpis.get('cost_weights')} != requested {weights}")
    evidence: dict[str, Any] = {}
    if not problems:
        evidence = {
            "delay_baseline_s_per_veh": bm["control_delay_s_per_veh"],
            "delay_optimized_s_per_veh": om["control_delay_s_per_veh"],
            "stops_baseline": bm["stops_per_veh"],
            "stops_optimized": om["stops_per_veh"],
            "cost_baseline": base.get("cost"),
            "cost_optimized": opt.get("cost"),
            "vehicles": bm.get("vehicles_total"),
        }
        recomputed_base = expected_cost(bm, bm, weights)
        recomputed_opt = expected_cost(om, bm, weights)
        if not (finite(base.get("cost")) and abs(base["cost"] - recomputed_base) < 1e-6):
            problems.append(f"baseline cost {base.get('cost')} != {recomputed_base:.6f}")
        if not (finite(opt.get("cost")) and abs(opt["cost"] - recomputed_opt) < 1e-6):
            problems.append(f"optimized cost {opt.get('cost')} != recomputed {recomputed_opt:.6f}")
        if finite(opt.get("cost")) and finite(base.get("cost")) and opt["cost"] > base["cost"] + 1e-9:
            problems.append("optimized cost is worse than the baseline cost")
        if not (finite(bm.get("vehicles_total")) and bm["vehicles_total"] > 0):
            problems.append("no vehicles were simulated")
    return problems, evidence


def nema_findings(plan: dict, expected_tls: set[str]) -> tuple[list[str], dict]:
    """Problems with a terasim.nema_timing.v1 document (empty list = all good) + evidence."""
    problems: list[str] = []
    if plan.get("schema") != NEMA_TIMING_SCHEMA:
        problems.append(f"schema {plan.get('schema')!r} != {NEMA_TIMING_SCHEMA}")
    entries = plan.get("intersections") or []
    signals = {str(e.get("tls_id")) for e in entries}
    if expected_tls and not expected_tls <= signals:
        problems.append(f"plan covers {sorted(signals)}; expected {sorted(expected_tls)}")
    if not entries:
        problems.append("plan has no intersections")
    coordination = plan.get("coordination") or {}
    cycle = coordination.get("cycle_length_s")
    cycles = sorted({float(e["cycle_s"]) for e in entries if finite(e.get("cycle_s"))})
    if not cycles and finite(cycle):
        cycles = [float(cycle)]
    offsets = {str(e.get("tls_id")): e.get("offset_s") for e in entries}
    rows = sum(len(e.get("phases") or []) for e in entries)
    if entries and not rows:
        problems.append("plan has no phase rows")
    return problems, {"signals": sorted(signals), "cycle_length_s": cycle, "cycles_s": cycles,
                      "offsets_s": offsets, "phase_rows": rows,
                      "handoff_ready": plan.get("handoff_ready")}


DEMO_SIGNALS = {inter["tls_id"] for inter in INTERSECTIONS}


class AcceptanceRun:
    def __init__(self, args: argparse.Namespace, report: Report, out: Path) -> None:
        self.args = args
        self.report = report
        self.out = out
        self.work = out / "work"
        self.stack: LocalStack | None = None
        self.inputs: dict = {}
        self.service = args.service
        self.tmc = args.tmc
        self.mcp: McpCaller | None = None
        self.runs: dict[str, str] = {}  # label -> run_id
        self.ondemand_answer: dict = {}
        # Local mode optimizes the whole corridor (three signals). Remote mode targets one
        # intersection: there the collector's registry starts empty and intersections are
        # registered one call at a time, and a corridor needs every intersection registered
        # before its first collection window (see README).
        self.targets = INTERSECTIONS if not args.remote else [INTERSECTIONS[1]]
        self.signals = {inter["tls_id"] for inter in self.targets}

    # -- plumbing ------------------------------------------------------------
    def http(self):
        import httpx
        return httpx.Client(base_url=self.service, timeout=60.0)

    def check(self, cid: str, fn: Callable[[], tuple[bool, dict, str]]) -> bool:
        started = time.monotonic()
        try:
            ok, evidence, detail = fn()
        except SkipCriterion as exc:
            self.report.record(cid, True, {}, str(exc), time.monotonic() - started, skipped=True)
            return True
        except Exception as exc:  # a failed criterion never stops the run
            (self.out / "errors").mkdir(exist_ok=True)
            (self.out / "errors" / f"{cid}.txt").write_text(traceback.format_exc(), encoding="utf-8")
            self.report.record(cid, False, {}, f"{type(exc).__name__}: {exc}"[:400],
                               time.monotonic() - started)
            return False
        self.report.record(cid, ok, evidence, detail, time.monotonic() - started)
        return ok

    def newest_run(self, corridor_id: str, *, origin: str | None = None,
                   exclude: set[str] = frozenset()) -> dict | None:
        with self.http() as client:
            runs = client.get("/v1/city/optimizations",
                              params={"corridor_id": corridor_id, "limit": 50}).json()
        for run in runs:
            if run["run_id"] in exclude:
                continue
            if origin is None or run.get("origin") == origin:
                return run
        return None

    def run_result(self, run_id: str) -> dict:
        with self.http() as client:
            reply = client.get(f"/v1/city/optimizations/{run_id}/result")
            reply.raise_for_status()
            return reply.json()

    def optimize_corridor(self, corridor_id: str, cost: dict | None = None,
                          targets: list[dict] | None = None) -> dict:
        targets = targets or self.targets
        if len(targets) > 1:
            tool = "optimize_corridor"
            args = {"corridor_id": corridor_id,
                    "from_intersection": public(targets[0]),
                    "to_intersection": public(targets[-1])}
        else:
            tool = "optimize_intersection"
            args = {"corridor_id": corridor_id, "intersection": public(targets[0])}
        if cost is not None:
            args["cost"] = {"weights": cost}
        payload, texts = self.mcp.call_json(tool, args)
        payload["_markdown"] = texts[0] if texts else ""
        return payload

    # -- sequence -------------------------------------------------------------
    def run(self) -> None:
        a = self.args
        self.report.meta.update({
            "profile": "neural-nde" if a.neural_nde else "standard",
            "nnde_sim_end_s": a.nnde_sim_end, "refine_top_k": a.refine_top_k,
            "cnde_sim_end_s": a.cnde_sim_end, "cnde_max_trials": a.cnde_max_trials,
            "cnde_max_workers": a.cnde_max_workers,
            "cnde_mode": "fast" if a.cnde_fast else "default",
            "mode": "remote" if a.remote else "local",
            "sim_end_s": a.sim_end, "max_trials": a.max_trials, "n_workers": a.n_workers,
            "scheduled_invocations": a.scheduled_invocations, "cadence_s": a.cadence_s,
            "python": platform.python_version(), "platform": platform.platform(),
        })
        if not self.check("ENV-1", self.check_env):
            return
        if a.neural_nde:
            self.run_neural_nde_profile()
            return
        try:
            if not a.remote:
                self.work.mkdir(parents=True, exist_ok=True)
                self.inputs = prepare_inputs(self.work, a)
                self.stack = LocalStack(self.work, self.inputs, a).start()
                self.service, self.tmc = self.stack.service_url, self.stack.tmc_url
                self.mcp = McpCaller(self.stack.mcp_url, timeout_s=a.tool_timeout_s)
                self.report.meta["ports"] = self.stack.ports
            else:
                self.mcp = McpCaller(a.mcp, timeout_s=a.tool_timeout_s)
            if not self.check("A3-1", self.check_health):
                return
            self.check("A4-1", self.check_map_upload)
            self.check("A4-2", self.check_opendrive)
            self.check("A4-3", self.check_opendrive_proj)
            self.check("C1-1", self.check_plan_upload)
            tmc_ok = self.check("TMC-1", self.check_tmc)
            if tmc_ok:
                if self.check("OD-1", self.check_ondemand):
                    self.check("B1-1", self.check_b1)
                    self.check("B2-1", self.check_b2)
                    self.check("C1-2", self.check_c1_seed)
                    self.check("C2-1", self.check_c2)
                    self.check("C3-1", self.check_c3)
                    self.check("C4-1", self.check_c4)
                    self.check("B-4", self.check_sim_metrics)
                self.check("SCH-1", self.check_scheduled_cadence)
            self.check("A4-4", self.check_hd_optimization)
            self.check("B-3", self.check_weights)
            self.check("C3-2", self.check_corridor_inventory)
            self.check("SCH-2", self.check_builtin_scheduler)
            self.check("A2-1", self.check_adversities_all)
            self.check("A2-2", self.check_adversities_tunable)
            self.check("A2-3", self.check_adversity_files)
            self.check("A1-1", self.check_behaviors)
            self.check("A1-2", self.check_neural_nde_interface)
        finally:
            if self.stack is not None:
                self.stack.stop()

    def run_neural_nde_profile(self) -> None:
        """--neural-nde: the Neural NDE re-evaluation stage with the runtime installed."""
        a = self.args
        try:
            self.work.mkdir(parents=True, exist_ok=True)
            self.inputs = prepare_inputs(self.work, a)
            self.stack = LocalStack(self.work, self.inputs, a).start(refine=True)
            self.service, self.tmc = self.stack.service_url, self.stack.tmc_url
            self.mcp = McpCaller(self.stack.mcp_url, timeout_s=a.tool_timeout_s)
            self.report.meta["ports"] = self.stack.ports
            if not self.check("A3-1", self.check_health):
                return
            self.check("A1-2", self.check_neural_nde_interface)
            self.check("A1-3", self.check_neural_nde_reevaluation)
            self.check("A1-4", self.check_cnde_traffic_model)
        finally:
            if self.stack is not None:
                self.stack.stop()

    # -- criteria -------------------------------------------------------------
    def check_env(self):
        import importlib.metadata as md

        packages = {}
        for name in ("terasim", "terasim-nde-nade", "terasim-city", "terasim-service",
                     "terasim-agent-skill", "terasim-tmc-service", "mcp", "eclipse-sumo"):
            try:
                packages[name] = md.version(name)
            except md.PackageNotFoundError:
                packages[name] = None
        missing = [k for k, v in packages.items() if v is None]
        sumo = shutil.which("sumo")
        sumo_version = ""
        if sumo:
            first = subprocess.run([sumo, "--version"], capture_output=True, text=True).stdout
            sumo_version = first.splitlines()[0] if first else ""
        redis = shutil.which("redis-server")
        problems = list(missing)
        if not shutil.which("netconvert"):
            problems.append("netconvert")
        if "1.23.1" not in sumo_version:
            problems.append(f"SUMO 1.23.1 (found {sumo_version or 'none'})")
        if not self.args.remote and not redis:
            problems.append("redis-server")
        evidence = {"python": platform.python_version(), "sumo": sumo_version,
                    "redis_server": bool(redis), **{k: v for k, v in packages.items() if v}}
        if self.args.neural_nde:
            runtime = neural_nde_runtime()
            evidence.update(runtime)
            self.report.meta["neural_nde_runtime"] = runtime
            if not runtime.get("runtime_installed"):
                problems.append("Neural NDE runtime module (csim) not importable")
            if not runtime.get("cuda_available"):
                problems.append("no CUDA device visible to torch")
            if not runtime.get("checkpoint_bytes"):
                problems.append("Neural NDE checkpoint not found next to the runtime")
        self.report.meta["packages"] = packages
        return not problems, evidence, ("missing: " + ", ".join(problems)) if problems else ""

    def check_health(self):
        with self.http() as client:
            openapi = client.get("/openapi.json")
            city_paths = [p for p in openapi.json().get("paths", {}) if p.startswith("/v1/city/")]
        import httpx

        tmc_health = httpx.get(f"{self.tmc}/v1/tmc/health", timeout=30).json()
        tools = set(self.mcp.list_tools())
        missing = sorted(EXPECTED_TOOLS - tools)
        ok = openapi.status_code == 200 and len(city_paths) >= 10 and not missing
        evidence = {"city_endpoints": len(city_paths), "mcp_tools": len(tools),
                    "tmc_corridor": tmc_health.get("corridor_id"),
                    "tmc_counted_labels": tmc_health.get("counted_object_types")}
        return ok, evidence, f"missing tools {missing}" if missing else ""

    def map_path(self) -> str:
        if self.args.remote:
            return self.args.map_path
        return str(self.inputs["sumocfg"])

    def check_map_upload(self):
        result, _ = self.mcp.call_json("upload_map", {"path": self.map_path()})
        summary = result.get("summary") or {}
        tls = summary.get("traffic_lights")
        ok = bool(result.get("map_id")) and tls == 3
        return ok, {"map_id": result.get("map_id"), "source": result.get("source"),
                    "traffic_lights": tls, "edges": summary.get("edges")}, ""

    def check_opendrive(self):
        if self.args.remote:
            raise SkipCriterion("remote mode: the generated OpenDRIVE file must be on the "
                                "service's asset volume; checked in local mode")
        result, _ = self.mcp.call_json("upload_map", {"path": str(self.inputs["xodr"])})
        s = result.get("summary") or {}
        ok = (result.get("source") == "opendrive" and s.get("geo_referenced") is True
              and s.get("traffic_lights") == 3 and s.get("largest_strong_component_share") == 1.0)
        self.hd_map_ids = getattr(self, "hd_map_ids", set()) | {result.get("map_id")}
        return ok, {"map_id": result.get("map_id"), "source": result.get("source"),
                    "edges": s.get("edges"), "lanes": s.get("lanes"),
                    "traffic_lights": s.get("traffic_lights"), "tls_source": s.get("tls_source"),
                    "geo_referenced": s.get("geo_referenced"),
                    "strong_connectivity": s.get("largest_strong_component_share"),
                    "conversion_s": s.get("conversion_seconds")}, ""

    def check_opendrive_proj(self):
        if self.args.remote:
            raise SkipCriterion("remote mode: needs a file on the asset volume")
        is_error, texts, _ = self.mcp.call("upload_map", {"path": str(self.inputs["xodr_local"])})
        message = " ".join(texts)
        refused = is_error and "proj" in message.lower()
        hit = re.search(r"(no usable|geo-?reference|georeference|proj)", message, re.I)
        excerpt = message[hit.start():hit.start() + 160] if hit else message[-160:]
        result, _ = self.mcp.call_json("upload_map", {"path": str(self.inputs["xodr_local"]),
                                                      "proj": self.inputs["proj"]})
        s = result.get("summary") or {}
        accepted = result.get("source") == "opendrive" and s.get("geo_referenced") is True
        self.hd_map_ids = getattr(self, "hd_map_ids", set()) | {result.get("map_id")}
        return refused and accepted, {
            "refused_without_proj": refused, "refusal_excerpt": excerpt,
            "accepted_with_proj": accepted, "traffic_lights": s.get("traffic_lights")}, ""

    def check_plan_upload(self):
        plan = make_nema_plan()
        result, _ = self.mcp.call_json("upload_signal_plan",
                                       {"plan": plan, "corridor_id": CORRIDOR_ID})
        stored = {tuple(item) for item in result.get("stored", [])}
        with self.http() as client:
            listed = {tuple(item) for item in client.get("/v1/city/plans").json().get("plans", [])}
        expected = {(CORRIDOR_ID, i["intersection_id"]) for i in INTERSECTIONS}
        ok = expected <= stored and expected <= listed
        return ok, {"stored": sorted("/".join(k) for k in stored),
                    "listed": len(listed), "cycle_s": 90}, ""

    def snapshot_count(self, corridor_id: str = CORRIDOR_ID) -> int:
        with self.http() as client:
            return int(client.get(f"/v1/city/snapshots/{corridor_id}").json().get("count", 0))

    def check_tmc(self):
        import httpx

        for inter in self.targets:
            self.mcp.call_json("register_intersection", {
                "intersection_id": inter["intersection_id"], "name": inter["name"],
                "lat": inter["lat"], "lon": inter["lon"], "corridor_id": CORRIDOR_ID})
        listing, _ = self.mcp.call_json("list_intersections", {})
        registered = [r.get("intersection_id") for r in listing.get("registered", [])]
        after_registration = (httpx.get(f"{self.tmc}/v1/tmc/health", timeout=30).json()
                              .get("last_tick") or {})
        tick = httpx.post(f"{self.tmc}/v1/tmc/run-once", timeout=120).json()
        deadline = time.monotonic() + 150
        while self.snapshot_count() < 1 and time.monotonic() < deadline:
            time.sleep(2)
        count = self.snapshot_count()
        health = httpx.get(f"{self.tmc}/v1/tmc/health", timeout=30).json()
        # The window's counts come from whichever tick injected it: the manual run-once (tick)
        # or an earlier tick of the collector, which it reports as last_injected.
        initial = getattr(self.stack, "initial_tick", {}) if self.stack is not None else {}
        injected = next((t for t in (tick, after_registration, health.get("last_tick") or {},
                                     health.get("last_injected") or {}, initial)
                         if t.get("status") == "injected"), {})
        behaviors = injected.get("behaviors") or tick.get("behaviors") or {}
        ok = (set(registered) >= {i["intersection_id"] for i in self.targets} and count >= 1
              and tick.get("status") in ("injected", "skipped"))
        if tick.get("status") == "injected" and sum(behaviors.values()) == 0:
            ok = False
        self.snapshots_after_tmc = count
        if count >= 1 and not behaviors:
            ok = False
        return ok, {"registered": registered, "manual_tick": tick.get("status"),
                    "injected_window": [injected.get("timestamp_start"), injected.get("timestamp_end")],
                    "cameras": len(behaviors), "behavior_objects": sum(behaviors.values()),
                    "behaviors_per_camera": behaviors, "buffered_snapshots": count}, ""

    def check_ondemand(self):
        started = time.monotonic()
        answer = self.optimize_corridor(CORRIDOR_ID)
        seconds = time.monotonic() - started
        self.ondemand_answer = answer
        run = self.newest_run(CORRIDOR_ID, origin="direct")
        if run is not None:
            self.runs["ondemand"] = run["run_id"]
        markdown = answer.get("_markdown", "")
        downloads = answer.get("downloads") or {}
        problems = []
        if "nema_plan" not in answer or "kpis" not in answer:
            problems.append("answer lacks nema_plan / kpis")
        if "KPIs" not in markdown and "control delay" not in markdown:
            problems.append("no ready-to-show KPI table in the markdown block")
        fetched = {}
        import httpx

        for name, url in downloads.items():
            reply = httpx.get(url, timeout=60)
            fetched[name] = reply.status_code
            if reply.status_code != 200:
                problems.append(f"download {name} -> HTTP {reply.status_code}")
            elif name.endswith(".pdf") and not reply.content.startswith(b"%PDF"):
                problems.append(f"{name} is not a PDF")
        if "timing_report.pdf" not in downloads:
            problems.append("no timing_report.pdf download link")
        return not problems, {"tool_seconds": round(seconds, 1), "run_id": run and run["run_id"],
                              "downloads": fetched,
                              "markdown_first_line": markdown.splitlines()[0] if markdown else ""}, \
            "; ".join(problems)

    def check_b1(self):
        problems, evidence = kpi_findings(self.ondemand_answer["kpis"], DEFAULT_WEIGHTS)
        keys = ("delay_baseline_s_per_veh", "delay_optimized_s_per_veh", "vehicles")
        own = [p for p in problems if "control_delay" in p or "vehicles" in p]
        return not own, {k: evidence.get(k) for k in keys}, "; ".join(own)

    def check_b2(self):
        problems, evidence = kpi_findings(self.ondemand_answer["kpis"], DEFAULT_WEIGHTS)
        own = [p for p in problems if "stops" in p]
        return not own, {k: evidence.get(k) for k in ("stops_baseline", "stops_optimized")}, \
            "; ".join(own)

    def check_c1_seed(self):
        result = self.run_result(self.runs["ondemand"])
        warm = ((result.get("search") or {}).get("warm_starts") or {}).get("used") or []
        baseline_id = (result.get("baseline") or {}).get("plan_id")
        with self.http() as client:
            reply = client.get(f"/v1/city/optimizations/{self.runs['ondemand']}/baseline")
        baseline_doc = reply.json() if reply.status_code == 200 else {}
        baseline_plan = baseline_doc.get("nema_plan") or {}
        n = len(baseline_plan.get("intersections") or [])
        seed = result.get("seed") or {}
        ok = (baseline_id == "stored_nema" and "stored_nema" in warm and n == len(self.targets)
              and baseline_doc.get("source") == "stored_nema")
        return ok, {"baseline_plan": baseline_id, "baseline_source": baseline_doc.get("source"),
                    "seed_source": seed.get("source"),
                    "stored_plans_used": seed.get("stored_plans_used"), "warm_starts": warm,
                    "baseline_endpoint_intersections": n}, ""

    def check_c2(self):
        result = self.run_result(self.runs["ondemand"])
        config = result.get("config") or {}
        n_trials = result.get("n_trials")
        best, base = result.get("best") or {}, result.get("baseline") or {}
        sim_end = config.get("sim_end")
        ok = (sim_end is not None and float(sim_end) == float(self.args.sim_end)
              and isinstance(n_trials, int) and n_trials >= 2
              and finite(best.get("score")) and finite(base.get("score"))
              and best["score"] <= base["score"] + 1e-9)
        return ok, {"sim_end_s": sim_end, "candidates_evaluated": n_trials,
                    "optimizer": config.get("optimizer"), "kpi_weights": config.get("kpi_weights"),
                    "demand_from": Path(str(config.get("tmc_json"))).name,
                    "vehicles": (base.get("moe") or {}).get("vehicles_total"),
                    "best_plan": best.get("plan_id"),
                    "best_score": best.get("score"), "baseline_score": base.get("score")}, ""

    def check_c3(self):
        if len(self.targets) < 2:
            raise SkipCriterion("remote mode optimizes one intersection; corridor coordination "
                                "is checked in local mode")
        problems, evidence = nema_findings(self.ondemand_answer["nema_plan"],
                                           self.signals)
        cycles = evidence["cycles_s"]
        if not cycles or any(abs(c / cycles[0] - round(c / cycles[0])) > 1e-6 for c in cycles):
            problems.append(f"no common cycle: {cycles}")
        if any(not finite(v) for v in evidence["offsets_s"].values()):
            problems.append("an intersection has no offset")
        result = self.run_result(self.runs["ondemand"])
        ran = (result.get("search") or {}).get("ran")
        evidence["search_ran"] = ran
        return not problems and ran is True, evidence, "; ".join(problems)

    def check_c4(self):
        run_id = self.runs["ondemand"]
        with self.http() as client:
            nema = client.get(f"/v1/city/optimizations/{run_id}/nema")
            pdf = client.get(f"/v1/city/optimizations/{run_id}/report.pdf", timeout=180)
        plan = nema.json() if nema.status_code == 200 else {}
        problems, evidence = nema_findings(plan, self.signals)
        if pdf.status_code != 200 or not pdf.content.startswith(b"%PDF"):
            problems.append(f"report.pdf -> HTTP {pdf.status_code}")
        evidence.update({"nema_http": nema.status_code, "schema": plan.get("schema"),
                         "report_pdf_bytes": len(pdf.content)})
        return not problems, evidence, "; ".join(problems)

    def check_sim_metrics(self):
        run_id = self.runs["ondemand"]
        deadline = time.monotonic() + self.args.sim_metrics_timeout_s
        status = None
        with self.http() as client:
            while time.monotonic() < deadline:
                reply = client.get(f"/v1/city/optimizations/{run_id}/sim_metrics")
                status = reply.status_code
                if status == 200:
                    break
                if status not in (202, 409):
                    break
                time.sleep(5)
        if status != 200:
            return False, {"last_http": status}, "simulated metrics not available"
        doc = reply.json()
        (self.out / "sim_metrics_ondemand.json").write_text(json.dumps(doc, indent=2),
                                                           encoding="utf-8")
        wanted = ("volume_veh_h", "aog_pct", "gor_pct", "split_failures", "queue_veh",
                  "control_delay_s", "dos")
        found: dict[str, list[str]] = {}
        problems = []
        for side in ("baseline", "optimized"):
            plan = ((doc.get("plans") or {}).get(side) or {})
            intersections = plan.get("intersections") or {}
            if isinstance(intersections, list):
                intersections = {str(i.get("tls_id", n)): i for n, i in enumerate(intersections)}
            signals = []
            for signal, item in intersections.items():
                text = json.dumps(item)
                missing = [k for k in wanted if f'"{k}"' not in text]
                if missing:
                    problems.append(f"{side}: signal {signal} lacks {missing}")
                signals.append(str(signal))
            if set(signals) != self.signals:
                problems.append(f"{side}: per-signal measures for {sorted(signals)}, expected "
                                f"{sorted(self.signals)}")
            found[side] = sorted(signals)
        return not problems, {"schema": doc.get("schema"), "signals": found,
                              "measures_checked": list(wanted)}, "; ".join(problems)

    def check_scheduled_cadence(self):
        n, cadence = self.args.scheduled_invocations, self.args.cadence_s
        runs_before = {self.runs.get("ondemand")} - {None}
        rows = []
        problems = []
        next_start = time.monotonic()
        for k in range(n):
            delay = next_start - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            started = time.monotonic()
            before = self.snapshot_count()
            answer = self.optimize_corridor(CORRIDOR_ID)
            run = self.newest_run(CORRIDOR_ID, origin="direct", exclude=runs_before)
            run_id = run and run["run_id"]
            if run_id:
                runs_before.add(run_id)
            kpi_problems, kpi_ev = kpi_findings(answer.get("kpis") or {}, DEFAULT_WEIGHTS)
            nema_problems, nema_ev = nema_findings(answer.get("nema_plan") or {},
                                                   self.signals)
            problems += [f"invocation {k + 1}: {p}" for p in kpi_problems + nema_problems]
            rows.append({"invocation": k + 1, "run_id": run_id,
                         "started_utc": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                         "snapshots_buffered": before,
                         "delay_baseline": kpi_ev.get("delay_baseline_s_per_veh"),
                         "delay_optimized": kpi_ev.get("delay_optimized_s_per_veh"),
                         "cost_optimized": kpi_ev.get("cost_optimized"),
                         "tool_seconds": round(time.monotonic() - started, 1)})
            next_start = started + cadence
        distinct = len({r["run_id"] for r in rows if r["run_id"]})
        if distinct != n:
            problems.append(f"{distinct} distinct runs recorded for {n} invocations")
        grew = rows[-1]["snapshots_buffered"] > self.snapshots_after_tmc if rows else False
        if not grew and not self.args.remote:
            # Local mode collects 1-minute windows, so the cadence spans new windows; a
            # deployment's 10-minute windows may not turn over during a short remote run.
            problems.append("the collector injected no new window between the invocations")
        return not problems, {"cadence_s": cadence, "invocations": rows,
                              "snapshots_at_first_check": self.snapshots_after_tmc}, \
            "; ".join(problems)

    def check_hd_optimization(self):
        if self.args.remote:
            raise SkipCriterion("remote mode: needs the OpenDRIVE upload (local mode)")
        snapshot = snapshot_for_hd(datetime.now(timezone.utc) - timedelta(minutes=10))
        self.mcp.call_json("upload_traffic_snapshot", {"snapshot": snapshot})
        answer = self.optimize_corridor(HD_CORRIDOR_ID, cost=CUSTOM_WEIGHTS, targets=INTERSECTIONS)
        self.hd_answer = answer
        run = self.newest_run(HD_CORRIDOR_ID, origin="direct")
        result = self.run_result(run["run_id"]) if run else {}
        config = result.get("config") or {}
        base_sumocfg = str(config.get("base_sumocfg") or "")
        hd_map_ids = sorted(m for m in getattr(self, "hd_map_ids", set()) if m)
        on_hd = config.get("map_id") in hd_map_ids or any(m in base_sumocfg for m in hd_map_ids)
        problems, evidence = kpi_findings(answer.get("kpis") or {}, CUSTOM_WEIGHTS)
        nema_problems, nema_ev = nema_findings(answer.get("nema_plan") or {}, set())
        if len(nema_ev["signals"]) != 3:
            nema_problems.append(f"expected 3 signals on the HD map, got {nema_ev['signals']}")
        problems += nema_problems
        if not on_hd:
            problems.append(f"run did not use the converted HD map ({base_sumocfg})")
        evidence.update({"map_id": config.get("map_id"), "opendrive_map_ids": hd_map_ids,
                         "nema_signals": nema_ev["signals"], "cycle_length_s": nema_ev["cycle_length_s"]})
        return not problems, evidence, "; ".join(problems)

    def check_weights(self):
        if not self.ondemand_answer:
            raise RuntimeError("no On-Demand answer to check")
        hd = getattr(self, "hd_answer", None)
        if hd is None:
            if self.args.remote:
                raise SkipCriterion("the custom-weight run uses the HD map (local mode)")
            raise RuntimeError("the custom-weight run (A4-4) did not complete")
        default_problems, od = kpi_findings(self.ondemand_answer.get("kpis") or {}, DEFAULT_WEIGHTS)
        custom_problems, cu = kpi_findings(hd.get("kpis") or {}, CUSTOM_WEIGHTS)
        problems = default_problems + custom_problems
        return not problems, {
            "default_weights": DEFAULT_WEIGHTS, "default_cost_baseline": od.get("cost_baseline"),
            "default_cost_optimized": od.get("cost_optimized"),
            "custom_weights": CUSTOM_WEIGHTS, "custom_cost_optimized": cu.get("cost_optimized"),
            "formula": "cost = sum(w * metric / baseline metric)"}, "; ".join(problems)

    def check_corridor_inventory(self):
        """camcfg inventory -> TMC spec + map + demand -> one coordinated corridor plan."""
        if self.args.remote:
            raise SkipCriterion("remote mode: the corridor's map must be on the service's asset "
                                "volume; checked in local mode")
        from terasim_tmc_service.spec import CorridorSpec

        out = self.work / "camcfg" / "arterial_32"
        generated = subprocess.run(
            [sys.executable, str(CAMERA_CONFIG_DIR / "camcfg.py"), "generate",
             str(CORRIDOR_INVENTORY), "-o", str(out)],
            capture_output=True, text=True, timeout=600)
        (self.work / "camcfg" / "generate.log").write_text(
            generated.stdout + generated.stderr, encoding="utf-8")
        if generated.returncode != 0:
            return False, {"camcfg_exit": generated.returncode}, generated.stderr[-300:]
        spec_doc = json.loads((out / "terasim" / "Metadata.json").read_text(encoding="utf-8"))
        spec = CorridorSpec.from_payload(spec_doc)  # the TMC collector's own loader
        streams = json.loads((out / "vss" / "vst-streams.json").read_text(encoding="utf-8"))
        counting = sum(len(it["Assignments"]) for it in spec_doc["intersections"])
        # The corridor map: the inventory's OpenStreetMap extract, stored with the map tool.
        osm_source = CORRIDOR_INVENTORY.parent / "inputs" / "arterial_8.osm"
        osm = self.work / "input" / "osm" / osm_source.name
        osm.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(osm_source, osm)
        stored, _ = self.mcp.call_json("upload_map", {"path": str(osm)})
        # Demand: ten minutes of counts on every counted approach (arterial 150, cross street
        # 90 vehicles; 85 % through, 10 % right, 5 % left), legal turns only.
        names = {inter.get("id"): inter for inter in
                 (json.loads((out / "report.json").read_text(encoding="utf-8"))
                  .get("intersections") or [])}
        share = {"Straight": 0.85, "Right": 0.10, "Left": 0.05}
        intersections = []
        for it in spec_doc["intersections"]:
            counts = {}
            for heading in it["Assignments"]:
                volume = 150 if heading in ("East", "West") else 90
                legal = {move for path, move in it["Movements"].items()
                         if path.startswith(f"{heading} - ")}
                counts[heading] = {move: round(volume * share[move]) for move in share
                                   if move in legal}
            intersections.append({"intersection_id": it["intersection_id"],
                                  "name": (names.get(it["intersection_id"]) or {}).get("name")
                                  or it["intersection_id"],
                                  "lat": it["lat"], "lon": it["lon"], "counts": counts})
        start = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.mcp.call_json("upload_traffic_snapshot", {"snapshot": {
            "corridor_id": spec.corridor_id, "observation_interval": "10m",
            "timestamp_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "intersections": intersections}})
        targets = [{"intersection_id": it["intersection_id"], "name": it["name"],
                    "lat": it["lat"], "lon": it["lon"]} for it in intersections]
        started = time.monotonic()
        answer = self.optimize_corridor(spec.corridor_id, targets=targets)
        seconds = time.monotonic() - started
        problems, kpis = kpi_findings(answer.get("kpis") or {}, DEFAULT_WEIGHTS)
        nema_problems, nema_ev = nema_findings(answer.get("nema_plan") or {}, set())
        problems += nema_problems
        signals = nema_ev["signals"]
        if len(signals) != len(spec_doc["intersections"]):
            problems.append(f"plan covers {len(signals)} signals, expected "
                            f"{len(spec_doc['intersections'])}")
        if len(nema_ev["cycles_s"]) != 1:
            problems.append(f"no common cycle: {nema_ev['cycles_s']}")
        if any(not finite(v) for v in nema_ev["offsets_s"].values()):
            problems.append(f"missing offsets: {nema_ev['offsets_s']}")
        return not problems, {
            "corridor_id": spec.corridor_id, "intersections": len(spec_doc["intersections"]),
            "camera_streams": len(streams), "counting_cameras": counting,
            "map_id": stored.get("map_id"),
            "map_traffic_lights": (stored.get("summary") or {}).get("traffic_lights"),
            "signals": signals, "cycle_length_s": nema_ev["cycle_length_s"],
            "offsets_s": nema_ev["offsets_s"], "tool_seconds": round(seconds, 1),
            **{k: kpis.get(k) for k in ("delay_baseline_s_per_veh", "delay_optimized_s_per_veh",
                                        "stops_baseline", "stops_optimized", "vehicles")}}, \
            "; ".join(problems)

    def check_builtin_scheduler(self):
        import httpx

        if self.args.remote:
            with self.http() as client:
                latest = client.get(f"/v1/city/scheduled/{CORRIDOR_ID}/latest").json()
            if not latest.get("run_id"):
                raise SkipCriterion("the deployment's built-in scheduler is off (the default); "
                                    "Scheduled mode is covered by SCH-1")
            run_id = latest["run_id"]
        else:
            if not self.args.builtin_scheduler:
                raise SkipCriterion("--no-builtin-scheduler")
            self.stack.stop_process("service")
            self.stack.start_service(scheduler=True)
            # keep the buffer fresh: the collector injects the current window
            httpx.post(f"{self.tmc}/v1/tmc/run-once", timeout=120)
            deadline = time.monotonic() + self.args.builtin_cadence_s + 600
            run_id = None
            with self.http() as client:
                while time.monotonic() < deadline and not run_id:
                    time.sleep(3)
                    run_id = client.get(f"/v1/city/scheduled/{CORRIDOR_ID}/latest").json().get("run_id")
            if not run_id:
                return False, {"cadence_s": self.args.builtin_cadence_s}, \
                    "the built-in scheduler submitted no run within the wait"
        status = None
        deadline = time.monotonic() + self.args.tool_timeout_s
        with self.http() as client:
            while time.monotonic() < deadline:
                status = client.get(f"/v1/city/optimizations/{run_id}/status").json().get("status")
                if status in ("completed", "failed", "lost"):
                    break
                time.sleep(3)
            nema = client.get(f"/v1/city/optimizations/{run_id}/nema")
        result = self.run_result(run_id) if status == "completed" else {}
        with self.http() as client:
            runs = client.get("/v1/city/optimizations",
                              params={"corridor_id": CORRIDOR_ID, "limit": 50}).json()
        listing = next((run for run in runs if run["run_id"] == run_id), None)
        if self.stack is not None:
            # The scheduler keeps submitting every cadence; stop it once the check is done.
            self.stack.stop_process("service-scheduler")
        nema_problems, nema_ev = nema_findings(nema.json() if nema.status_code == 200 else {},
                                               self.signals)
        best = (result.get("best") or {})
        ok = (status == "completed" and not nema_problems and finite(best.get("score"))
              and listing is not None and listing.get("origin") == "scheduled")
        return ok, {"run_id": run_id, "status": status, "origin": listing and listing.get("origin"),
                    "cadence_s": self.args.builtin_cadence_s, "best_score": best.get("score"),
                    "nema_signals": nema_ev["signals"]}, "; ".join(nema_problems)

    def run_adversarial(self, label: str, adversities: list[str] | str,
                        sampling_probability: float = 0.5) -> dict:
        """One NADE run on the demo corridor (child process); returns its evidence."""
        out_dir = self.out / "adversarial" / label
        if out_dir.exists():
            shutil.move(str(out_dir), str(out_dir) + f".previous-{int(time.time())}")
        out_dir.mkdir(parents=True)
        spec = {"adversities": adversities, "sampling_probability": sampling_probability}
        cmd = [sys.executable, str(Path(__file__).resolve()), "--adversarial-child",
               str(out_dir), json.dumps(spec)]
        # The NADE environment writes diagnostics relative to the working directory; a fixed
        # hash seed (with the seeded random generators of the child) makes the run repeatable.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=out_dir,
                              env={**os.environ, "PYTHONHASHSEED": "0"})
        (out_dir / "child.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        monitor = out_dir / "monitor.json"
        if proc.returncode != 0 or not monitor.is_file():
            raise RuntimeError(f"NADE run {label} failed (exit {proc.returncode}): "
                               f"{(proc.stderr or proc.stdout)[-300:]}")
        doc = json.loads(monitor.read_text(encoding="utf-8"))
        events = doc.get("event_info") or {}
        kinds = sorted({info.get("predicted_collision_type")
                        for event in events.values()
                        for info in (event.get("adversarial_info_dict") or {}).values()})
        collisions = (out_dir / "collision.xml").read_text(encoding="utf-8").count("<collision ") \
            if (out_dir / "collision.xml").is_file() else 0
        self.adversarial = getattr(self, "adversarial", {})
        self.adversarial[label] = doc
        return {"adversities": adversities, "sampling_probability": sampling_probability,
                "adversarial_events": len(events), "event_types": kinds,
                "collisions": collisions, "sim_time_s": doc.get("final_time"),
                "finish_reason": doc.get("finish_reason"),
                "importance_weight": doc.get("weight"),
                "background_vehicles_at_warmup": doc.get("warmup_vehicle_num")}

    def check_adversities_all(self):
        evidence = self.run_adversarial("intersection-all", "all")
        return evidence["adversarial_events"] >= 1, evidence, ""

    def check_adversities_tunable(self):
        only_tfl = self.run_adversarial("intersection-tfl-only", ["intersection_tfl"])
        none = self.run_adversarial("no-adversities", [])
        problems = []
        if only_tfl["adversarial_events"] < 1 or only_tfl["event_types"] != ["intersection_tfl"]:
            problems.append(f"tfl-only run produced {only_tfl['event_types']}")
        if none["adversarial_events"] != 0:
            problems.append(f"run without adversities produced {none['adversarial_events']} events")
        return not problems, {"tfl_only": only_tfl, "no_adversities": none}, "; ".join(problems)

    def check_adversity_files(self):
        import yaml
        from hydra.utils import get_class

        counts: dict[str, int] = {}
        problems = []
        for path in sorted(ADVERSITY_DIR.rglob("*.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for group, entries in (doc.get("adversity_cfg") or {}).items():
                for name, cfg in (entries or {}).items():
                    counts[group] = counts.get(group, 0) + 1
                    try:
                        get_class(cfg["_target_"])
                    except Exception as exc:  # noqa: BLE001 - report every unresolved file
                        problems.append(f"{path.name}: {cfg.get('_target_')} ({type(exc).__name__})")
        total = sum(counts.values())
        return total > 0 and not problems, {"parameter_files": total, "by_group": counts}, \
            "; ".join(problems)

    def check_behaviors(self):
        from terasim_city.factories import VEHICLE_BEHAVIORS

        names = sorted(VEHICLE_BEHAVIORS)
        runs = getattr(self, "adversarial", {})
        warmup = [doc.get("warmup_vehicle_num") for doc in runs.values()]
        distance = [doc.get("total_distance") for doc in runs.values() if doc.get("total_distance")]
        ok = "cnde" in names and "sumo_passive" in names and any(
            isinstance(v, int) and v > 0 for v in warmup)
        return ok, {"vehicle_behaviors": names, "nade_background_vehicles": warmup,
                    "nade_factory": "terasim_nde_nade.vehicle.nde_vehicle_factory.NDEVehicleFactory"}, \
            "the Neural NDE runtime module and checkpoint are installed separately (A1-2, A1-3)"


    def nnde_run(self) -> dict:
        """One asynchronous optimization with the re-evaluation stage on (shared by A1-2/A1-3).

        The demand is a snapshot pushed for a corridor id without a scheduled-config entry,
        so the worker selects a covering stored map: a map-store package, which the Neural
        NDE behavior needs for its map artifacts.
        """
        cached = getattr(self, "_nnde_run", None)
        if cached is not None:
            return cached
        import httpx

        a = self.args
        if self.stack is not None and "service-refine" not in self.stack.procs:
            for name in ("service", "service-scheduler"):
                self.stack.stop_process(name)
            self.stack.start_service(refine=True)
        if a.neural_nde:
            # The Neural NDE model is used with OpenStreetMap-based networks: store the
            # corridor's OSM version; it is the only stored map of this profile.
            stored, _ = self.mcp.call_json("upload_map", {"path": str(self.inputs["osm"])})
            self.nnde_map = {"map_id": stored.get("map_id"), "source": stored.get("source"),
                             "traffic_lights": (stored.get("summary") or {}).get("traffic_lights")}
        snapshot = snapshot_for_hd(datetime.now(timezone.utc) - timedelta(minutes=10),
                                   NNDE_CORRIDOR_ID)
        self.mcp.call_json("upload_traffic_snapshot", {"snapshot": snapshot})
        request = {"corridor_id": NNDE_CORRIDOR_ID,
                   "corridor": {"from": INTERSECTIONS[0]["intersection_id"],
                                "to": INTERSECTIONS[-1]["intersection_id"]},
                   "sim_end": a.nnde_sim_end, "max_trials": a.nnde_max_trials,
                   "patience": a.nnde_max_trials, "n_workers": min(2, a.n_workers)}
        started = time.monotonic()
        gpu_baseline = gpu_memory_used_mib()
        gpu_peak = gpu_baseline
        with self.http() as client:
            reply = client.post("/v1/city/optimizations", json=request)
            if reply.status_code != 202:
                raise RuntimeError(f"submission answered HTTP {reply.status_code}: {reply.text[:300]}")
            run_id = reply.json()["run_id"]
            status = None
            deadline = time.monotonic() + a.tool_timeout_s
            while time.monotonic() < deadline:
                status = client.get(f"/v1/city/optimizations/{run_id}/status").json().get("status")
                used = gpu_memory_used_mib()
                if used is not None and (gpu_peak is None or used > gpu_peak):
                    gpu_peak = used
                if status in ("completed", "failed", "lost"):
                    break
                time.sleep(5)
            nema = client.get(f"/v1/city/optimizations/{run_id}/nema")
        result = self.run_result(run_id) if status == "completed" else {}
        if self.stack is not None:
            self.stack.stop_process("service-refine")
        self._nnde_run = {"run_id": run_id, "status": status, "request": request,
                          "seconds": round(time.monotonic() - started, 1), "result": result,
                          "gpu_memory_peak_delta_mib": (gpu_peak - gpu_baseline)
                          if gpu_peak is not None and gpu_baseline is not None else None,
                          "nema": nema.json() if nema.status_code == 200 else {}}
        (self.out / "neural_nde_run.json").write_text(
            json.dumps({k: v for k, v in self._nnde_run.items() if k != "result"}
                       | {"refine": result.get("refine"),
                          "export_source": result.get("export_source"),
                          "best": result.get("best")}, indent=2, default=str),
            encoding="utf-8")
        return self._nnde_run

    def check_neural_nde_interface(self):
        if self.args.remote:
            raise SkipCriterion("the re-evaluation stage is a deployment setting of the service; "
                                "checked in local mode")
        run = self.nnde_run()
        result = run["result"]
        refine = result.get("refine") or {}
        runtime = neural_nde_runtime()
        installed = bool(runtime.get("runtime_installed"))
        # The worker picks any stored map that covers the corridor (the SUMO map or one of
        # its OpenDRIVE conversions, whose signal ids differ): expect three signals.
        nema_problems, nema_ev = nema_findings(run["nema"], set())
        if len(nema_ev["signals"]) != len(INTERSECTIONS):
            nema_problems.append(f"expected {len(INTERSECTIONS)} signals, got {nema_ev['signals']}")
        baseline = refine.get("baseline") or {}
        reasons = [str(item.get("reason", ""))[:160] for item in refine.get("invalid") or []]
        problems = list(nema_problems)
        if run["status"] != "completed":
            problems.append(f"run status {run['status']}")
        if not refine.get("executed") or refine.get("behavior") != "cnde":
            problems.append(f"re-evaluation stage did not run with behavior cnde: {refine}")
        if (refine.get("candidates_evaluated") or 0) < 2:
            problems.append("fewer than two plans re-evaluated")
        expected_source = "refine" if installed else "coarse"
        if result.get("export_source") != expected_source:
            problems.append(f"export_source {result.get('export_source')!r}, expected "
                            f"{expected_source!r} (runtime installed: {installed})")
        evidence = {"run_id": run["run_id"], "map": getattr(self, "nnde_map", None) or "stored map selected by coordinates",
                    "refine_behavior": refine.get("behavior"),
                    "refine_top_k": refine.get("top_k"),
                    "plans_reevaluated": refine.get("candidates_evaluated"),
                    "runtime_installed": installed,
                    "export_source": result.get("export_source"),
                    "nema_signals": nema_ev["signals"], "seconds": run["seconds"]}
        if not installed:
            evidence["reevaluation_without_runtime"] = refine_reason_from_log(
                self.stack.logs / "service-refine.log" if self.stack else None) or (
                reasons[0] if reasons else "")
            detail = ("the Neural NDE runtime is a separate module, not installed here: the "
                      "stage reports it and the SUMO-stage plan is exported")
        else:
            evidence["baseline_reevaluated"] = bool(baseline.get("valid"))
            detail = ""
        return not problems, evidence, "; ".join(problems) or detail

    def check_neural_nde_reevaluation(self):
        run = self.nnde_run()
        result = run["result"]
        refine = result.get("refine") or {}
        problems = []
        if run["status"] != "completed":
            problems.append(f"run status {run['status']}")
        if result.get("export_source") != "refine":
            problems.append(f"export_source {result.get('export_source')!r}, expected 'refine'")
        entries = [refine.get("baseline") or {}] + list(refine.get("evaluations") or [])
        if refine.get("invalid"):
            problems.append(f"invalid re-evaluations: {refine.get('invalid')}"[:300])
        rows = []
        for entry in entries:
            moe = entry.get("moe") or {}
            if not entry.get("valid") or not finite(moe.get("control_delay_s_per_veh")):
                problems.append(f"plan {entry.get('plan_id')} has no valid Neural NDE evaluation")
            rows.append({"plan": entry.get("plan_id"), "score": entry.get("score"),
                         "delay_s_per_veh": moe.get("control_delay_s_per_veh"),
                         "stops_per_veh": moe.get("stops_per_veh"),
                         "vehicles": moe.get("vehicles_total")})
        if not refine.get("winner_plan_id"):
            problems.append("no re-evaluation winner")
        coarse = (result.get("best") or {}).get("plan_id")
        evidence = {"run_id": run["run_id"], "device": neural_nde_runtime().get("cuda_device"),
                    "sim_end_s": run["request"]["sim_end"], "plans": rows,
                    "coarse_winner": coarse, "exported_plan": refine.get("winner_plan_id"),
                    "gpu_memory_peak_delta_mib": run.get("gpu_memory_peak_delta_mib"),
                    "seconds": run["seconds"]}
        return not problems, evidence, "; ".join(problems)

    def check_cnde_traffic_model(self):
        """A1-4: ``traffic_model="cnde"`` on an optimize tool call, as the VSS agent sends it.

        The deployment keeps SUMO traffic (no re-evaluation stage) and bounds a Neural NDE
        run with TERASIM_CITY_CNDE_MAX_WORKERS / _MAX_TRIALS. The corridor's entry names a
        map package under the input root that holds its Neural NDE map artifacts (the
        OpenStreetMap package of the A1-2/A1-3 run, as a deployment mounts one) and a budget
        larger than those limits. The same call without ``traffic_model`` then runs under SUMO
        with the entry's own budget.
        """
        a = self.args
        map_id = (getattr(self, "nnde_map", None) or {}).get("map_id")
        if not map_id:
            stored, _ = self.mcp.call_json("upload_map", {"path": str(self.inputs["osm"])})
            map_id = stored.get("map_id")
        stored_package = self.stack.data_root / "maps" / str(map_id)
        package = self.work / "input" / "cnde_map"
        if not package.exists():
            shutil.copytree(stored_package, package)
        artifacts = sorted(path.name for path in package.glob("cnde_*.pkl"))
        net = (package / "net.net.xml").read_text(encoding="utf-8")
        signals = sorted(set(re.findall(r'<tlLogic id="([^"]+)"', net)))
        scheduled = self.work / "input" / "scheduled_config_cnde.yaml"
        scheduled.write_text(
            Path(self.inputs["scheduled"]).read_text(encoding="utf-8")
            + f"  {NNDE_CORRIDOR_ID}:\n"
            f"    base_sumocfg: {package / 'base.sumocfg'}\n"
            "    corridor:\n"
            f"      tls_ids: {json.dumps(signals)}\n"
            f"    sim_end: {a.cnde_sim_end}\n"
            f"    max_trials: {CNDE_ENTRY_TRIALS}\n"
            f"    patience: {CNDE_ENTRY_TRIALS}\n"
            f"    n_workers: {CNDE_ENTRY_WORKERS}\n",
            encoding="utf-8")
        for name in ("service", "service-scheduler", "service-refine"):
            self.stack.stop_process(name)
        self.stack.start_service(cnde_traffic=True, scheduled=scheduled)
        if getattr(self, "_nnde_run", None) is None:
            snapshot = snapshot_for_hd(datetime.now(timezone.utc) - timedelta(minutes=10),
                                       NNDE_CORRIDOR_ID)
            self.mcp.call_json("upload_traffic_snapshot", {"snapshot": snapshot})

        call = {"corridor_id": NNDE_CORRIDOR_ID,
                "from_intersection": public(INTERSECTIONS[0]),
                "to_intersection": public(INTERSECTIONS[-1])}
        sampler = GpuSampler().start()
        started = time.monotonic()
        cnde_answer, cnde_texts = self.mcp.call_json("optimize_corridor",
                                                     {**call, "traffic_model": "cnde"})
        cnde_seconds = round(time.monotonic() - started, 1)
        gpu_peak_delta = sampler.stop()
        started = time.monotonic()
        sumo_answer, sumo_texts = self.mcp.call_json("optimize_corridor", call)
        sumo_seconds = round(time.monotonic() - started, 1)

        try:
            with self.http() as client:
                records = client.get("/v1/city/optimizations",
                                     params={"corridor_id": NNDE_CORRIDOR_ID, "limit": 10}).json()
            direct = [run for run in records if run.get("origin") == "direct"]
            cnde_run = next((run for run in direct if run.get("traffic_model") == "cnde"), None)
            sumo_run = next((run for run in direct if run.get("traffic_model") == "sumo"), None)
            cnde_result = self.run_result(cnde_run["run_id"]) if cnde_run else {}
            sumo_result = self.run_result(sumo_run["run_id"]) if sumo_run else {}
        finally:
            self.stack.stop_process("service-cnde")
        problems = []
        if cnde_run is None or sumo_run is None:
            problems.append(f"expected one direct run per traffic model, listed: "
                            f"{[(r.get('run_id'), r.get('traffic_model')) for r in direct]}")
        cnde_config = cnde_result.get("config") or {}
        sumo_config = sumo_result.get("config") or {}
        cnde_markdown = cnde_texts[0] if cnde_texts else ""
        sumo_markdown = sumo_texts[0] if sumo_texts else ""

        if cnde_answer.get("traffic_model") != "cnde":
            problems.append(f"CNDE answer traffic_model {cnde_answer.get('traffic_model')!r}")
        if CNDE_FACTS_LINE not in cnde_markdown:
            problems.append("the CNDE answer's markdown block lacks the traffic-model line")
        if cnde_result.get("traffic_model") != "cnde" or cnde_config.get("vehicle_behavior") != "cnde":
            problems.append(f"CNDE run result: traffic_model={cnde_result.get('traffic_model')!r}, "
                            f"vehicle_behavior={cnde_config.get('vehicle_behavior')!r}")
        if cnde_config.get("n_workers") != min(CNDE_ENTRY_WORKERS, a.cnde_max_workers):
            problems.append(f"CNDE run n_workers {cnde_config.get('n_workers')}, expected "
                            f"{min(CNDE_ENTRY_WORKERS, a.cnde_max_workers)}")
        cnde_trials = cnde_result.get("n_trials")
        sumo_trials = sumo_result.get("n_trials")
        if not (isinstance(cnde_trials, int) and isinstance(sumo_trials, int)
                and 1 < cnde_trials < sumo_trials):
            problems.append(f"trials: CNDE {cnde_trials}, SUMO {sumo_trials} (the CNDE limit "
                            f"{a.cnde_max_trials} applies to the CNDE run only)")
        if "traffic_model" in sumo_answer or "Traffic model" in sumo_markdown:
            problems.append("the answer without traffic_model names a traffic model")
        if sumo_config.get("vehicle_behavior") != "sumo_passive" or "traffic_model" in sumo_result:
            problems.append(f"SUMO run result: vehicle_behavior={sumo_config.get('vehicle_behavior')!r}")
        kpi_problems, kpis = kpi_findings(cnde_answer.get("kpis") or {}, DEFAULT_WEIGHTS)
        problems += [f"CNDE KPIs: {problem}" for problem in kpi_problems]

        (self.out / "cnde_traffic_model_run.json").write_text(json.dumps({
            "map_package": {"map_id": map_id, "artifacts": artifacts, "signals": signals},
            "entry": {"sim_end": a.cnde_sim_end, "max_trials": CNDE_ENTRY_TRIALS,
                      "n_workers": CNDE_ENTRY_WORKERS},
            "limits": {"TERASIM_CITY_CNDE_MAX_WORKERS": a.cnde_max_workers,
                       "TERASIM_CITY_CNDE_MAX_TRIALS": a.cnde_max_trials},
            "cnde": {"run": cnde_run, "seconds": cnde_seconds, "markdown": cnde_markdown,
                     "kpis": cnde_answer.get("kpis"), "traffic_model": cnde_answer.get("traffic_model"),
                     "result_config": cnde_config, "n_trials": cnde_trials,
                     "gpu_memory_peak_delta_mib": gpu_peak_delta},
            "sumo": {"run": sumo_run, "seconds": sumo_seconds, "markdown": sumo_markdown,
                     "kpis": sumo_answer.get("kpis"), "result_config": sumo_config,
                     "n_trials": sumo_trials},
        }, indent=2, default=str), encoding="utf-8")
        evidence = {
            "tool": "optimize_corridor", "map_package_artifacts": artifacts,
            "cnde_run_id": cnde_run and cnde_run["run_id"],
            "cnde_record_traffic_model": cnde_run and cnde_run.get("traffic_model"),
            "cnde_vehicle_behavior": cnde_config.get("vehicle_behavior"),
            "cnde_n_workers": cnde_config.get("n_workers"), "cnde_trials": cnde_trials,
            "cnde_sim_end_s": a.cnde_sim_end,
            "cnde_delay_s_per_veh": [kpis.get("delay_baseline_s_per_veh"),
                                     kpis.get("delay_optimized_s_per_veh")],
            "cnde_answer_names_traffic_model": CNDE_FACTS_LINE in cnde_markdown,
            "cnde_seconds": cnde_seconds, "gpu_memory_peak_delta_mib": gpu_peak_delta,
            "sumo_run_id": sumo_run and sumo_run["run_id"],
            "sumo_record_traffic_model": sumo_run and sumo_run.get("traffic_model"),
            "sumo_vehicle_behavior": sumo_config.get("vehicle_behavior"),
            "sumo_trials": sumo_trials, "sumo_seconds": sumo_seconds,
        }
        return not problems, evidence, "; ".join(problems)


class SkipCriterion(Exception):
    pass


class GpuSampler:
    """Samples the GPU memory in use (nvidia-smi) every few seconds while a call runs."""

    def __init__(self, interval_s: float = 2.0) -> None:
        self.interval_s = interval_s
        self.baseline = None
        self.peak = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "GpuSampler":
        self.baseline = self.peak = gpu_memory_used_mib()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            used = gpu_memory_used_mib()
            if used is not None and (self.peak is None or used > self.peak):
                self.peak = used

    def stop(self) -> int | None:
        """Peak memory above the level at start, MiB (None without nvidia-smi)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self.peak is None or self.baseline is None:
            return None
        return self.peak - self.baseline


def neural_nde_runtime() -> dict:
    """Whether the optional Neural NDE runtime (module csim) and its checkpoint are installed."""
    import importlib.util

    info: dict[str, Any] = {"runtime_installed": False}
    try:
        spec = importlib.util.find_spec("csim")
    except (ImportError, ValueError):
        spec = None
    if spec is None or not spec.submodule_search_locations:
        return info
    info["runtime_installed"] = True
    root = Path(list(spec.submodule_search_locations)[0]).parent
    checkpoint = root / "assets" / "nn_weights.pth"
    info["checkpoint_bytes"] = checkpoint.stat().st_size if checkpoint.is_file() else 0
    try:
        import torch

        info["torch"] = torch.__version__
        info["cuda_available"] = bool(torch.cuda.is_available())
        if info["cuda_available"]:
            info["cuda_device"] = torch.cuda.get_device_name(0)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        info["torch_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return info


def refine_reason_from_log(path: Path | None) -> str:
    """Why the re-evaluation stage produced no baseline evaluation (the service logs it)."""
    if path is None or not path.is_file():
        return ""
    found = re.findall(r"produced no valid evaluation under vehicle_behavior '[^']+' \((.*?)\);",
                       path.read_text(encoding="utf-8", errors="replace"))
    return found[-1][:160] if found else ""


def gpu_memory_used_mib() -> int | None:
    """GPU memory in use on the first device (nvidia-smi), or None without one."""
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout
        return int(out.split()[0])
    except (subprocess.SubprocessError, ValueError, IndexError):
        return None


def public(inter: dict) -> dict:
    return {k: inter[k] for k in ("intersection_id", "name", "lat", "lon")}


# ---------------------------------------------------------------------------
# Adversarial environment child process (NADE on the demo corridor)
# ---------------------------------------------------------------------------

def adversarial_child(out_dir: Path, spec: dict) -> int:
    import yaml

    wanted = spec.get("adversities", "all")
    adversities: dict[str, dict] = {"vehicle": {}}
    for path in sorted((ADVERSITY_DIR / "vehicle").glob("intersection_*.yaml")):
        for name, cfg in yaml.safe_load(path.read_text())["adversity_cfg"]["vehicle"].items():
            if wanted == "all" or name in wanted:
                adversities["vehicle"][name] = cfg
    probability = float(spec.get("sampling_probability", 0.5))
    config = {
        "environment": {"module": "terasim_nde_nade.envs", "class": "NADE", "parameters": {
            "vehicle_factory": "terasim_nde_nade.vehicle.nde_vehicle_factory.NDEVehicleFactory",
            "info_extractor": "terasim.logger.infoextractor.InfoExtractor",
            "log_flag": True, "warmup_time_lb": 60, "warmup_time_ub": 61, "run_time": 300,
            "MOBIL_lc_flag": True, "stochastic_acc_flag": False, "drive_rule": "righthand",
            "adversity_sampling_probability": probability, "adversity_cfg": adversities}},
        "input": {"sumo_net_file": str(DEMO_DIR / "demo.net.xml"),
                  "sumo_config_file": str(DEMO_DIR / "demo_scenario.sumocfg")},
        "simulator": {"module": "terasim.simulator", "class": "Simulator", "parameters": {
            "num_tries": 10, "gui_flag": False, "realtime_flag": False, "sumo_seed": 42,
            "sumo_output_file_types": ["collision", "tripinfo"]}},
    }
    from terasim_service.utils.base import create_environment, create_simulator, set_random_seed

    set_random_seed(int(spec.get("seed", 42)))
    env = create_environment(config, out_dir)
    sim = create_simulator(config, out_dir)
    sim.bind_env(env)
    sim.run()
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path,
                        default=Path(f"acceptance-out-{datetime.now():%Y%m%d-%H%M%S}"))
    parser.add_argument("--sim-end", type=float, default=1800.0,
                        help="simulated seconds per candidate plan (contract: 1800)")
    parser.add_argument("--max-trials", type=int, default=24,
                        help="candidate plans per optimization (the deployment default)")
    parser.add_argument("--n-workers", type=int, default=4)
    parser.add_argument("--scheduled-invocations", type=int, default=2)
    parser.add_argument("--cadence-s", type=float, default=60.0,
                        help="cadence of the caller-driven Scheduled mode")
    parser.add_argument("--builtin-scheduler", action=argparse.BooleanOptionalAction, default=True,
                        help="also check the optional built-in scheduler (local mode)")
    parser.add_argument("--builtin-cadence-s", type=float, default=60.0)
    parser.add_argument("--tool-timeout-s", type=float, default=1800.0)
    parser.add_argument("--sim-metrics-timeout-s", type=float, default=600.0)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--service", default="http://127.0.0.1:8100")
    parser.add_argument("--mcp", default="http://127.0.0.1:9911/mcp")
    parser.add_argument("--tmc", default="http://127.0.0.1:8200")
    parser.add_argument("--map-path", default="/data/input/map/demo.sumocfg",
                        help="remote mode: the map file on the service's asset volume")
    parser.add_argument("--neural-nde", action="store_true",
                        help="Neural NDE profile (needs the separately installed runtime and "
                             "checkpoint and a CUDA GPU): environment, health, A1-2, A1-3, A1-4")
    parser.add_argument("--refine-top-k", type=int, default=2,
                        help="candidates re-evaluated by the Neural NDE stage (A1-2/A1-3)")
    parser.add_argument("--nnde-sim-end", type=float, default=None,
                        help="simulated seconds per plan in the A1-2/A1-3 run "
                             "(default: 600 with --neural-nde, else 300)")
    parser.add_argument("--nnde-max-trials", type=int, default=None,
                        help="coarse candidates of the A1-2/A1-3 run (default 6, else 4)")
    parser.add_argument("--cnde-sim-end", type=float, default=300.0,
                        help="simulated seconds per candidate of the traffic_model=cnde call (A1-4)")
    parser.add_argument("--cnde-max-trials", type=int, default=2,
                        help="TERASIM_CITY_CNDE_MAX_TRIALS of the A1-4 deployment")
    parser.add_argument("--cnde-max-workers", type=int, default=2,
                        help="TERASIM_CITY_CNDE_MAX_WORKERS of the A1-4 deployment")
    parser.add_argument("--cnde-fast", action="store_true",
                        help="Neural NDE fast mode for A1-2 to A1-4: "
                             "TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY=1 and two OpenMP / MKL / "
                             "OpenBLAS threads per trial process")
    args = parser.parse_args(argv)
    if args.nnde_sim_end is None:
        args.nnde_sim_end = 600.0 if args.neural_nde else 300.0
    if args.nnde_max_trials is None:
        args.nnde_max_trials = 6 if args.neural_nde else 4
    if args.neural_nde and args.remote:
        parser.error("--neural-nde runs in local mode")
    return args


def main(argv: list[str]) -> int:
    if argv[:1] == ["--adversarial-child"]:
        return adversarial_child(Path(argv[1]), json.loads(argv[2]))
    args = parse_args(argv)
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; choose a new --out directory")
    out.mkdir(parents=True, exist_ok=True)
    report = Report()
    if args.neural_nde:
        wanted = {"ENV-1", "A3-1", "A1-2"}
        selected = [c for c in CRITERIA if c[0] in wanted] + NEURAL_NDE_CRITERIA
    else:
        selected = CRITERIA
    for cid, component, title in selected:
        report.define(cid, component, title)
    try:
        AcceptanceRun(args, report, out).run()
    finally:
        report.write(out)
    print(f"\nreport: {out / 'acceptance_report.md'}")
    print("RESULT:", "PASS" if not report.failed else
          "FAIL (" + ", ".join(c.cid for c in report.failed) + ")")
    return 0 if not report.failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
