"""Lanelet2 (``.osm``) map provider: lanelets → SUMO plain XML → netconvert.

A generic, basic converter (site-specific repairs — a pedestrian layer,
per-junction fixes, elevation, signal-plan porting — are deliberately out of
scope):

* **Lanes.** Every drivable lanelet (subtype ``road`` — the Lanelet2 default —
  ``highway``, ``bus_lane``, ``play_street``, ``emergency_lane``) contributes
  one SUMO lane whose shape is its centerline (both bounds resampled to a
  common point count and averaged). Crosswalks, walkways, shoulders and bicycle
  lanes are skipped. ``one_way=no`` lanelets are added in both directions.
* **Junctions.** Lanelets inside an intersection are collapsed into one SUMO
  junction, so the net looks like an OSM import to the corridor pipeline (one
  junction per intersection, approaches as incoming edges, turns as
  connections). A lanelet is *interior* when its centerline crosses another
  lanelet (2-D; grade-separated crossings are skipped when ``ele`` is mapped),
  when it carries Autoware's ``intersection_area`` tag, or when it
  diverges/merges with an interior lanelet; only lanelets up to
  ``INTERIOR_MAX_LENGTH_M`` long qualify, and short stubs touching a junction
  (``STUB_M``) join it. The cross-sections an interior lanelet joins form one
  junction, and every path through interior lanelets becomes a lane-to-lane
  connection; netconvert builds internal lanes, right-of-way and signal
  programs.
* **Edges.** The remaining lanelets are chained across pass-through
  cross-sections (one in, one out, same speed), and chains running side by side
  (shared boundary nodes) between the same two junctions become one multi-lane
  edge; lane 0 is the curb-side lane.
* **Traffic lights.** A junction is signalized when a lanelet in or entering it
  references a ``traffic_light`` regulatory element (Autoware attaches them to
  the intersection lanelets, other maps to the approach); a light on a lanelet
  ending mid-block signalizes that cross-section. netconvert generates static
  programs — the corridor optimizer replaces the timings.
* **Coordinates.** Node ``lat``/``lon`` (preferred) go to netconvert with
  ``--proj.utm``, so the net is geo-referenced like an OSM import. Nodes that
  only carry Autoware ``local_x``/``local_y`` are placed through a ``proj``
  override for that local frame, or through ``map_projector_info.yaml`` next to
  the map (MGRS / LocalCartesianUTM / LocalCartesian).
* **Driving side.** Inferred from shared boundaries: opposite-direction
  neighbours share their LEFT bound under right-hand traffic and their RIGHT
  bound under left-hand traffic; ``--lefthand`` follows.

Not modelled: elevation, pedestrians/bicycles, right_of_way / traffic_sign
regulatory elements (netconvert derives priorities), lane-change restrictions
from line types, signal timings. ``lanelet2_mapping.json`` in the package maps
every SUMO edge lane and junction back to its lanelet ids.
"""

from __future__ import annotations

import json
import logging
import math
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr

from terasim_city.maps.base import MapPackage
from terasim_city.maps.geo import (
    PROJ_OVERRIDE_HINT,
    GeoReferenceError,
    geo_reference_problem,
    local_frame_proj,
    parse_projector_info,
    validate_proj_string,
)
from terasim_city.maps.osm import _run_netconvert, write_base_sumocfg
from terasim_city.maps.summary import (
    conversion_summary,
    ensure_net_loads,
    netconvert_warning_summary,
)
from terasim_city.maps.validate import (
    ensure_valid_map_package,
    map_package_warnings,
    package_metadata,
)

logger = logging.getLogger(__name__)

#: Lanelet subtypes carrying motor traffic (a missing subtype means ``road``).
VEHICLE_SUBTYPES = frozenset({"road", "highway", "bus_lane", "play_street", "emergency_lane"})
#: Speed for lanelets without a parseable ``speed_limit`` tag.
DEFAULT_SPEED_KMH = 50.0
#: Lanelets longer than this are never collapsed into a junction.
INTERIOR_MAX_LENGTH_M = 80.0
#: Lanelets this short that touch a junction are absorbed into it, so no stub
#: edge becomes an approach once netconvert trims edges at the junction shape.
STUB_M = 8.0
#: A lanelet linking two different junctions merges them only up to this length.
JUNCTION_LINK_M = 3.0
#: Crossing centerlines this far apart in elevation are grade-separated.
GRADE_SEPARATION_M = 3.0
#: Autoware's projection file, looked up next to the map.
PROJECTOR_INFO_NAME = "map_projector_info.yaml"
#: Name of the lanelet↔SUMO id mapping written into the package.
MAPPING_FILE_NAME = "lanelet2_mapping.json"
#: netconvert flags (``--lefthand`` is appended for left-hand traffic).
NETCONVERT_LANELET2_FLAGS: tuple[str, ...] = (
    "--proj.utm",
    "--no-turnarounds",
    "--tls.default-type", "static",
)

_EARTH_RADIUS_M = 6_371_008.8
_CROSS_GRID_M = 25.0
_SIMPLIFY_CROSS_M = 0.3
_SIMPLIFY_SHAPE_M = 0.05


# ---------------------------------------------------------------------------
# Format sniffing
# ---------------------------------------------------------------------------

def _file_mentions(path: Path, needles: tuple[bytes, ...]) -> bool:
    longest = max(len(n) for n in needles)
    tail = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 22)
            if not chunk:
                return False
            data = tail + chunk
            if any(n in data for n in needles):
                return True
            tail = data[-longest:]


def is_lanelet2_osm(path: Path | str) -> bool:
    """True iff an OSM XML file holds at least one ``type=lanelet`` relation.

    A byte scan rules out ordinary OpenStreetMap extracts (which never contain
    the value ``lanelet``) cheaply; candidates are then confirmed by parsing.
    """
    path = Path(path)
    if not _file_mentions(path, (b'"lanelet"', b"'lanelet'")):
        return False
    try:
        for _event, elem in ET.iterparse(str(path), events=("end",)):
            if elem.tag == "relation":
                for tag in elem.findall("tag"):
                    if tag.get("k") == "type" and tag.get("v") == "lanelet":
                        return True
                elem.clear()
            elif elem.tag in ("node", "way"):
                elem.clear()
    except ET.ParseError:
        return False
    return False


