"""Unit tests for the OD peel-off flow source (``CorridorGraphFlowSource``).

Two things are proven here:

1. On the clean 3-intersection corridor demo the OD source produces flows and
   the routes are SHORT — well under the edge-level BFS route length — which is
   the whole reason the OD source exists.
2. When two TMC intersections have no drivable path between them, the OD source
   never chains a stream across the gap: every generated route stays entirely
   within one connected component. ``getShortestPath`` returning ``None`` is the
   signal that self-separates disconnected sub-corridors.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_city.conftest import REPO_ROOT, requires_sumolib

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_NET = DEMO_DIR / "demo.net.xml"
DEMO_TMC = DEMO_DIR / "demo_tmc.json"

requires_demo_assets = pytest.mark.skipif(
    not DEMO_NET.exists() or not DEMO_TMC.exists(),
    reason=f"corridor demo assets not present at {DEMO_DIR}",
)

requires_netconvert = pytest.mark.skipif(
    shutil.which("netconvert") is None,
    reason="netconvert not on PATH (needed to build the disconnected fixture net)",
)


# ---------------------------------------------------------------------------
# Clean-corridor route-shortness test
# ---------------------------------------------------------------------------

@requires_sumolib
@requires_demo_assets
def test_od_routes_are_short_on_demo(tmp_path: Path):
    """OD routes stay short: a corridor-length trunk plus a short tail.

    The removed legacy BFS source exploded to hundreds of long routes on this
    same corridor; the absolute bound below is the regression guard.
    """
    from terasim_city.flow.sources.corridor_graph import CorridorGraphFlowSource

    od_out = tmp_path / "od.rou.xml"
    CorridorGraphFlowSource(DEMO_TMC, dump_intermediate=False).generate(
        DEMO_NET, od_out, sim_end=60.0
    )

    od_lens = _route_edge_lengths(od_out)
    assert od_lens, "OD source produced no routes on the demo corridor"
    assert max(od_lens) <= 12


@requires_sumolib
@requires_demo_assets
def test_od_preserves_total_flow_on_demo(tmp_path: Path):
    """The OD peel-off preserves volume: emitted vehs/h == external inflows.

    Self-contained conservation check: every external boundary inflow (the
    counts the boundary stage kept) is scaled to hourly and must reappear in
    the emitted flows, within sub-noise drop tolerance.
    """
    import json

    from lxml import etree as ET

    from terasim_city.flow.descriptors import TMCData
    from terasim_city.flow.sources.corridor_graph import CorridorGraphFlowSource

    od_out = tmp_path / "od.rou.xml"
    CorridorGraphFlowSource(DEMO_TMC, dump_intermediate=True).generate(
        DEMO_NET, od_out, sim_end=60.0
    )

    boundary = json.loads((tmp_path / "boundary.json").read_text(encoding="utf-8"))
    obs_seconds = TMCData.from_json(DEMO_TMC).observation_seconds()
    expected = sum(
        float(count)
        for inter in boundary["intersections"]
        for turns in inter.get("counts", {}).values()
        for count in turns.values()
    ) * 3600.0 / obs_seconds

    root = ET.parse(str(od_out)).getroot()
    emitted = sum(float(f.get("vehsPerHour")) for f in root.findall("flow"))
    assert emitted == pytest.approx(expected, rel=0.02)


@requires_sumolib
@requires_demo_assets
def test_od_flows_use_best_depart_lane(tmp_path: Path):
    """Every OD flow spawns with departLane=best — the regression lock.

    ``best`` places the vehicle on the lane needing the fewest lane changes
    for its WHOLE route. ``allowed`` (random among lanes valid for the next
    edge only) measurably strands turning vehicles on short/congested
    approaches (a dense urban grid at 0.1 s steps: 4 teleports incl. 1
    wrong-lane vs 0 with best); ``free`` is route-blind and worse. If this
    assertion fails, someone changed the spawn-lane policy — re-run the A/B
    before accepting.
    """
    from lxml import etree as ET

    from terasim_city.flow.sources.corridor_graph import CorridorGraphFlowSource

    out = tmp_path / "od.rou.xml"
    CorridorGraphFlowSource(DEMO_TMC, dump_intermediate=False).generate(
        DEMO_NET, out, sim_end=60.0
    )
    root = ET.parse(str(out)).getroot()
    flows = root.findall("flow")
    assert flows
    assert all(f.get("departLane") == "best" for f in flows)


# ---------------------------------------------------------------------------
# Disconnected-corridor self-separation test
# ---------------------------------------------------------------------------

@requires_sumolib
@requires_netconvert
def test_od_never_chains_across_disconnected_intersections(tmp_path: Path):
    """Two TMC intersections with no drivable path between them stay separate.

    We build a net with two 4-way junctions A and B whose arms are dead-end
    stubs — there is NO edge linking A's component to B's. Both are TMC
    cameras. The OD source must never chain a stream from A into B (or vice
    versa): every route's edges belong to a single component.
    """
    import sumolib

    from terasim_city.flow.sources.corridor_graph import CorridorGraphFlowSource

    net_path, tmc_path, a_edges, b_edges = _build_disconnected_fixture(tmp_path)

    net = sumolib.net.readNet(str(net_path))
    # Sanity: the two components really are unreachable from each other.
    a_out = net.getEdge("EBout_A")
    b_in = net.getEdge("EBin_B")
    edges, _cost = net.getShortestPath(a_out, b_in, vClass="passenger")
    assert edges is None, "fixture is not actually disconnected"

    out = tmp_path / "routes.rou.xml"
    CorridorGraphFlowSource(tmc_path, dump_intermediate=True).generate(
        net_path, out, sim_end=60.0
    )

    from lxml import etree as ET

    root = ET.parse(str(out)).getroot()
    routes = root.findall("route")
    assert routes, "expected routes from both disconnected intersections"

    saw_a = saw_b = False
    for route in routes:
        edge_ids = set(route.get("edges").split())
        touches_a = bool(edge_ids & a_edges)
        touches_b = bool(edge_ids & b_edges)
        # No single route may span both components.
        assert not (touches_a and touches_b), (
            f"route {route.get('id')} chained across the disconnected gap: "
            f"{sorted(edge_ids)}"
        )
        saw_a |= touches_a
        saw_b |= touches_b

    # Both intersections must have produced their own local flows.
    assert saw_a and saw_b


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _route_edge_lengths(routes_file: Path) -> list[int]:
    from lxml import etree as ET

    root = ET.parse(str(routes_file)).getroot()
    return [len(r.get("edges").split()) for r in root.findall("route")]


def _build_disconnected_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, set[str], set[str]]:
    """Build a 2-junction disconnected net + TMC payload via netconvert.

    Returns ``(net_path, tmc_path, a_edge_ids, b_edge_ids)`` where the edge-id
    sets partition the two connected components.
    """
    from lxml import etree as ET

    center_lat = 37.3350
    m_per_deg_lat = 110_950.0
    m_per_deg_lon = 88_500.0
    d = 300.0 / m_per_deg_lon  # arm offset in lon
    dlat = 300.0 / m_per_deg_lat
    # A far to the west, B far to the east — well beyond any arm stub, so the
    # two crosses never share a node or edge.
    a_lon = -121.9100
    b_lon = -121.8900

    junctions = {"A": a_lon, "B": b_lon}

    nod = ET.Element("nodes")
    edg = ET.Element("edges")
    con = ET.Element("connections")
    edge_ids: dict[str, set[str]] = {"A": set(), "B": set()}

    def add_node(nid, lon, lat, ntype):
        ET.SubElement(nod, "node", {
            "id": nid, "x": f"{lon:.6f}", "y": f"{lat:.6f}", "type": ntype,
        })

    def add_edge(j, eid, frm, to, lanes):
        ET.SubElement(edg, "edge", {
            "id": eid, "from": frm, "to": to,
            "numLanes": str(lanes), "speed": "13.89", "priority": "3",
        })
        edge_ids[j].add(eid)

    def conn(frm, to, fl, tl):
        ET.SubElement(con, "connection", {
            "from": frm, "to": to, "fromLane": str(fl), "toLane": str(tl),
        })

    for j, lon in junctions.items():
        add_node(j, lon, center_lat, "traffic_light")
        add_node(f"{j}_W", lon - d, center_lat, "priority")
        add_node(f"{j}_E", lon + d, center_lat, "priority")
        add_node(f"{j}_N", lon, center_lat + dlat, "priority")
        add_node(f"{j}_S", lon, center_lat - dlat, "priority")

        # Arterial arms (2 lanes), cross arms (1 lane), both directions.
        add_edge(j, f"EBin_{j}", f"{j}_W", j, 2)
        add_edge(j, f"EBout_{j}", j, f"{j}_E", 2)
        add_edge(j, f"WBin_{j}", f"{j}_E", j, 2)
        add_edge(j, f"WBout_{j}", j, f"{j}_W", 2)
        add_edge(j, f"NBin_{j}", f"{j}_S", j, 1)
        add_edge(j, f"NBout_{j}", j, f"{j}_N", 1)
        add_edge(j, f"SBin_{j}", f"{j}_N", j, 1)
        add_edge(j, f"SBout_{j}", j, f"{j}_S", 1)

        # EB approach: right->S, through->E, left->N.
        conn(f"EBin_{j}", f"SBout_{j}", 0, 0)
        conn(f"EBin_{j}", f"EBout_{j}", 0, 0)
        conn(f"EBin_{j}", f"EBout_{j}", 1, 1)
        conn(f"EBin_{j}", f"NBout_{j}", 1, 0)
        # WB approach: right->N, through->W, left->S.
        conn(f"WBin_{j}", f"NBout_{j}", 0, 0)
        conn(f"WBin_{j}", f"WBout_{j}", 0, 0)
        conn(f"WBin_{j}", f"WBout_{j}", 1, 1)
        conn(f"WBin_{j}", f"SBout_{j}", 1, 0)
        # NB approach: right->E, through->N, left->W.
        conn(f"NBin_{j}", f"EBout_{j}", 0, 0)
        conn(f"NBin_{j}", f"NBout_{j}", 0, 0)
        conn(f"NBin_{j}", f"WBout_{j}", 0, 1)
        # SB approach: right->W, through->S, left->E.
        conn(f"SBin_{j}", f"WBout_{j}", 0, 0)
        conn(f"SBin_{j}", f"SBout_{j}", 0, 0)
        conn(f"SBin_{j}", f"EBout_{j}", 0, 1)

    nod_path = tmp_path / "disc.nod.xml"
    edg_path = tmp_path / "disc.edg.xml"
    con_path = tmp_path / "disc.con.xml"
    net_path = tmp_path / "disc.net.xml"
    ET.ElementTree(nod).write(str(nod_path), encoding="utf-8", xml_declaration=True)
    ET.ElementTree(edg).write(str(edg_path), encoding="utf-8", xml_declaration=True)
    ET.ElementTree(con).write(str(con_path), encoding="utf-8", xml_declaration=True)

    result = subprocess.run(
        [
            "netconvert",
            "--node-files", str(nod_path),
            "--edge-files", str(edg_path),
            "--connection-files", str(con_path),
            "--proj.utm", "true",
            "--tls.default-type", "static",
            "--output-file", str(net_path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"netconvert failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )

    import sumolib

    net = sumolib.net.readNet(str(net_path))
    counts = {
        "East": {"Left": 40, "Straight": 400, "Right": 60},
        "West": {"Left": 40, "Straight": 380, "Right": 60},
        "North": {"Left": 30, "Straight": 90, "Right": 50},
        "South": {"Left": 30, "Straight": 90, "Right": 50},
    }
    intersections = []
    for j in ("A", "B"):
        x, y = net.getNode(j).getCoord()
        lon, lat = net.convertXY2LonLat(x, y)
        intersections.append({
            "intersection_id": j,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "counts": counts,
        })
    tmc = {
        "corridor_id": "disconnected_test",
        "observation_interval": "1h",
        "timestamp_start": "2026-07-01T08:00:00",
        "intersections": intersections,
    }
    tmc_path = tmp_path / "disc_tmc.json"
    tmc_path.write_text(json.dumps(tmc, indent=2), encoding="utf-8")

    return net_path, tmc_path, edge_ids["A"], edge_ids["B"]


@requires_sumolib
@requires_demo_assets
def test_through_chain_follows_the_boundary_verdict(tmp_path: Path):
    """The chain and the spawn suppression must be two sides of ONE verdict.

    Chaining A->B is only legal when the boundary marked B's approach
    corridor-internal (B's own spawn there was suppressed; the chain is what
    delivers those vehicles). If the boundary keeps the approach external, B
    injects its own counted inflow — chaining too would inject the same
    vehicles twice.
    """
    import copy

    import sumolib

    from terasim_city.flow.descriptors import TMCData
    from terasim_city.flow.network.boundary import build_boundary_json
    from terasim_city.flow.network.matcher import map_intersections
    from terasim_city.flow.sources.corridor_graph import build_streams

    net = sumolib.net.readNet(str(DEMO_NET))
    tmc = TMCData.from_json(DEMO_TMC)
    mapped = map_intersections(net, tmc, max_dist_m=50.0)
    boundary = build_boundary_json(net, tmc, mapped, 6, 45.0)
    internal = {
        iid: set(dirs)
        for iid, dirs in boundary["injection_strategy"][
            "removed_internal_directions"
        ].items()
    }
    assert internal, "demo corridor must mark some approaches internal"

    # With the real boundary: chaining happens, and every chained hop lands on
    # an approach the boundary marked internal.
    streams = build_streams(net, boundary, mapped, tmc,
                            exit_hops=2, allow_uturn=True)
    chained = [s for s in streams if len(s.decisions) >= 2]
    assert chained, "demo corridor must chain through its internal approaches"
    for stream in chained:
        for iid, direction, _turn in stream.decisions[1:]:
            assert direction in internal.get(iid, set()), (
                f"stream chained into {iid}/{direction}, which the boundary "
                "kept external — that demand is injected twice"
            )

    # With the verdict erased (every approach external): no chaining at all.
    no_internal = copy.deepcopy(boundary)
    no_internal["injection_strategy"]["removed_internal_directions"] = {}
    streams = build_streams(net, no_internal, mapped, tmc,
                            exit_hops=2, allow_uturn=True)
    assert streams and all(len(s.decisions) == 1 for s in streams), (
        "with no internal approaches every through must exit, not chain"
    )


@requires_sumolib
@requires_netconvert
def test_downstream_camera_turn_volumes_match_its_own_counts(tmp_path: Path):
    """A chained-into camera must not have its turn ratios applied twice.

    Fixture: one straight arterial, C(camera) -- M(signalized, NO camera) -- A(camera),
    300 m apart, demand westbound. M stops the boundary's upstream walk and
    carries no camera, so A's approach is judged EXTERNAL and A spawns its own
    counted inflow — while the purely geometric chain still wants to route C's
    through movement into A. Before the gate, C's volume was ALSO split by A's
    turn ratios, doubling every turning movement A actually observed.
    """
    from lxml import etree as ET

    from terasim_city.flow.descriptors import TMCData
    from terasim_city.flow.network.boundary import build_boundary_json
    from terasim_city.flow.network.matcher import map_intersections
    from terasim_city.flow.sources.corridor_graph import CorridorGraphFlowSource

    net_path, tmc_path = _build_uncovered_midblock_fixture(tmp_path)

    # Precondition that CREATES the divergence: with an uncovered signal in
    # between, the boundary keeps both approaches external (both cameras spawn).
    # Without this the test would pass vacuously.
    import sumolib

    net = sumolib.net.readNet(str(net_path))
    tmc = TMCData.from_json(tmc_path)
    mapped = map_intersections(net, tmc, max_dist_m=50.0)
    boundary = build_boundary_json(net, tmc, mapped, 6, 45.0)
    assert not boundary["injection_strategy"]["removed_internal_directions"], (
        "fixture no longer diverges: the boundary marked an approach internal"
    )

    out = tmp_path / "routes.rou.xml"
    CorridorGraphFlowSource(tmc_path, dump_intermediate=False).generate(
        net_path, out, sim_end=3600.0
    )
    root = ET.parse(str(out)).getroot()
    routes = {r.get("id"): r.get("edges").split() for r in root.findall("route")}
    load: dict[str, float] = {}
    for flow in root.findall("flow"):
        vph = float(flow.get("vehsPerHour"))
        for edge_id in routes.get(flow.get("route"), []):
            load[edge_id] = load.get(edge_id, 0.0) + vph

    # A observed 300 WB-left; WB-left at A exits southbound.
    assert load.get("SBout_A", 0.0) == pytest.approx(300.0, rel=0.02), (
        f"A's left turn carries {load.get('SBout_A', 0.0):.0f} veh/h but A only "
        "observed 300 — the upstream camera's volume was re-split by A's ratios"
    )


def _build_uncovered_midblock_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Straight arterial C -- M(signalized, uncovered) -- A, cameras at C and A.

    Returns ``(net_path, tmc_path)``. Demand is westbound only: C is upstream,
    A downstream. A splits its inflow 300 through / 300 left so a doubled turn
    volume is directly observable.
    """
    from lxml import etree as ET

    lat = 37.3350
    m_per_deg_lat, m_per_deg_lon = 110_950.0, 88_500.0
    arm = 300.0 / m_per_deg_lon
    arm_lat = 300.0 / m_per_deg_lat
    lon = {"A": -121.9000, "M": -121.9000 + arm, "C": -121.9000 + 2 * arm}

    nod, edg, con = ET.Element("nodes"), ET.Element("edges"), ET.Element("connections")

    def node(nid, x, y, ntype):
        ET.SubElement(nod, "node", {"id": nid, "x": f"{x:.6f}",
                                    "y": f"{y:.6f}", "type": ntype})

    def edge(eid, frm, to):
        ET.SubElement(edg, "edge", {"id": eid, "from": frm, "to": to,
                                    "numLanes": "2", "speed": "13.89",
                                    "priority": "10"})

    def conn(frm, to, fl=0, tl=0):
        ET.SubElement(con, "connection", {"from": frm, "to": to,
                                          "fromLane": str(fl), "toLane": str(tl)})

    # Every arterial junction is signalized; only A and C get cameras, so M is
    # a "major junction without a camera" — the divergence trigger.
    for j, x in lon.items():
        node(j, x, lat, "traffic_light")
        node(f"{j}_N", x, lat + arm_lat, "priority")
        node(f"{j}_S", x, lat - arm_lat, "priority")
        edge(f"NBin_{j}", f"{j}_S", j)
        edge(f"NBout_{j}", j, f"{j}_N")
        edge(f"SBin_{j}", f"{j}_N", j)
        edge(f"SBout_{j}", j, f"{j}_S")

    node("W_end", lon["A"] - arm, lat, "priority")
    node("E_end", lon["C"] + arm, lat, "priority")
    edge("EB_W_A", "W_end", "A"); edge("EB_A_M", "A", "M")
    edge("EB_M_C", "M", "C");     edge("EB_C_E", "C", "E_end")
    edge("WB_E_C", "E_end", "C"); edge("WB_C_M", "C", "M")
    edge("WB_M_A", "M", "A");     edge("WB_A_W", "A", "W_end")

    eb_in = {"A": "EB_W_A", "M": "EB_A_M", "C": "EB_M_C"}
    eb_out = {"A": "EB_A_M", "M": "EB_M_C", "C": "EB_C_E"}
    wb_in = {"A": "WB_M_A", "M": "WB_C_M", "C": "WB_E_C"}
    wb_out = {"A": "WB_A_W", "M": "WB_M_A", "C": "WB_C_M"}

    for j in lon:
        conn(eb_in[j], eb_out[j], 0, 0); conn(eb_in[j], eb_out[j], 1, 1)
        conn(eb_in[j], f"NBout_{j}", 1, 0); conn(eb_in[j], f"SBout_{j}", 0, 0)
        conn(wb_in[j], wb_out[j], 0, 0); conn(wb_in[j], wb_out[j], 1, 1)
        conn(wb_in[j], f"SBout_{j}", 1, 0); conn(wb_in[j], f"NBout_{j}", 0, 0)
        conn(f"NBin_{j}", f"NBout_{j}", 0, 0); conn(f"NBin_{j}", wb_out[j], 1, 0)
        conn(f"NBin_{j}", eb_out[j], 0, 0)
        conn(f"SBin_{j}", f"SBout_{j}", 0, 0); conn(f"SBin_{j}", eb_out[j], 1, 0)
        conn(f"SBin_{j}", wb_out[j], 0, 0)

    paths = {}
    for name, tree in (("mid.nod.xml", nod), ("mid.edg.xml", edg), ("mid.con.xml", con)):
        paths[name] = tmp_path / name
        ET.ElementTree(tree).write(str(paths[name]), encoding="utf-8",
                                   xml_declaration=True)
    net_path = tmp_path / "mid.net.xml"
    result = subprocess.run(
        ["netconvert",
         "--node-files", str(paths["mid.nod.xml"]),
         "--edge-files", str(paths["mid.edg.xml"]),
         "--connection-files", str(paths["mid.con.xml"]),
         "--proj.utm", "true", "--tls.default-type", "static",
         "--output-file", str(net_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"netconvert failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )

    import sumolib

    net = sumolib.net.readNet(str(net_path))
    # "West" is the TMC contract name for the WESTBOUND movement (canonical WB).
    counts = {
        "A": {"Left": 300, "Straight": 300, "Right": 0},
        "C": {"Left": 0, "Straight": 600, "Right": 0},
    }
    payload = {
        "corridor_id": "uncovered_midblock", "observation_interval": "1h",
        "timestamp_start": "2026-01-05T08:00:00", "intersections": [],
    }
    for cam in ("A", "C"):
        x, y = net.getNode(cam).getCoord()
        cam_lon, cam_lat = net.convertXY2LonLat(x, y)
        payload["intersections"].append({
            "intersection_id": cam, "lat": cam_lat, "lon": cam_lon,
            "counts": {
                "West": counts[cam],
                "East": {"Left": 0, "Straight": 0, "Right": 0},
                "North": {"Left": 0, "Straight": 0, "Right": 0},
                "South": {"Left": 0, "Straight": 0, "Right": 0},
            },
        })
    tmc_path = tmp_path / "mid_tmc.json"
    tmc_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return net_path, tmc_path
