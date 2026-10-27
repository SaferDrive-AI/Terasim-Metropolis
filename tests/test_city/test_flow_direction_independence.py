"""Flow generation must be direction-agnostic.

Chaining a through movement to the next monitored intersection is a pure
geometry question: E-W corridors, N-S corridors and full grids must all chain,
and a corridor rotated by 90 degrees must produce the exact same flow
structure. These tests build synthetic OSM maps (converted with netconvert via
the map providers) and synthetic TMC payloads from scratch.

Test names deliberately avoid the substrings ``sumo`` and ``simulation`` so
the repo-root conftest does not auto-mark them ``requires_sumo``; netconvert
is checked explicitly.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from terasim_city.flow.sources.corridor_graph import CorridorGraphFlowSource
from terasim_city.maps import OsmMapProvider

requires_netconvert = pytest.mark.skipif(
    shutil.which("netconvert") is None, reason="netconvert not on PATH"
)

# Synthetic geography: ~0.003 deg spacing (~300 m) around a mid-latitude point.
LAT0, LON0 = 37.0000, -122.0000
STEP = 0.003
STUB = 0.002  # cross-street / corridor-end stub length in degrees


# ---------------------------------------------------------------------------
# Synthetic OSM + TMC builders
# ---------------------------------------------------------------------------

def _osm_document(nodes: dict[int, tuple[float, float, bool]],
                  ways: list[tuple[int, list[int], str]]) -> str:
    """Render an OSM XML document.

    ``nodes``: id -> (lat, lon, is_signal). ``ways``: (id, node_ids, highway).
    """
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<osm version="0.6" generator="terasim-test">']
    for nid, (lat, lon, signal) in sorted(nodes.items()):
        if signal:
            out.append(f'  <node id="{nid}" lat="{lat:.6f}" lon="{lon:.6f}" version="1">')
            out.append('    <tag k="highway" v="traffic_signals"/>')
            out.append('  </node>')
        else:
            out.append(f'  <node id="{nid}" lat="{lat:.6f}" lon="{lon:.6f}" version="1"/>')
    for wid, nids, highway in ways:
        out.append(f'  <way id="{wid}" version="1">')
        for nid in nids:
            out.append(f'    <nd ref="{nid}"/>')
        out.append(f'    <tag k="highway" v="{highway}"/>')
        out.append('  </way>')
    out.append('</osm>')
    return "\n".join(out) + "\n"


def _corridor_osm(orientation: str) -> str:
    """A 3-intersection signalized corridor with cross streets.

    ``orientation``: 'EW' (main street runs west→east) or 'NS' (south→north).
    The NS map is the EW map rotated 90 deg CCW around (LAT0, LON0):
    (dlon, dlat) -> (-dlat, dlon) — identical topology, rotated geometry.
    """
    def place(u: float, v: float) -> tuple[float, float]:
        # u = along the main street, v = across it (in degrees offsets).
        if orientation == "EW":
            return (LAT0 + v, LON0 + u)
        return (LAT0 + u, LON0 - v)  # rotate 90 deg CCW

    nodes: dict[int, tuple[float, float, bool]] = {}
    ways: list[tuple[int, list[int], str]] = []

    def add_node(nid: int, u: float, v: float, signal: bool = False) -> int:
        lat, lon = place(u, v)
        nodes[nid] = (lat, lon, signal)
        return nid

    # Main street: end stub — I0 — I1 — I2 — end stub.
    add_node(100, -STUB, 0.0)
    for k in range(3):
        add_node(101 + k, k * STEP, 0.0, signal=True)
    add_node(104, 2 * STEP + STUB, 0.0)
    ways.append((10, [100, 101, 102, 103, 104], "secondary"))

    # One cross street per intersection (two stubs).
    for k in range(3):
        a = add_node(200 + 2 * k, k * STEP, STUB)
        b = add_node(201 + 2 * k, k * STEP, -STUB)
        ways.append((20 + k, [a, 101 + k, b], "residential"))

    return _osm_document(nodes, ways)


def _grid_osm() -> str:
    """A 3x3 signalized grid: 3 N-S streets x 3 E-W streets, stubs all around."""
    nodes: dict[int, tuple[float, float, bool]] = {}
    ways: list[tuple[int, list[int], str]] = []

    def nid(r: int, c: int) -> int:  # grid node ids
        return 100 + 10 * r + c

    for r in range(3):
        for c in range(3):
            nodes[nid(r, c)] = (LAT0 + r * STEP, LON0 + c * STEP, True)

    # E-W streets (one per row) with west/east stubs.
    for r in range(3):
        w = 300 + r
        e = 310 + r
        nodes[w] = (LAT0 + r * STEP, LON0 - STUB, False)
        nodes[e] = (LAT0 + r * STEP, LON0 + 2 * STEP + STUB, False)
        ways.append((30 + r, [w, nid(r, 0), nid(r, 1), nid(r, 2), e], "secondary"))

    # N-S streets (one per column) with south/north stubs.
    for c in range(3):
        s = 400 + c
        n = 410 + c
        nodes[s] = (LAT0 - STUB, LON0 + c * STEP, False)
        nodes[n] = (LAT0 + 2 * STEP + STUB, LON0 + c * STEP, False)
        ways.append((40 + c, [s, nid(0, c), nid(1, c), nid(2, c), n], "secondary"))

    return _osm_document(nodes, ways)


_MAIN_COUNTS = {"Left": 30, "Straight": 300, "Right": 30}
_CROSS_COUNTS = {"Left": 10, "Straight": 60, "Right": 10}


def _rotate_dir(d: str, orientation: str) -> str:
    """Map the EW fixture's direction labels onto the NS (90 deg CCW) fixture."""
    if orientation == "EW":
        return d
    return {
        "East": "North",
        "West": "South",
        "North": "West",
        "South": "East",
    }[d]