# ---------------------------------------------------------------------------
# Small geometry helpers (local metric frame)
# ---------------------------------------------------------------------------

Point = tuple[float, float]


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _length(pts: list[Point]) -> float:
    return sum(_dist(a, b) for a, b in zip(pts, pts[1:]))


def _resample(pts: list[tuple], count: int) -> list[tuple]:
    """``count`` points evenly spaced by arc length (coordinates of any dimension)."""
    if len(pts) == 1 or count < 2:
        return [pts[0]] * max(count, 1)
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + _dist(a, b))
    total = cum[-1]
    if total <= 1e-9:
        return [pts[0]] * count
    out = []
    seg = 0
    for i in range(count):
        target = total * i / (count - 1)
        while seg < len(pts) - 2 and cum[seg + 1] < target:
            seg += 1
        span = cum[seg + 1] - cum[seg]
        t = 0.0 if span <= 1e-12 else (target - cum[seg]) / span
        a, b = pts[seg], pts[seg + 1]
        out.append(tuple(a[k] + t * (b[k] - a[k]) for k in range(len(a))))
    return out


def _simplify(pts: list[tuple], tolerance: float) -> list[tuple]:
    """Ramer-Douglas-Peucker on the 2-D part of the points (extra dims carried)."""
    if len(pts) <= 2:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        lo, hi = stack.pop()
        a, b = pts[lo], pts[hi]
        dx, dy = b[0] - a[0], b[1] - a[1]
        norm = math.hypot(dx, dy)
        best, best_i = -1.0, -1
        for i in range(lo + 1, hi):
            p = pts[i]
            if norm <= 1e-12:
                d = _dist(p, a)
            else:
                d = abs(dx * (a[1] - p[1]) - dy * (a[0] - p[0])) / norm
            if d > best:
                best, best_i = d, i
        if best > tolerance:
            keep[best_i] = True
            stack.append((lo, best_i))
            stack.append((best_i, hi))
    return [p for p, k in zip(pts, keep) if k]


def _segment_hit(p1, p2, q1, q2) -> tuple[float, float] | None:
    """Parameters (t, u) where segment p1p2 meets q1q2, or None."""
    rx, ry = p2[0] - p1[0], p2[1] - p1[1]
    sx, sy = q2[0] - q1[0], q2[1] - q1[1]
    denom = rx * sy - ry * sx
    if abs(denom) < 1e-12:
        return None
    qpx, qpy = q1[0] - p1[0], q1[1] - p1[1]
    t = (qpx * sy - qpy * sx) / denom
    u = (qpx * ry - qpy * rx) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return t, u
    return None


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict = {}

    def find(self, item):
        parent = self.parent
        parent.setdefault(item, item)
        root = item
        while parent[root] != root:
            root = parent[root]
        while parent[item] != root:
            parent[item], item = root, parent[item]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _sort_key(lanelet_id: str):
    """Numeric ids sort numerically (deterministic, human-friendly SUMO ids)."""
    base = lanelet_id.split("_", 1)[0]
    return (0, int(base), lanelet_id) if base.lstrip("-").isdigit() else (1, 0, lanelet_id)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass
class _Raw:
    nodes: dict[str, tuple]          # id -> (lat, lon, local_x, local_y, ele)
    ways: dict[str, list[str]]
    lanelets: list[tuple[str, str, str, dict, tuple[str, ...]]]  # id, left, right, tags, regs
    traffic_lights: set[str]
    skipped: Counter = field(default_factory=Counter)


def _float(raw) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse(path: Path) -> _Raw:
    nodes: dict[str, tuple] = {}
    ways: dict[str, list[str]] = {}
    lanelets: list = []
    traffic_lights: set[str] = set()
    skipped: Counter = Counter()
    for _event, elem in ET.iterparse(str(path), events=("end",)):
        tag = elem.tag
        if tag == "node":
            tags = {t.get("k"): t.get("v") for t in elem.findall("tag")}
            nodes[elem.get("id", "")] = (
                _float(elem.get("lat")),
                _float(elem.get("lon")),
                _float(tags.get("local_x")),
                _float(tags.get("local_y")),
                _float(tags.get("ele")),
            )
            elem.clear()
        elif tag == "way":
            ways[elem.get("id", "")] = [nd.get("ref", "") for nd in elem.findall("nd")]
            elem.clear()
        elif tag == "relation":
            tags = {t.get("k"): t.get("v") for t in elem.findall("tag")}
            kind = tags.get("type")
            if kind == "lanelet":
                subtype = tags.get("subtype") or "road"
                if subtype not in VEHICLE_SUBTYPES:
                    skipped[f"subtype={subtype}"] += 1
                elif tags.get("participant:vehicle") == "no":
                    skipped["participant:vehicle=no"] += 1
                else:
                    left = right = None
                    regs = []
                    for member in elem.findall("member"):
                        role = member.get("role")
                        if member.get("type") == "way" and role == "left":
                            left = member.get("ref")
                        elif member.get("type") == "way" and role == "right":
                            right = member.get("ref")
                        elif role == "regulatory_element":
                            regs.append(member.get("ref", ""))
                    if left is None or right is None:
                        skipped["missing left/right bound"] += 1
                    else:
                        lanelets.append((elem.get("id", ""), left, right, tags, tuple(regs)))
            elif kind == "regulatory_element" and tags.get("subtype") == "traffic_light":
                traffic_lights.add(elem.get("id", ""))
            elem.clear()
    return _Raw(nodes, ways, lanelets, traffic_lights, skipped)


