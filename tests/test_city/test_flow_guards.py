"""Fail-loud guards and chaining-fallback behavior of the OD flow source.

1. An intersection CARRYING DEMAND that fails to match must raise (silent
   zero-traffic scenarios must never reach the optimizer).
2. Zero-count unmatched intersections stay harmless (no raise).
3. If input demand is positive but zero flows come out, generation raises.
4. When the nearest ahead intersection is not drivably reachable (one-way),
   the through stream falls back to the next reachable candidate instead of
   terminating.

Test names deliberately avoid the substrings ``sumo`` and ``simulation`` so
the repo-root conftest does not auto-mark them ``requires_sumo``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from terasim_city.flow.sources.corridor_graph import CorridorGraphFlowSource
from tests.test_city.conftest import REPO_ROOT

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_NET = DEMO_DIR / "demo.net.xml"
DEMO_TMC = DEMO_DIR / "demo_tmc.json"

requires_demo_assets = pytest.mark.skipif(
    not DEMO_NET.exists() or not DEMO_TMC.exists(),
    reason=f"corridor demo assets not present at {DEMO_DIR}",
)
requires_netconvert = pytest.mark.skipif(
    shutil.which("netconvert") is None, reason="netconvert not on PATH"
)

_COUNTS = {"Left": 10, "Straight": 100, "Right": 10}


def _write_tmc(path: Path, intersections: list[dict]) -> Path:
    path.write_text(json.dumps({
        "corridor_id": "guard_test",
        "observation_interval": "1h",
        "timestamp_start": "2026-07-01T08:00:00",
        "intersections": intersections,
    }), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Guard (a): unmatched intersection WITH demand -> ValueError
# ---------------------------------------------------------------------------

@requires_demo_assets
def test_unmatched_intersection_with_demand_raises(tmp_path: Path):
    demo = json.loads(DEMO_TMC.read_text())["intersections"][0]
    tmc = _write_tmc(tmp_path / "tmc.json", [
        demo,  # matches fine
        {  # nowhere near the demo net, but carries real demand
            "intersection_id": "ghost",
            "lat": 0.0, "lon": 0.0,
            "counts": {"East": dict(_COUNTS)},
        },
    ])
    src = CorridorGraphFlowSource(tmc, dump_intermediate=False)
    with pytest.raises(ValueError) as excinfo:
        src.generate(DEMO_NET, tmp_path / "routes.rou.xml", sim_end=60.0)
    message = str(excinfo.value)
    assert "ghost" in message                 # names the intersection
    assert "lat=0.0" in message               # includes coordinates
    assert "silently dropped" in message


@requires_demo_assets
def test_unmatched_intersection_without_demand_is_harmless(tmp_path: Path):
    demo = json.loads(DEMO_TMC.read_text())["intersections"][0]
    tmc = _write_tmc(tmp_path / "tmc.json", [
        demo,
        {  # unmatched but zero counts: metadata only, must not raise
            "intersection_id": "ghost",
            "lat": 0.0, "lon": 0.0,
            "counts": {"East": {"Left": 0, "Straight": 0, "Right": 0}},
        },
    ])
    out = tmp_path / "routes.rou.xml"
    CorridorGraphFlowSource(tmc, dump_intermediate=False).generate(
        DEMO_NET, out, sim_end=60.0
    )
    assert out.exists()


# ---------------------------------------------------------------------------
# Guard (c): positive input demand but zero flows -> ValueError
# ---------------------------------------------------------------------------

@requires_demo_assets
def test_zero_flows_from_positive_demand_raises(tmp_path: Path, monkeypatch):
    import terasim_city.flow.sources.corridor_graph as cg

    # Simulate total downstream loss: the writer emits nothing.
    monkeypatch.setattr(cg, "write_routes_xml", lambda *a, **k: (0, 0.0, {}))
    src = CorridorGraphFlowSource(DEMO_TMC, dump_intermediate=False)
    with pytest.raises(ValueError, match="ZERO"):
        src.generate(DEMO_NET, tmp_path / "routes.rou.xml", sim_end=60.0)


# ---------------------------------------------------------------------------
# Chaining fallback: nearest ahead unreachable (one-way) -> next reachable
# ---------------------------------------------------------------------------

LAT0, LON0 = 37.0000, -122.0000
STEP = 0.003
STUB = 0.002


def _fallback_osm() -> str:
    """Main street I0 → I2 eastward; I1 sits on a physically SEPARATE
    frontage road (its own connected component, like a divided carriageway
    slip network). I1 is the nearest candidate ahead of I0 and has an EB
    approach, but no drivable connector exists from the main line — the
    through stream must fall back to I2 instead of terminating."""
    nodes: dict[int, tuple[float, float, bool]] = {}
    ways: list[tuple[int, list[int], str]] = []

    def node(nid, dlon, dlat, sig=False):
        nodes[nid] = (LAT0 + dlat, LON0 + dlon, sig)
        return nid

    # Main line (one connected component): stub — I0 — I2 — stub.
    node(1, -STUB, 0.0)
    node(10, 0.0, 0.0, sig=True)              # I0
    node(12, 2 * STEP, 0.0, sig=True)         # I2 (reachable, farther)
    node(2, 2 * STEP + STUB, 0.0)
    ways.append((100, [1, 10, 12, 2], "secondary"))
    # Cross arms so I0/I2 are real 4-way signals.
    for k, nid in ((0, 10), (2, 12)):
        a = node(30 + 2 * k, k * STEP, STUB)
        b = node(31 + 2 * k, k * STEP, -STUB)
        ways.append((120 + k, [a, nid, b], "residential"))

    # Frontage-road island (SEPARATE component): a 4-way signal at I1's
    # position, offset ~90 m north of the main line, ZERO links to it.
    node(50, STEP - STUB, 0.0008)
    node(51, STEP, 0.0008, sig=True)          # I1 (nearest ahead, unreachable)
    node(52, STEP + STUB, 0.0008)
    node(53, STEP, 0.0008 + STUB)
    node(54, STEP, 0.0008 - 0.0005)
    ways.append((130, [50, 51, 52], "secondary"))
    ways.append((131, [53, 51, 54], "residential"))

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<osm version="0.6" generator="terasim-test">']
    for nid, (lat, lon, sig) in sorted(nodes.items()):
        if sig:
            out.append(f'  <node id="{nid}" lat="{lat:.6f}" lon="{lon:.6f}" version="1">')
            out.append('    <tag k="highway" v="traffic_signals"/>')
            out.append('  </node>')
        else:
            out.append(f'  <node id="{nid}" lat="{lat:.6f}" lon="{lon:.6f}" version="1"/>')
    for wid, nids, hw in ways:
        out.append(f'  <way id="{wid}" version="1">')
        for nid in nids:
            out.append(f'    <nd ref="{nid}"/>')
        out.append(f'    <tag k="highway" v="{hw}"/>')
        out.append('  </way>')
    out.append('</osm>')
    return "\n".join(out) + "\n"


@requires_netconvert
def test_nearest_unreachable_falls_back_to_next_candidate(tmp_path: Path):
    from terasim_city.maps import OsmMapProvider

    osm = tmp_path / "fallback.osm"
    osm.write_text(_fallback_osm(), encoding="utf-8")
    pkg = OsmMapProvider(osm).build(tmp_path / "map")

    tmc = _write_tmc(tmp_path / "tmc.json", [
        {"intersection_id": "i0", "lat": LAT0, "lon": LON0,
         "counts": {"East": dict(_COUNTS)}},
        {"intersection_id": "i1", "lat": LAT0 + 0.0008, "lon": LON0 + STEP,
         "counts": {"East": dict(_COUNTS)}},
        {"intersection_id": "i2", "lat": LAT0, "lon": LON0 + 2 * STEP,
         "counts": {"East": dict(_COUNTS)}},
    ])
    out = tmp_path / "flow" / "routes.rou.xml"
    CorridorGraphFlowSource(tmc, dump_intermediate=True).generate(
        pkg.net_file, out, sim_end=600.0
    )

    patterns = json.loads((out.parent / "patterns.json").read_text(encoding="utf-8"))
    # The EB through stream from i0 cannot reach i1 (one-way against it) but
    # MUST fall back to i2 instead of terminating: some stream's decision
    # chain visits i0 and later i2, never i1 in between.
    chains = [
        [iid for (iid, _d, _t) in p["decisions"]] for p in patterns
    ]
    assert any(
        c[0] == "i0" and "i2" in c and "i1" not in c for c in chains
    ), f"no i0→i2 fallback chain found; chains: {chains}"