def _corridor_tmc(orientation: str) -> dict:
    def place(u: float, v: float) -> tuple[float, float]:
        if orientation == "EW":
            return (LAT0 + v, LON0 + u)
        return (LAT0 + u, LON0 - v)

    intersections = []
    for k in range(3):
        lat, lon = place(k * STEP, 0.0)
        counts = {
            _rotate_dir("East", orientation): dict(_MAIN_COUNTS),
            _rotate_dir("West", orientation): dict(_MAIN_COUNTS),
            _rotate_dir("North", orientation): dict(_CROSS_COUNTS),
            _rotate_dir("South", orientation): dict(_CROSS_COUNTS),
        }
        intersections.append(
            {"intersection_id": f"i{k}", "lat": lat, "lon": lon, "counts": counts}
        )
    return {
        "corridor_id": f"synthetic_{orientation.lower()}",
        "observation_interval": "1h",
        "timestamp_start": "2026-07-01T08:00:00",
        "intersections": intersections,
    }


def _grid_tmc() -> dict:
    intersections = []
    for r in range(3):
        for c in range(3):
            intersections.append({
                "intersection_id": f"g{r}{c}",
                "lat": LAT0 + r * STEP,
                "lon": LON0 + c * STEP,
                "counts": {
                    "East": dict(_MAIN_COUNTS),
                    "West": dict(_MAIN_COUNTS),
                    "North": dict(_MAIN_COUNTS),
                    "South": dict(_MAIN_COUNTS),
                },
            })
    return {
        "corridor_id": "synthetic_grid",
        "observation_interval": "1h",
        "timestamp_start": "2026-07-01T08:00:00",
        "intersections": intersections,
    }