def _speed_ms(tags: dict) -> float:
    raw = (tags.get("speed_limit") or "").strip().lower()
    number = ""
    for ch in raw:
        if ch.isdigit() or ch == ".":
            number += ch
        elif number:
            break
    try:
        value = float(number)
    except ValueError:
        value = DEFAULT_SPEED_KMH
    if value <= 0:
        value = DEFAULT_SPEED_KMH
    if "mph" in raw:
        value *= 1.609344
    return value / 3.6


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------

def _valid_latlon(entry: tuple) -> bool:
    lat, lon = entry[0], entry[1]
    return (
        lat is not None and lon is not None and -90.0 <= lat <= 90.0
        and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0)
    )


def _resolve_lonlat(
    raw: _Raw, used: set[str], source: Path, proj: str | None
) -> tuple[dict[str, tuple[float, float]], str]:
    """lon/lat of every used node, and a label for where they came from."""
    entries = {nid: raw.nodes[nid] for nid in used}
    if proj is None and all(_valid_latlon(e) for e in entries.values()):
        return {nid: (e[1], e[0]) for nid, e in entries.items()}, "lat/lon"

    if proj is not None:
        frame, label = proj, "local_x/local_y + proj override"
    else:
        info_path = source.with_name(PROJECTOR_INFO_NAME)
        if not info_path.exists():
            raise GeoReferenceError(
                f"{source}: the lanelet nodes carry no usable lat/lon and there is no "
                f"{PROJECTOR_INFO_NAME} next to the map, so the converted net could not be "
                f"matched to lat/lon. Fix: add lat/lon to the nodes, place Autoware's "
                f"{PROJECTOR_INFO_NAME} (MGRS / LocalCartesianUTM / LocalCartesian) next to the "
                f"map, or {PROJ_OVERRIDE_HINT} (here: the frame of the local_x/local_y tags)"
            )
        try:
            frame = local_frame_proj(parse_projector_info(info_path))
        except ValueError as exc:
            raise GeoReferenceError(f"{info_path}: {exc}. Fix: {PROJ_OVERRIDE_HINT}") from exc
        label = f"local_x/local_y + {PROJECTOR_INFO_NAME}"

    missing = [nid for nid, e in entries.items() if e[2] is None or e[3] is None]
    if missing:
        why = (
            "a projection override places the nodes by their local_x/local_y tags"
            if proj is not None else "the nodes carry no usable lat/lon"
        )
        raise GeoReferenceError(
            f"{source}: {why}, but {len(missing)} lanelet node(s) (e.g. {missing[0]}) have no "
            "local_x/local_y tags, so they cannot be placed"
        )
    from pyproj import CRS, Transformer

    transformer = Transformer.from_crs(CRS.from_proj4(frame), CRS.from_epsg(4326), always_xy=True)
    ids = list(entries)
    lons, lats = transformer.transform([entries[i][2] for i in ids], [entries[i][3] for i in ids])
    out = {}
    for nid, lon, lat in zip(ids, lons, lats):
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise GeoReferenceError(f"{source}: node {nid} could not be projected with {frame!r}")
        out[nid] = (float(lon), float(lat))
    return out, label


class _LocalFrame:
    """Equirectangular metres around the map centre (exactly invertible)."""

    def __init__(self, lonlat: dict[str, tuple[float, float]]) -> None:
        lons = [v[0] for v in lonlat.values()]
        lats = [v[1] for v in lonlat.values()]
        self.lon0 = (min(lons) + max(lons)) / 2.0
        self.lat0 = (min(lats) + max(lats)) / 2.0
        self.ky = math.radians(1.0) * _EARTH_RADIUS_M
        self.kx = self.ky * math.cos(math.radians(self.lat0))

    def forward(self, lon: float, lat: float) -> Point:
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        return (x / self.kx + self.lon0, y / self.ky + self.lat0)


# ---------------------------------------------------------------------------
# Lanes (oriented lanelets with geometry)
# ---------------------------------------------------------------------------

@dataclass
class _Lane:
    id: str
    left: list[str]
    right: list[str]
    left_xy: list[Point]
    right_xy: list[Point]
    center: list[tuple]           # (x, y) or (x, y, z)
    width: float
    length: float
    speed: float
    tags: dict
    regs: tuple[str, ...]

    @property
    def start(self) -> tuple[str, str]:
        return (self.left[0], self.right[0])

    @property
    def end(self) -> tuple[str, str]:
        return (self.left[-1], self.right[-1])


