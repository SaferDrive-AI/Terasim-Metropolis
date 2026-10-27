#!/usr/bin/env python3
"""Build the 3-intersection corridor demo map and its data assets.

Generates, into this script's own directory:

* ``demo.nod.xml`` / ``demo.edg.xml`` / ``demo.con.xml`` — hand-written
  netconvert inputs for an E-W arterial with three signalised junctions
  (J1, J2, J3, ~300 m apart) and N-S cross-street stubs at each.
* ``demo.net.xml`` — netconvert output, geo-referenced (``--proj.utm``).
  netconvert emits the default static TLS program ``0`` for each junction.
* ``demo.tls.xml`` — one alternative static program ``alt_fixed`` per
  junction (program ``0`` phase states with uniform 30 s greens).
* ``demo.rou.xml`` — empty ``<routes/>`` placeholder (the TMC pipeline
  generates the real routes).
* ``demo.sumocfg`` — parseable by ``terasim_city.scenario.sumocfg_io``.
* ``demo_tmc.json`` + ``snapshots/snap_01..06.json`` — TMC payloads whose
  lat/lon are read back from the *generated* net (exact geo match).
* ``baseline_sheet.json`` — timing sheet (terasim.timing_sheet.v1).
* ``scheduled_config.yaml`` — scheduler corridor config.

Idempotent: rerunning regenerates everything in place. Sanity checks at the
end: sumolib import of the net, TLS program presence, a 10 s headless SUMO
run, and a full ``run_tmc_pipeline`` call that must yield at least one flow.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from lxml import etree as ET

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Geometry: synthetic lon/lat grid centred on (37.335, -121.900), ~300 m spacing.
# ---------------------------------------------------------------------------

CENTER_LAT = 37.3350
# Metres-per-degree at this latitude (WGS84 approximations, plenty for a demo).
M_PER_DEG_LAT = 110_950.0
M_PER_DEG_LON = 88_500.0
SPACING_M = 300.0
DLON = SPACING_M / M_PER_DEG_LON
DLAT = SPACING_M / M_PER_DEG_LAT

# E-W arterial chain: 2 entry segments, 3 signalised junctions, 2 exit segments.
# lon index -3..+3 relative to J2 at -121.900 + 0 (J2 centred for symmetry).
J2_LON = -121.9000

ARTERIAL_CHAIN = ["W2", "W1", "J1", "J2", "J3", "E1", "E2"]
ARTERIAL_LON = {name: J2_LON + (i - 3) * DLON for i, name in enumerate(ARTERIAL_CHAIN)}

TLS_NODES = ("J1", "J2", "J3")

ARTERIAL_SPEED = "13.89"  # 50 km/h
CROSS_SPEED = "11.11"     # 40 km/h


def _nodes() -> list[tuple[str, float, float, str]]:
    """(id, lon, lat, type) for every node in the demo network."""
    rows: list[tuple[str, float, float, str]] = []
    for name in ARTERIAL_CHAIN:
        ntype = "traffic_light" if name in TLS_NODES else "priority"
        rows.append((name, ARTERIAL_LON[name], CENTER_LAT, ntype))
    # Cross-street arm nodes: two segments (~300 m each) north and south.
    for j in TLS_NODES:
        lon = ARTERIAL_LON[j]
        for step in (1, 2):
            rows.append((f"{j}_N{step}", lon, CENTER_LAT + step * DLAT, "priority"))
            rows.append((f"{j}_S{step}", lon, CENTER_LAT - step * DLAT, "priority"))
    return rows


def _edges() -> list[tuple[str, str, str, int, str, int]]:
    """(id, from, to, numLanes, speed, priority) for every edge."""
    rows: list[tuple[str, str, str, int, str, int]] = []
    # Arterial: eastbound EB_e along the chain, westbound WB_e along the reverse.
    for e in range(len(ARTERIAL_CHAIN) - 1):
        a, b = ARTERIAL_CHAIN[e], ARTERIAL_CHAIN[e + 1]
        rows.append((f"EB_{e}", a, b, 2, ARTERIAL_SPEED, 3))
    rchain = list(reversed(ARTERIAL_CHAIN))
    for e in range(len(rchain) - 1):
        a, b = rchain[e], rchain[e + 1]
        rows.append((f"WB_{e}", a, b, 2, ARTERIAL_SPEED, 3))
    # Cross streets: northbound comes from the south arm, southbound from north.
    for j in TLS_NODES:
        rows += [
            (f"NB_{j}_0", f"{j}_S2", f"{j}_S1", 1, CROSS_SPEED, 2),
            (f"NB_{j}_1", f"{j}_S1", j, 1, CROSS_SPEED, 2),
            (f"NB_{j}_2", j, f"{j}_N1", 1, CROSS_SPEED, 2),
            (f"NB_{j}_3", f"{j}_N1", f"{j}_N2", 1, CROSS_SPEED, 2),
            (f"SB_{j}_0", f"{j}_N2", f"{j}_N1", 1, CROSS_SPEED, 2),
            (f"SB_{j}_1", f"{j}_N1", j, 1, CROSS_SPEED, 2),
            (f"SB_{j}_2", j, f"{j}_S1", 1, CROSS_SPEED, 2),
            (f"SB_{j}_3", f"{j}_S1", f"{j}_S2", 1, CROSS_SPEED, 2),
        ]
    return rows


def _junction_io(j: str) -> dict[str, str]:
    """Incoming/outgoing edge ids at a signalised junction."""
    k = ARTERIAL_CHAIN.index(j)          # 2, 3, 4
    rk = len(ARTERIAL_CHAIN) - 1 - k     # index of j in the reversed chain
    return {
        "eb_in": f"EB_{k - 1}", "eb_out": f"EB_{k}",
        "wb_in": f"WB_{rk - 1}", "wb_out": f"WB_{rk}",
        "nb_in": f"NB_{j}_1", "nb_out": f"NB_{j}_2",
        "sb_in": f"SB_{j}_1", "sb_out": f"SB_{j}_2",
    }


def _connections() -> list[dict[str, str]]:
    """Explicit connections at the three TLS junctions.

    Arterial approaches (2 lanes): lane 0 = through + right, lane 1 =
    through + left. Cross-street approaches (1 lane): left/through/right.
    """
    rows: list[dict[str, str]] = []

    def conn(frm: str, to: str, fl: int, tl: int) -> dict[str, str]:
        return {"from": frm, "to": to, "fromLane": str(fl), "toLane": str(tl)}

    for j in TLS_NODES:
        io = _junction_io(j)
        # Eastbound approach: right -> south out, through, left -> north out.
        rows += [
            conn(io["eb_in"], io["sb_out"], 0, 0),
            conn(io["eb_in"], io["eb_out"], 0, 0),
            conn(io["eb_in"], io["eb_out"], 1, 1),
            conn(io["eb_in"], io["nb_out"], 1, 0),
        ]
        # Westbound approach: right -> north out, through, left -> south out.
        rows += [
            conn(io["wb_in"], io["nb_out"], 0, 0),
            conn(io["wb_in"], io["wb_out"], 0, 0),
            conn(io["wb_in"], io["wb_out"], 1, 1),
            conn(io["wb_in"], io["sb_out"], 1, 0),
        ]
        # Northbound approach: right -> east out, through, left -> west out.
        rows += [
            conn(io["nb_in"], io["eb_out"], 0, 0),
            conn(io["nb_in"], io["nb_out"], 0, 0),
            conn(io["nb_in"], io["wb_out"], 0, 1),
        ]
        # Southbound approach: right -> west out, through, left -> east out.
        rows += [
            conn(io["sb_in"], io["wb_out"], 0, 0),
            conn(io["sb_in"], io["sb_out"], 0, 0),
            conn(io["sb_in"], io["eb_out"], 0, 1),
        ]
    return rows


# ---------------------------------------------------------------------------
# netconvert input writers
# ---------------------------------------------------------------------------

def write_netconvert_inputs() -> None:
    nod = ET.Element("nodes")
    for nid, lon, lat, ntype in _nodes():
        ET.SubElement(nod, "node", {
            "id": nid, "x": f"{lon:.6f}", "y": f"{lat:.6f}", "type": ntype,
        })
    ET.ElementTree(nod).write(
        str(HERE / "demo.nod.xml"), encoding="utf-8",
        xml_declaration=True, pretty_print=True,
    )

    edg = ET.Element("edges")
    for eid, frm, to, lanes, speed, priority in _edges():
        ET.SubElement(edg, "edge", {
            "id": eid, "from": frm, "to": to,
            "numLanes": str(lanes), "speed": speed, "priority": str(priority),
        })
    ET.ElementTree(edg).write(
        str(HERE / "demo.edg.xml"), encoding="utf-8",
        xml_declaration=True, pretty_print=True,
    )

    con = ET.Element("connections")
    for attrs in _connections():
        ET.SubElement(con, "connection", attrs)
    ET.ElementTree(con).write(
        str(HERE / "demo.con.xml"), encoding="utf-8",
        xml_declaration=True, pretty_print=True,
    )


def run_netconvert() -> None:
    cmd = [
        "netconvert",
        "--node-files", str(HERE / "demo.nod.xml"),
        "--edge-files", str(HERE / "demo.edg.xml"),
        "--connection-files", str(HERE / "demo.con.xml"),
        "--proj.utm", "true",
        "--tls.default-type", "static",
        "--output-file", str(HERE / "demo.net.xml"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"netconvert failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# TLS alternative program (alt_fixed)
# ---------------------------------------------------------------------------

def write_tls_additional() -> None:
    """Copy each junction's default program 0 into an ``alt_fixed`` program.

    Green phases (state contains G/g, no y) get a uniform 30 s duration;
    yellow / all-red phases keep their netconvert durations. Deriving the
    states from the generated net guarantees valid link-index lengths.
    """
    net_root = ET.parse(str(HERE / "demo.net.xml")).getroot()
    add = ET.Element("additional")
    found: set[str] = set()
    for tl in net_root.findall("tlLogic"):
        tls_id = tl.get("id")
        if tls_id not in TLS_NODES or tl.get("programID") != "0":
            continue
        found.add(tls_id)
        alt = ET.SubElement(add, "tlLogic", {
            "id": tls_id, "type": "static",
            "programID": "alt_fixed", "offset": "0",
        })
        for ph in tl.findall("phase"):
            state = ph.get("state", "")
            is_green = ("y" not in state) and any(c in "Gg" for c in state)
            duration = "30" if is_green else ph.get("duration", "3")
            ET.SubElement(alt, "phase", {"duration": duration, "state": state})
    missing = set(TLS_NODES) - found
    if missing:
        raise RuntimeError(
            f"demo.net.xml has no default TLS program 0 for {sorted(missing)}"
        )
    ET.ElementTree(add).write(
        str(HERE / "demo.tls.xml"), encoding="utf-8",
        xml_declaration=True, pretty_print=True,
    )


# ---------------------------------------------------------------------------
# sumocfg + placeholder routes
# ---------------------------------------------------------------------------

def write_cfg_files() -> None:
    (HERE / "demo.rou.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<routes/>\n', encoding="utf-8"
    )
    cfg = ET.Element("configuration")
    inp = ET.SubElement(cfg, "input")
    ET.SubElement(inp, "net-file", {"value": "demo.net.xml"})
    ET.SubElement(inp, "route-files", {"value": "demo.rou.xml"})
    ET.SubElement(inp, "additional-files", {"value": "demo.tls.xml"})
    time = ET.SubElement(cfg, "time")
    ET.SubElement(time, "begin", {"value": "0"})
    ET.SubElement(time, "step-length", {"value": "0.1"})
    ET.ElementTree(cfg).write(
        str(HERE / "demo.sumocfg"), encoding="utf-8",
        xml_declaration=True, pretty_print=True,
    )


# ---------------------------------------------------------------------------
# TMC payloads — lat/lon read back from the generated net for exact matching
# ---------------------------------------------------------------------------

# Hourly external TMC counts: eastbound-heavy arterial, moderate cross streets.
HOURLY_COUNTS: dict[str, dict[str, dict[str, int]]] = {
    "i1": {
        "East": {"Left": 60, "Straight": 560, "Right": 90},
        "West": {"Left": 50, "Straight": 400, "Right": 80},
        "North": {"Left": 45, "Straight": 120, "Right": 70},
        "South": {"Left": 40, "Straight": 110, "Right": 60},
    },
    "i2": {
        "East": {"Left": 70, "Straight": 590, "Right": 80},
        "West": {"Left": 55, "Straight": 430, "Right": 70},
        "North": {"Left": 50, "Straight": 140, "Right": 80},
        "South": {"Left": 45, "Straight": 130, "Right": 65},
    },
    "i3": {
        "East": {"Left": 55, "Straight": 520, "Right": 100},
        "West": {"Left": 60, "Straight": 380, "Right": 90},
        "North": {"Left": 40, "Straight": 100, "Right": 60},
        "South": {"Left": 50, "Straight": 150, "Right": 75},
    },
}

INTERSECTION_TO_NODE = {"i1": "J1", "i2": "J2", "i3": "J3"}

# Deterministic per-snapshot demand factors (mild AM ramp-up then dip).
SNAPSHOT_FACTORS = (0.85, 0.95, 1.05, 1.15, 1.05, 0.95)


def _node_lonlat(net) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for node_id in TLS_NODES:
        x, y = net.getNode(node_id).getCoord()
        lon, lat = net.convertXY2LonLat(x, y)
        out[node_id] = (round(lon, 6), round(lat, 6))
    return out


def write_tmc_assets(net) -> None:
    lonlat = _node_lonlat(net)

    def intersections(scale: float, snap_index: int | None) -> list[dict]:
        rows = []
        for iid, node_id in INTERSECTION_TO_NODE.items():
            lon, lat = lonlat[node_id]
            counts = {}
            for card, turns in HOURLY_COUNTS[iid].items():
                counts[card] = {}
                for turn, hourly in turns.items():
                    if snap_index is None:
                        counts[card][turn] = hourly
                    else:
                        # 10-minute share with a small deterministic wobble so
                        # the six snapshots are not identical.
                        wobble = 1.0 + 0.03 * (((snap_index + len(turn)) % 3) - 1)
                        counts[card][turn] = max(1, round(hourly / 6.0 * scale * wobble))
            rows.append({
                "intersection_id": iid, "lat": lat, "lon": lon, "counts": counts,
            })
        return rows

    demo_tmc = {
        "corridor_id": "demo_arterial",
        "observation_interval": "1h",
        "timestamp_start": "2026-07-01T08:00:00",
        "intersections": intersections(1.0, None),
    }
    (HERE / "demo_tmc.json").write_text(
        json.dumps(demo_tmc, indent=2) + "\n", encoding="utf-8"
    )

    snap_dir = HERE / "snapshots"
    snap_dir.mkdir(exist_ok=True)
    for i, factor in enumerate(SNAPSHOT_FACTORS):
        payload = {
            "corridor_id": "demo_arterial",
            "observation_interval": "10min",
            "timestamp_start": f"2026-07-01T08:{i * 10:02d}:00",
            "intersections": intersections(factor, i),
        }
        (snap_dir / f"snap_{i + 1:02d}.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Baseline timing sheet + scheduler config
# ---------------------------------------------------------------------------

EW_MOVEMENTS = ["EB-left", "EB-through", "EB-right", "WB-left", "WB-through", "WB-right"]
NS_MOVEMENTS = ["NB-left", "NB-through", "NB-right", "SB-left", "SB-through", "SB-right"]


def write_baseline_sheet() -> None:
    sheet = {
        "schema": "terasim.timing_sheet.v1",
        "corridor_id": "demo_arterial",
        "cycle_s": 90,
        "intersections": [
            {
                "intersection_id": iid,
                "tls_id": tls,
                "offset_s": offset,
                "phases": [
                    {"nema_phases": [2, 6], "movements": EW_MOVEMENTS,
                     "green_s": 48, "yellow_s": 3, "all_red_s": 1},
                    {"nema_phases": [4, 8], "movements": NS_MOVEMENTS,
                     "green_s": 34, "yellow_s": 3, "all_red_s": 1},
                ],
            }
            for iid, tls, offset in (("i1", "J1", 0), ("i2", "J2", 20), ("i3", "J3", 40))
        ],
    }
    (HERE / "baseline_sheet.json").write_text(
        json.dumps(sheet, indent=2) + "\n", encoding="utf-8"
    )


def write_scheduled_config() -> None:
    (HERE / "scheduled_config.yaml").write_text(
        "corridors:\n"
        "  demo_arterial:\n"
        "    base_sumocfg: examples/corridor_demo/demo.sumocfg\n"
        "    corridor:\n"
        "      tls_ids: [J1, J2, J3]\n"
        "    baseline_sheet: examples/corridor_demo/baseline_sheet.json\n"
        "    sim_end: 300\n"
        "    max_trials: 6\n"
        "    n_workers: 2\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------

def sanity_checks(net) -> None:
    # 1. All three junctions carry a TLS with default program '0' in the net.
    net_root = ET.parse(str(HERE / "demo.net.xml")).getroot()
    programs = {
        tl.get("id")
        for tl in net_root.findall("tlLogic")
        if tl.get("programID") == "0"
    }
    missing = set(TLS_NODES) - programs
    if missing:
        raise RuntimeError(f"junctions without TLS program 0: {sorted(missing)}")

    # 2. Every junction has all four incoming approaches.
    from terasim_city.flow.network.matcher import (
        edge_heading_into_node,
        nearest_cardinal,
    )
    for node_id in TLS_NODES:
        node = net.getNode(node_id)
        cards = {
            nearest_cardinal(edge_heading_into_node(e))
            for e in node.getIncoming()
            if not e.getID().startswith(":") and e.allows("passenger")
        }
        if cards != {"EB", "WB", "NB", "SB"}:
            raise RuntimeError(f"{node_id} approaches incomplete: {sorted(cards)}")

    # 3. A short headless SUMO run of the sumocfg completes cleanly.
    sumo_bin = shutil.which("sumo")
    if sumo_bin is None:
        raise RuntimeError("`sumo` binary not on PATH; cannot sanity-check demo.sumocfg")
    result = subprocess.run(
        [sumo_bin, "-c", str(HERE / "demo.sumocfg"), "--no-step-log", "-e", "10"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"sumo dry-run failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )

    # 4. The full TMC pipeline generates at least one flow on this map.
    from terasim_city.api import run_tmc_pipeline
    with tempfile.TemporaryDirectory() as tmp:
        scenario = run_tmc_pipeline(
            HERE / "demo_tmc.json", HERE / "demo.sumocfg", tmp, sim_end=60.0
        )
        if scenario.n_flows <= 0:
            raise RuntimeError("run_tmc_pipeline produced zero flows on the demo map")
        print(
            f"pipeline check ok: n_flows={scenario.n_flows} "
            f"total_vph={scenario.total_vehs_per_hour:.1f}"
        )


def main() -> int:
    write_netconvert_inputs()
    run_netconvert()
    write_tls_additional()
    write_cfg_files()

    import sumolib
    net = sumolib.net.readNet(str(HERE / "demo.net.xml"))
    if not net.hasGeoProj():
        raise RuntimeError("demo.net.xml is not geo-referenced (missing projection)")

    write_tmc_assets(net)
    write_baseline_sheet()
    write_scheduled_config()
    sanity_checks(net)

    print(f"corridor demo assets written to {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