def _generate(tmp_path: Path, name: str, osm_text: str, tmc: dict):
    """Convert OSM → net (via the map provider) and run the OD flow source.

    Returns (rou_path, patterns list, flows list).
    """
    from lxml import etree as ET

    osm = tmp_path / f"{name}.osm"
    osm.write_text(osm_text, encoding="utf-8")
    pkg = OsmMapProvider(osm).build(tmp_path / f"{name}_map")
    assert pkg.metadata["has_tls"], f"{name}: netconvert produced no signals"

    tmc_path = tmp_path / f"{name}_tmc.json"
    tmc_path.write_text(json.dumps(tmc), encoding="utf-8")

    out = tmp_path / name / "routes.rou.xml"
    CorridorGraphFlowSource(tmc_path).generate(pkg.net_file, out, sim_end=600.0)

    patterns = json.loads((out.parent / "patterns.json").read_text(encoding="utf-8"))
    root = ET.parse(str(out)).getroot()
    flows = [
        (f.get("id"), float(f.get("vehsPerHour")))
        for f in root.findall("flow")
    ]
    return out, patterns, flows


def _chain_lengths(patterns: list[dict]) -> list[int]:
    """Number of monitored intersections each stream passed through."""
    return sorted(len(p["decisions"]) for p in patterns)


def _has_chain_in(patterns: list[dict], directions: set[str], min_hops: int = 2) -> bool:
    """True if some stream chained >= min_hops intersections travelling in ``directions``."""
    for p in patterns:
        ds = [d for (_iid, d, turn) in p["decisions"] if turn == "through"]
        if len(p["decisions"]) >= min_hops and ds and set(ds) <= directions:
            return True
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@requires_netconvert
def test_ns_corridor_chains_and_matches_ew_structure(tmp_path):
    """A N-S corridor must chain exactly like the same corridor laid E-W."""
    _, ew_patterns, ew_flows = _generate(
        tmp_path, "ew", _corridor_osm("EW"), _corridor_tmc("EW")
    )
    _, ns_patterns, ns_flows = _generate(
        tmp_path, "ns", _corridor_osm("NS"), _corridor_tmc("NS")
    )

    # The E-W corridor chains along EB/WB; the N-S one must chain along NB/SB.
    assert _has_chain_in(ew_patterns, {"EB"}, min_hops=3)
    assert _has_chain_in(ew_patterns, {"WB"}, min_hops=3)
    assert _has_chain_in(ns_patterns, {"NB"}, min_hops=3)
    assert _has_chain_in(ns_patterns, {"SB"}, min_hops=3)

    # Rotation invariance: identical topology → identical flow structure.
    assert _chain_lengths(ns_patterns) == _chain_lengths(ew_patterns)
    assert len(ns_flows) == len(ew_flows)
    ew_total = sum(v for _, v in ew_flows)
    ns_total = sum(v for _, v in ns_flows)
    assert ns_total == pytest.approx(ew_total, rel=0.01)


@requires_netconvert
def test_grid_chains_both_axes(tmp_path):
    """A 3x3 grid must chain through movements horizontally AND vertically."""
    _, patterns, flows = _generate(tmp_path, "grid", _grid_osm(), _grid_tmc())

    assert flows, "grid produced no flows"
    # Horizontal chains (EB and WB) and vertical chains (NB and SB) must all
    # traverse at least 3 monitored intersections (a full row/column).
    assert _has_chain_in(patterns, {"EB"}, min_hops=3)
    assert _has_chain_in(patterns, {"WB"}, min_hops=3)
    assert _has_chain_in(patterns, {"NB"}, min_hops=3)
    assert _has_chain_in(patterns, {"SB"}, min_hops=3)


@requires_netconvert
def test_corridor_end_through_exits_network(tmp_path):
    """At the corridor's last intersection the through must exit, not wrap."""
    _, patterns, _flows = _generate(
        tmp_path, "ewx", _corridor_osm("EW"), _corridor_tmc("EW")
    )
    # The EB trunk starting at i0 chains i0→i1→i2 and terminates: no stream
    # may visit more monitored intersections than exist.
    assert max(len(p["decisions"]) for p in patterns) <= 3