def _make_lane(
    lane_id: str,
    left: list[str],
    right: list[str],
    xy: dict[str, Point],
    ele: dict[str, float | None],
    tags: dict,
    regs: tuple[str, ...],
) -> _Lane | None:
    lp = [xy[n] for n in left]
    rp = [xy[n] for n in right]
    if _dist(lp[0], rp[0]) + _dist(lp[-1], rp[-1]) > _dist(lp[0], rp[-1]) + _dist(lp[-1], rp[0]):
        right, rp = right[::-1], rp[::-1]
    count = max(len(lp), len(rp), 2)
    with_z = all(ele.get(n) is not None for n in (*left, *right))
    l3 = [(p[0], p[1], ele[n]) for p, n in zip(lp, left)] if with_z else lp
    r3 = [(p[0], p[1], ele[n]) for p, n in zip(rp, right)] if with_z else rp
    ls, rs = _resample(l3, count), _resample(r3, count)
    center = [tuple((a[k] + b[k]) / 2.0 for k in range(len(a))) for a, b in zip(ls, rs)]
    # Driving direction: the LEFT bound must lie on the left of the travel direction.
    side = 0.0
    for i in range(count - 1):
        dx, dy = center[i + 1][0] - center[i][0], center[i + 1][1] - center[i][1]
        side += dx * (ls[i][1] - rs[i][1]) - dy * (ls[i][0] - rs[i][0])
    if side < 0:
        left, right, lp, rp = left[::-1], right[::-1], lp[::-1], rp[::-1]
        ls, rs, center = ls[::-1], rs[::-1], center[::-1]
    length = _length([(c[0], c[1]) for c in center])
    if length < 0.1:
        return None
    widths = sorted(_dist(a, b) for a, b in zip(ls, rs))
    width = min(max(widths[len(widths) // 2], 2.0), 6.0)
    return _Lane(lane_id, left, right, lp, rp, center, width, length, _speed_ms(tags), tags, regs)


# ---------------------------------------------------------------------------
# The converter
# ---------------------------------------------------------------------------

@dataclass
class _Edge:
    id: str
    from_node: str
    to_node: str
    lanes: list[list[_Lane]]          # curb side first; each a chain of lanelets
    shape: list[Point]


@dataclass
class Lanelet2Conversion:
    """What :func:`convert_lanelet2_to_plain` produced (plain XML + statistics)."""

    nod_file: Path
    edg_file: Path
    con_file: Path
    lefthand: bool
    stats: dict
    mapping: dict
    warnings: list[str]


def convert_lanelet2_to_plain(
    osm_file: Path | str, work_dir: Path | str, *, proj: str | None = None
) -> Lanelet2Conversion:
    """Translate a Lanelet2 map into SUMO plain XML (nodes/edges/connections) in ``work_dir``.

    Node and shape coordinates are written as lon,lat for netconvert's
    ``--proj.utm``.

    Raises:
        GeoReferenceError: The nodes cannot be placed on the globe.
        ValueError: The map holds no usable drivable lanelet.
    """
    osm_file = Path(osm_file)
    raw = _parse(osm_file)
    warnings: list[str] = []
    skipped = Counter(raw.skipped)

    candidates = _usable_lanelets(raw, skipped, osm_file)
    used_nodes = {n for c in candidates for n in (*c[1], *c[2])}
    lonlat, coordinate_source = _resolve_lonlat(raw, used_nodes, osm_file, proj)
    frame = _LocalFrame(lonlat)
    xy = {n: frame.forward(*lonlat[n]) for n in used_nodes}
    ele = {n: raw.nodes[n][4] for n in used_nodes}
    lefthand, votes = _driving_side(candidates, warnings)

    topo = _Topology(_build_lanes(candidates, xy, ele, skipped))
    interior, cluster_uf = _junction_lanelets(topo)
    cluster_keys = topo.keys_of(interior)
    signal_lanes = [lane for lane in topo.lanes if set(lane.regs) & raw.traffic_lights]
    # A light on a lanelet ending mid-block keeps that cross-section a node.
    tl_sections = {
        lane.end for lane in signal_lanes
        if lane.id not in interior and lane.end not in cluster_keys
    }
    chains = _chain_outer_lanes(topo, interior, cluster_keys | tl_sections)
    nodes = _Nodes(chains, cluster_uf, cluster_keys, interior, topo)
    chains = nodes.split_self_loops(chains, cluster_keys, warnings)
    edges = _group_edges(chains, nodes.node_of, lefthand)
    connections = _connections(edges, topo, interior)

    tls_nodes = {
        nodes.node_of[key]
        for lane in signal_lanes
        for key in [lane.start if lane.id in interior else lane.end]
        if key in nodes.node_of
    }
    tls_nodes &= {e.from_node for e in edges} | {e.to_node for e in edges}
    if raw.traffic_lights and not tls_nodes:
        warnings.append(
            f"{len(raw.traffic_lights)} traffic_light regulatory element(s) found but none is "
            "referenced by a drivable lanelet, so no junction was signalized"
        )
    nod_file, edg_file, con_file = _write_plain_xml(
        Path(work_dir), frame, xy, nodes.node_keys, edges, connections, tls_nodes
    )
    stats = {
        "lanelets": len(raw.lanelets) + sum(raw.skipped.values()),
        "lanelets_used": len(topo.lanes),
        "lanelets_skipped": dict(skipped),
        "junction_lanelets": len(interior),
        "junction_clusters": len(nodes.cluster_lanes),
        "signal_regulatory_elements": len(raw.traffic_lights),
        "signalized_nodes": len(tls_nodes),
        "driving_side": "left" if lefthand else "right",
        "driving_side_evidence": {
            "shared_left_bounds": votes[0], "shared_right_bounds": votes[1],
        },
        "coordinate_source": coordinate_source,
    }
    mapping = {
        "edges": {
            edge.id: [[lane.id for lane in chain] for chain in edge.lanes] for edge in edges
        },
        "junctions": nodes.cluster_lanes,
    }
    return Lanelet2Conversion(nod_file, edg_file, con_file, lefthand, stats, mapping, warnings)


def _usable_lanelets(raw: _Raw, skipped: Counter, source: Path) -> list[tuple]:
    """(id, left node ids, right node ids, tags, regs, left way, right way) per lanelet."""
    candidates = []
    for lanelet_id, left_way, right_way, tags, regs in raw.lanelets:
        left = [n for n in raw.ways.get(left_way, ()) if n in raw.nodes]
        right = [n for n in raw.ways.get(right_way, ()) if n in raw.nodes]
        if len(left) < 2 or len(right) < 2:
            skipped["bound way missing or shorter than 2 nodes"] += 1
            continue
        candidates.append((lanelet_id, left, right, tags, regs, left_way, right_way))
    if not candidates:
        raise ValueError(
            f"{source}: no drivable lanelet found (looked for type=lanelet relations with "
            f"subtype in {sorted(VEHICLE_SUBTYPES)} and two bound ways)"
        )
    return candidates


def _driving_side(candidates: list[tuple], warnings: list[str]) -> tuple[bool, tuple[int, int]]:
    """Left-hand traffic?, from bounds shared by opposite-direction neighbours.

    Under right-hand traffic opposite lanes share their LEFT bound; under
    left-hand traffic their RIGHT bound. Returns the verdict and the votes
    (shared left bounds, shared right bounds).
    """
    left_users: Counter = Counter(c[5] for c in candidates)
    right_users: Counter = Counter(c[6] for c in candidates)
    rht_votes = sum(1 for n in left_users.values() if n >= 2)
    lht_votes = sum(1 for n in right_users.values() if n >= 2)
    if rht_votes == lht_votes == 0:
        warnings.append(
            "driving side could not be inferred (no opposite-direction lanelets share a "
            "boundary); right-hand traffic assumed"
        )
    return lht_votes > rht_votes, (rht_votes, lht_votes)


def _build_lanes(
    candidates: list[tuple], xy: dict, ele: dict, skipped: Counter
) -> list[_Lane]:
    """Oriented lanes with geometry; ``one_way=no`` lanelets also get their reverse twin."""
    lanes: list[_Lane] = []
    for lanelet_id, left, right, tags, regs, _left_way, _right_way in candidates:
        lane = _make_lane(lanelet_id, left, right, xy, ele, tags, regs)
        if lane is None:
            skipped["degenerate geometry"] += 1
            continue
        lanes.append(lane)
        if (tags.get("one_way") or "yes").strip().lower() == "no":
            twin = _make_lane(
                f"{lanelet_id}_rev", lane.right[::-1], lane.left[::-1], xy, ele, tags, regs
            )
            if twin is not None:
                lanes.append(twin)
    lanes.sort(key=lambda lane: _sort_key(lane.id))
    return lanes


class _Topology:
    """Lanes indexed by their start/end cross-sections (the Lanelet2 succession rule)."""

    def __init__(self, lanes: list[_Lane]) -> None:
        self.lanes = lanes
        self.by_id = {lane.id: lane for lane in lanes}
        self.starts: dict[tuple, list[_Lane]] = defaultdict(list)
        self.ends: dict[tuple, list[_Lane]] = defaultdict(list)
        for lane in lanes:
            self.starts[lane.start].append(lane)
            self.ends[lane.end].append(lane)

    def successors(self, lane: _Lane) -> list[_Lane]:
        return [s for s in self.starts.get(lane.end, ()) if s.id != lane.id]

    def keys_of(self, lane_ids) -> set[tuple]:
        lanes = [self.by_id[lane_id] for lane_id in lane_ids]
        return {key for lane in lanes for key in (lane.start, lane.end)}


def _junction_lanelets(topo: _Topology) -> tuple[set[str], _UnionFind]:
    """Interior lanelets and the union-find grouping their cross-sections into junctions."""
    by_id = topo.by_id
    interior, crossings = _find_interior(topo.lanes, topo.starts, topo.ends)
    cluster_uf = _UnionFind()
    for lane_id in interior:
        cluster_uf.union(by_id[lane_id].start, by_id[lane_id].end)
    for id_a, id_b in crossings:  # conflicting movements belong to one junction
        if id_a in interior and id_b in interior:
            cluster_uf.union(by_id[id_a].start, by_id[id_b].start)
    by_area: dict[str, str] = {}
    for lane_id in sorted(interior):  # Autoware: one intersection_area id, one junction
        area = (by_id[lane_id].tags.get("intersection_area") or "").strip()
        if area:
            first = by_area.setdefault(area, lane_id)
            cluster_uf.union(by_id[first].start, by_id[lane_id].start)

    def absorb_inside() -> None:
        """Lanelets running from a junction back into the same junction lie inside it."""
        changed = True
        while changed:
            changed = False
            keys = topo.keys_of(interior)
            for lane in topo.lanes:
                if (
                    lane.id not in interior and lane.start in keys and lane.end in keys
                    and cluster_uf.find(lane.start) == cluster_uf.find(lane.end)
                ):
                    interior.add(lane.id)
                    changed = True

    absorb_inside()
    # Stub lanelets touching a junction (typically the few metres between a
    # crosswalk and the stop line) join it, so no stub becomes an approach edge
    # once netconvert trims edges at the junction shape. One pass, no cascade;
    # a link between two different junctions merges them only when very short.
    keys = topo.keys_of(interior)
    for lane in topo.lanes:
        if lane.id in interior or lane.length > STUB_M:
            continue
        start_in, end_in = lane.start in keys, lane.end in keys
        if start_in and end_in and lane.length > JUNCTION_LINK_M:
            continue
        if start_in or end_in:
            interior.add(lane.id)
            cluster_uf.union(lane.start, lane.end)
    absorb_inside()
    return interior, cluster_uf


def _chain_outer_lanes(
    topo: _Topology, interior: set[str], stop_keys: set[tuple]
) -> list[list[_Lane]]:
    """Chain non-interior lanes across pass-through cross-sections (1 in, 1 out, same speed).

    Cross-sections in ``stop_keys`` (junctions, signal stop lines) always end a chain.
    """
    outer = [lane for lane in topo.lanes if lane.id not in interior]
    out_starts: dict[tuple, list[_Lane]] = defaultdict(list)
    out_ends: dict[tuple, list[_Lane]] = defaultdict(list)
    for lane in outer:
        out_starts[lane.start].append(lane)
        out_ends[lane.end].append(lane)
    nxt: dict[str, _Lane] = {}
    for lane in outer:
        key = lane.end
        if key in stop_keys or len(out_ends[key]) != 1 or len(out_starts[key]) != 1:
            continue
        follower = out_starts[key][0]
        if follower.id != lane.id and abs(follower.speed - lane.speed) < 0.3:
            nxt[lane.id] = follower
    has_prev = {f.id for f in nxt.values()}
    chains: list[list[_Lane]] = []
    seen: set[str] = set()

    def follow(first: _Lane) -> list[_Lane]:
        chain = [first]
        seen.add(first.id)
        while chain[-1].id in nxt and nxt[chain[-1].id].id not in seen:
            chain.append(nxt[chain[-1].id])
            seen.add(chain[-1].id)
        return chain

    for lane in outer:
        if lane.id not in has_prev and lane.id not in seen:
            chains.append(follow(lane))
    for lane in outer:  # closed loops of pass-through lanelets: split in two
        if lane.id not in seen:
            loop = follow(lane)
            half = max(1, len(loop) // 2)
            chains.extend(part for part in (loop[:half], loop[half:]) if part)
    return chains


class _Nodes:
    """SUMO nodes: one per junction cluster (``J<n>``) and per cross-section line (``N<n>``).

    A cross-section line is the union of side-by-side lane cross-sections
    (one's right bound node is the other's left bound node), so parallel lanes
    start and end at shared nodes and can be grouped into one edge.
    """

    def __init__(
        self,
        chains: list[list[_Lane]],
        cluster_uf: _UnionFind,
        cluster_keys: set[tuple],
        interior: set[str],
        topo: _Topology,
    ) -> None:
        self.node_of: dict[tuple, str] = {}
        self.node_keys: dict[str, set] = {}
        members: dict = defaultdict(set)
        for key in cluster_keys:
            members[cluster_uf.find(key)].add(key)
        lanes_of: dict = defaultdict(list)
        for lane_id in interior:
            lanes_of[cluster_uf.find(topo.by_id[lane_id].start)].append(lane_id)
        ordered = sorted(members, key=lambda root: min(_sort_key(i) for i in lanes_of[root]))
        #: ``J<n>`` -> its interior lanelet ids (written to the mapping file).
        self.cluster_lanes = {
            f"J{index}": sorted(lanes_of[root], key=_sort_key)
            for index, root in enumerate(ordered)
        }
        for index, root in enumerate(ordered):
            self._assign(f"J{index}", members[root])

        section_keys = {k for chain in chains for k in (chain[0].start, chain[-1].end)}
        plain_keys = sorted(section_keys - cluster_keys)
        line_uf = _UnionFind()
        by_left_node: dict[str, list[tuple]] = defaultdict(list)
        for key in plain_keys:
            line_uf.find(key)
            by_left_node[key[0]].append(key)
        for key in plain_keys:
            for neighbour in by_left_node.get(key[1], ()):
                line_uf.union(key, neighbour)
        lines: dict = defaultdict(list)
        for key in plain_keys:
            lines[line_uf.find(key)].append(key)
        for index, root in enumerate(sorted(lines, key=lambda r: min(lines[r]))):
            self._assign(f"N{index}", lines[root])

    def _assign(self, node_id: str, keys) -> None:
        self.node_keys[node_id] = set(keys)
        for key in keys:
            self.node_of[key] = node_id

    def _own_node(self, key: tuple) -> None:
        old = self.node_of.get(key)
        if old is not None:
            self.node_keys[old].discard(key)
        self._assign(f"N{len(self.node_keys)}", [key])

    def split_self_loops(
        self, chains: list[list[_Lane]], cluster_keys: set[tuple], warnings: list[str]
    ) -> list[list[_Lane]]:
        """A SUMO edge may not start and end at the same node.

        Such a chain is split in the middle (the split cross-section becomes a
        node of its own), a lone lanelet's end gets its own node; only what
        cannot be split is dropped (with a warning).
        """
        fixed: list[list[_Lane]] = []
        dropped = 0
        for chain in chains:
            if self.node_of[chain[0].start] != self.node_of[chain[-1].end]:
                fixed.append(chain)
            elif len(chain) >= 2:
                half = len(chain) // 2
                self._own_node(chain[half].start)
                fixed.extend((chain[:half], chain[half:]))
            elif chain[-1].end not in cluster_keys and chain[-1].end != chain[0].start:
                self._own_node(chain[-1].end)
                fixed.append(chain)
            else:
                dropped += 1
        if dropped:
            warnings.append(
                f"{dropped} lanelet(s) starting and ending at the same junction dropped"
            )
        self.node_keys = {nid: keys for nid, keys in self.node_keys.items() if keys}
        return fixed


def _connections(
    edges: list[_Edge], topo: _Topology, interior: set[str]
) -> set[tuple[str, int, str, int]]:
    """(from edge, from lane, to edge, to lane) for every lanelet succession, through junctions."""
    lane_of: dict[str, tuple[str, int]] = {}
    for edge in edges:
        for index, chain in enumerate(edge.lanes):
            for lane in chain:
                lane_of[lane.id] = (edge.id, index)
    connections: set[tuple[str, int, str, int]] = set()
    for edge in edges:
        for index, chain in enumerate(edge.lanes):
            for target in _exits(chain[-1], topo.successors, interior):
                if target.id in lane_of:
                    connections.add((edge.id, index, *lane_of[target.id]))
    return connections


def _write_plain_xml(
    work_dir: Path,
    frame: _LocalFrame,
    xy: dict,
    node_keys: dict[str, set],
    edges: list[_Edge],
    connections: set[tuple[str, int, str, int]],
    tls_nodes: set[str],
) -> tuple[Path, Path, Path]:
    """Write ``lanelet2.{nod,edg,con}.xml`` (lon,lat coordinates) into ``work_dir``."""
    work_dir.mkdir(parents=True, exist_ok=True)
    nod_file = work_dir / "lanelet2.nod.xml"
    edg_file = work_dir / "lanelet2.edg.xml"
    con_file = work_dir / "lanelet2.con.xml"

    def lonlat_str(p: Point) -> str:
        lon, lat = frame.inverse(p[0], p[1])
        return f"{lon:.9f},{lat:.9f}"

    used = {e.from_node for e in edges} | {e.to_node for e in edges}
    with open(nod_file, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<nodes>\n')
        for node_id in sorted(used, key=lambda n: (n[0], int(n[1:]))):
            mids = [
                ((xy[k[0]][0] + xy[k[1]][0]) / 2.0, (xy[k[0]][1] + xy[k[1]][1]) / 2.0)
                for k in node_keys[node_id]
            ]
            centre = (sum(m[0] for m in mids) / len(mids), sum(m[1] for m in mids) / len(mids))
            lon, lat = lonlat_str(centre).split(",")
            kind = ' type="traffic_light"' if node_id in tls_nodes else ""
            fh.write(f'  <node id="{node_id}" x="{lon}" y="{lat}"{kind}/>\n')
        fh.write("</nodes>\n")
    with open(edg_file, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<edges>\n')
        for edge in edges:
            speed = max(chain[0].speed for chain in edge.lanes)
            shape = " ".join(lonlat_str(p) for p in edge.shape)
            fh.write(
                f'  <edge id={quoteattr(edge.id)} from="{edge.from_node}" to="{edge.to_node}" '
                f'numLanes="{len(edge.lanes)}" speed="{speed:.2f}" spreadType="center" '
                f'shape="{shape}">\n'
            )
            for index, chain in enumerate(edge.lanes):
                widths = sorted(lane.width for lane in chain)
                fh.write(
                    f'    <lane index="{index}" width="{widths[len(widths) // 2]:.2f}" '
                    f'speed="{chain[0].speed:.2f}"/>\n'
                )
            fh.write("  </edge>\n")
        fh.write("</edges>\n")
    with_successor = {c[0] for c in connections}
    with open(con_file, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<connections>\n')
        for from_edge, from_lane, to_edge, to_lane in sorted(connections):
            fh.write(
                f"  <connection from={quoteattr(from_edge)} to={quoteattr(to_edge)} "
                f'fromLane="{from_lane}" toLane="{to_lane}"/>\n'
            )
        for edge in edges:  # explicit dead ends: netconvert must not invent turns
            if edge.id not in with_successor:
                fh.write(f"  <connection from={quoteattr(edge.id)}/>\n")
        fh.write("</connections>\n")
    return nod_file, edg_file, con_file


def _find_interior(
    lanes: list[_Lane], starts: dict, ends: dict
) -> tuple[set[str], set[tuple[str, str]]]:
    """Ids of the lanelets inside a junction, and the crossing lanelet pairs.

    See the module docstring for the rules; crossing pairs are returned so the
    caller can put conflicting movements into the same junction.
    """
    short = [lane for lane in lanes if lane.length <= INTERIOR_MAX_LENGTH_M]
    # Autoware marks intersection lanelets explicitly. (Its ``turn_direction``
    # tag is NOT such a marker: it also sits on the turn-pocket lanes leading
    # up to the stop line, and seeding on it chains neighbouring junctions.)
    interior = {
        lane.id for lane in short
        if (lane.tags.get("intersection_area") or "").strip()
    }
    # 2-D crossings between lanelets that do not share a cross-section.
    simplified = {
        lane.id: _simplify(lane.center, _SIMPLIFY_CROSS_M) for lane in short
    }
    grid: dict[tuple[int, int], list[tuple[str, int]]] = defaultdict(list)
    for lane_id, pts in simplified.items():
        for s in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[s][:2], pts[s + 1][:2]
            gx0, gx1 = (math.floor(v / _CROSS_GRID_M) for v in (min(x1, x2), max(x1, x2)))
            gy0, gy1 = (math.floor(v / _CROSS_GRID_M) for v in (min(y1, y2), max(y1, y2)))
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    grid[(gx, gy)].append((lane_id, s))
    by_id = {lane.id: lane for lane in lanes}
    checked: set[tuple[str, str]] = set()
    crossings: set[tuple[str, str]] = set()
    for items in grid.values():
        for a in range(len(items)):
            id_a, seg_a = items[a]
            for b in range(a + 1, len(items)):
                id_b, seg_b = items[b]
                if id_a == id_b:
                    continue
                pair = (id_a, id_b) if id_a < id_b else (id_b, id_a)
                if pair in checked:
                    continue
                lane_a, lane_b = by_id[id_a], by_id[id_b]
                if {lane_a.start, lane_a.end} & {lane_b.start, lane_b.end}:
                    checked.add(pair)
                    continue
                pa, pb = simplified[id_a], simplified[id_b]
                hit = _segment_hit(pa[seg_a], pa[seg_a + 1], pb[seg_b], pb[seg_b + 1])
                if hit is None:
                    continue
                checked.add(pair)
                if len(pa[0]) == 3 and len(pb[0]) == 3:
                    t, u = hit
                    za = pa[seg_a][2] + t * (pa[seg_a + 1][2] - pa[seg_a][2])
                    zb = pb[seg_b][2] + u * (pb[seg_b + 1][2] - pb[seg_b][2])
                    if abs(za - zb) > GRADE_SEPARATION_M:
                        continue
                interior.add(id_a)
                interior.add(id_b)
                crossings.add(pair)
    # Diverge/merge siblings of interior lanelets are interior too.
    short_ids = {lane.id for lane in short}
    frontier = list(interior)
    while frontier:
        lane = by_id[frontier.pop()]
        for sibling in (*starts.get(lane.start, ()), *ends.get(lane.end, ())):
            if sibling.id not in interior and sibling.id in short_ids:
                interior.add(sibling.id)
                frontier.append(sibling.id)
    return interior, crossings


def _exits(last: _Lane, successors, interior: set[str]) -> list[_Lane]:
    """Non-interior lanelets reached from ``last`` directly or through a junction."""
    found: dict[str, _Lane] = {}
    stack = list(successors(last))
    visited: set[str] = set()
    while stack:
        lane = stack.pop()
        if lane.id in visited:
            continue
        visited.add(lane.id)
        if lane.id in interior:
            if len(visited) <= 200:
                stack.extend(successors(lane))
        else:
            found[lane.id] = lane
    return [found[k] for k in sorted(found, key=_sort_key)]


def _chain_bound(chain: list[_Lane], side: str) -> list[Point]:
    pts: list[Point] = []
    for lane in chain:
        bound = lane.left_xy if side == "left" else lane.right_xy
        pts.extend(bound if not pts else bound[1:])
    return pts


def _chain_center(chain: list[_Lane]) -> list[Point]:
    pts: list[Point] = []
    for lane in chain:
        center = [(c[0], c[1]) for c in lane.center]
        pts.extend(center if not pts else center[1:])
    return pts


def _group_edges(
    chains: list[list[_Lane]], node_of: dict, lefthand: bool
) -> list[_Edge]:
    """Side-by-side chains between the same two nodes become one multi-lane edge."""
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, chain in enumerate(chains):
        groups[(node_of[chain[0].start], node_of[chain[-1].end])].append(index)
    left_nodes = [{n for lane in chain for n in lane.left} for chain in chains]
    right_nodes = [{n for lane in chain for n in lane.right} for chain in chains]
    edges: list[_Edge] = []
    used_ids: set[str] = set()
    for (from_node, to_node), members in groups.items():
        # right_of[a] = chains directly right of chain a (a's right bound is their left).
        right_of = {
            a: [b for b in members if b != a and right_nodes[a] & left_nodes[b]] for a in members
        }
        left_of = defaultdict(list)
        for a, rights in right_of.items():
            for b in rights:
                left_of[b].append(a)
        placed: set[int] = set()
        rows: list[list[int]] = []
        for start in members:
            if start in placed or left_of[start]:
                continue
            row = [start]
            placed.add(start)
            while len(right_of[row[-1]]) == 1 and right_of[row[-1]][0] not in placed:
                follower = right_of[row[-1]][0]
                if len(left_of[follower]) != 1:
                    break
                row.append(follower)
                placed.add(follower)
            rows.append(row)
        rows.extend([m] for m in members if m not in placed)
        for row in rows:  # row is ordered left -> right
            curb_first = row if lefthand else row[::-1]
            lane_chains = [chains[i] for i in curb_first]
            if len(row) == 1:
                shape = _chain_center(chains[row[0]])
            else:
                left_bound = _chain_bound(chains[row[0]], "left")
                right_bound = _chain_bound(chains[row[-1]], "right")
                count = max(len(left_bound), len(right_bound), 2)
                shape = [
                    ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
                    for a, b in zip(_resample(left_bound, count), _resample(right_bound, count))
                ]
            edge_id = f"ll{lane_chains[0][0].id}"
            suffix = 1
            while edge_id in used_ids:
                suffix += 1
                edge_id = f"ll{lane_chains[0][0].id}_{suffix}"
            used_ids.add(edge_id)
            shape = _simplify(shape, _SIMPLIFY_SHAPE_M)
            edges.append(_Edge(edge_id, from_node, to_node, lane_chains, shape))
    edges.sort(key=lambda e: _sort_key(e.id[2:]))
    return edges


class Lanelet2MapProvider:
    """Convert a local Lanelet2 ``.osm`` file into a geo-referenced :class:`MapPackage`.

    Args:
        osm_file: The Lanelet2 map.
        proj: Optional proj.4 definition of the nodes' ``local_x``/``local_y``
            frame; when given, those tags place the nodes instead of lat/lon.
    """

    name = "lanelet2"

    def __init__(self, osm_file: Path | str, *, proj: str | None = None) -> None:
        osm_file = Path(osm_file).resolve()
        if not osm_file.exists():
            raise ValueError(f"Lanelet2 file does not exist: {osm_file}")
        self.osm_file = osm_file
        self.proj = validate_proj_string(proj) if proj is not None else None

    @property
    def projector_info(self) -> Path | None:
        """Autoware's projection file next to the map, if any."""
        path = self.osm_file.with_name(PROJECTOR_INFO_NAME)
        return path if path.exists() else None

    def build(self, out_dir: Path) -> MapPackage:
        """Convert into ``out_dir`` and assemble a validated, geo-referenced package.

        Raises:
            GeoReferenceError: The nodes cannot be placed (a ``ValueError``).
            MapConversionError: netconvert failed (a ``RuntimeError``).
            ValueError: No drivable lanelet, or the package fails validation.
        """
        started = time.monotonic()
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        net_file = out_dir / "net.net.xml"
        with tempfile.TemporaryDirectory(prefix=".lanelet2-", dir=out_dir) as work:
            plain = convert_lanelet2_to_plain(self.osm_file, work, proj=self.proj)
            flags = (*NETCONVERT_LANELET2_FLAGS, *(("--lefthand",) if plain.lefthand else ()))
            cmd = [
                "netconvert",
                "--node-files", str(plain.nod_file),
                "--edge-files", str(plain.edg_file),
                "--connection-files", str(plain.con_file),
                *flags,
                "-o", str(net_file),
            ]
            result = _run_netconvert(cmd, input_file=self.osm_file)

        problem = geo_reference_problem(net_file)
        if problem is not None:
            raise GeoReferenceError(f"{self.osm_file}: {problem}. Fix: {PROJ_OVERRIDE_HINT}")
        ensure_net_loads(net_file, source=self.osm_file)
        (out_dir / MAPPING_FILE_NAME).write_text(
            json.dumps(plain.mapping, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        base_sumocfg = write_base_sumocfg(out_dir, net_file)
        pkg = MapPackage(
            root=out_dir.resolve(),
            net_file=net_file.resolve(),
            base_sumocfg=base_sumocfg,
            tls_files=(),
            geo_referenced=True,
            source="lanelet2",
            metadata={
                **package_metadata(self.osm_file, net_file),
                "lanelet2_file": str(self.osm_file),
                "proj_override": self.proj,
                "lanelet2_mapping": MAPPING_FILE_NAME,
                "netconvert_flags": list(flags),
            },
        )
        ensure_valid_map_package(pkg, context=str(self.osm_file))
        warnings = [*plain.warnings, *map_package_warnings(pkg)]
        for warning in warnings:
            logger.warning("%s: %s", self.osm_file, warning)
        pkg.metadata["conversion"] = conversion_summary(
            net_file,
            source_format="lanelet2",
            warnings=warnings,
            tls_source="lanelet2_traffic_lights" if plain.stats["signalized_nodes"] else "none",
            proj_override=self.proj,
            conversion_seconds=round(time.monotonic() - started, 2),
            netconvert_warnings=netconvert_warning_summary(result.stderr),
            lanelet2=plain.stats,
        )
        pkg.save()
        return pkg


__all__ = [
    "DEFAULT_SPEED_KMH",
    "INTERIOR_MAX_LENGTH_M",
    "MAPPING_FILE_NAME",
    "NETCONVERT_LANELET2_FLAGS",
    "PROJECTOR_INFO_NAME",
    "VEHICLE_SUBTYPES",
    "Lanelet2Conversion",
    "Lanelet2MapProvider",
    "convert_lanelet2_to_plain",
    "is_lanelet2_osm",
]
