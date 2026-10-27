#!/usr/bin/env python3
"""Generate a corridor's camera configuration from one inventory file.

One YAML inventory (corridor -> intersections -> cameras) is the single source for
every place a camera is named. ``generate`` writes, under the output directory:

  terasim/Metadata.json              TMC spec of the terasim-tmc collector: per
                                     intersection the OSM edge -> travel direction table
                                     (Headings), the turn table (Movements) and the
                                     approach -> camera table (Assignments)
  terasim/scheduled_config.yaml      the corridor's signals, map and optimization budget
  terasim/values-corridor.yaml       both files as terasim-phase1 chart values (0.6.0 or later)
  terasim/corridor-configmap.yaml    the same two files as a ConfigMap you manage
                                     (chart value corridorConfig.existingConfigMap)
  terasim/register-intersections.sh  registers every intersection with the TMC collector
  terasim/register-map.sh            (map.osm_extract only) stores the corridor's map
  vss/vst-streams.json               one {"name", "sensorUrl"} per camera, the body of
                                     POST /api/v1/rtsp-streams/add
  vss/register-vst-streams.sh        adds them through the VSS API
  vss/rtsp-fanout.yaml               (test feeds only) one RTSP server that re-serves the
                                     recorded feeds under one path per camera
  vss/calibration.json               VA-API calibration upload (cameras that have data)
  vss/calibration-todo.json          placeholder entries of cameras still to calibrate
  report.md, report.json             what was derived from the maps, and every issue

Where the tables come from:
  * Headings: the OSM road graph that VSS behavior analytics map-matches against
    (``road_graph.osm``: OSM XML or an Overpass JSON response, e.g. the osmnx cache of
    the behavior-analytics pod). Behaviors carry their path as OSM edges "<u>-<v>"
    (raw node pairs, osmSimplify false); every edge of the intersection's streets whose
    nearer end lies within ``headings_radius_m`` of the junction gets the compass
    direction of travel along it. Without an OSM graph the table is approximated from
    the SUMO map, which keeps junction node ids only (the report says so).
  * Movements: the cardinal turn table over the approaches and exits found above.
  * Assignments: each camera's ``approach``.
  * Signals (tls_ids): TeraSim's SUMO map (``map.sumo_net``), bound with the same
    radius and ambiguity rule as the TeraSim service. The SUMO approaches are checked
    against the cameras.

Usage:
  python3 camcfg.py generate INVENTORY.yaml -o OUT_DIR [--strict]
  python3 camcfg.py osm-extract INVENTORY.yaml -o corridor.osm
Exit status 1 when the inventory has errors (the files are still written, with the
reasons in report.md); with --strict also on warnings.

Requirements: Python 3.9+, PyYAML; sumolib to read SUMO maps (pip install pyyaml sumolib).
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import math
import re
import shlex
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

try:  # PyYAML is the only hard dependency; fail with a clear message when it is missing
    import yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    yaml = None

INVENTORY_VERSION = 1
HEADINGS = ("North", "East", "South", "West")
SHORT = {"North": "NB", "East": "EB", "South": "SB", "West": "WB"}
_CLOCKWISE = {"North": "East", "East": "South", "South": "West", "West": "North"}
_COUNTERCLOCKWISE = {after: before for before, after in _CLOCKWISE.items()}
_HEADING_ALIASES = {}
for _heading in HEADINGS:
    for _alias in (_heading, _heading[0], SHORT[_heading], f"{_heading}bound"):
        _HEADING_ALIASES[_alias.lower()] = _heading
_NO_APPROACH = {"", "none", "null", "-", "exit"}

#: Same binding standard as the TeraSim service (terasim_city.binding): a camera
#: intersection binds to the nearest traffic light within this radius (SUMO map units),
#: and a runner-up within AMBIGUITY_CLEARANCE x the winner's distance is ambiguous.
BINDING_RADIUS_M = 60.0
AMBIGUITY_CLEARANCE = 1.5
#: Default reach of the Headings table along each street (metres from the junction).
#: 125 m covers every edge of the hand-made reference spec it was checked against.
DEFAULT_HEADINGS_RADIUS_M = 125.0
#: Junction search radius around an intersection's coordinates (metres).
JUNCTION_SEARCH_M = 40.0
#: Edges whose direction is within this many degrees of a diagonal are reported.
DIAGONAL_WARN_DEG = 10.0
#: TeraSim's map store (chart: TERASIM_CITY_DATA_ROOT) and its map id length.
DEFAULT_STORE_ROOT = "/data/output/store"
#: The map baked into the TeraSim image (the synthetic demo corridor), used as the
#: service-side map path when the inventory names neither map.sumocfg nor map.osm_extract.
DEFAULT_SUMOCFG = "/data/input/map/demo.sumocfg"
MAP_ID_LEN = 12
#: TeraSim's OpenStreetMap import: bounding box margin and Overpass query
#: (terasim_service.city.map_acquisition), so an extract made here converts to the
#: same map the service would download itself.
OSM_MARGIN_M = 250.0
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_EARTH_RADIUS_M = 6_371_000.0
_SENSOR_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: Optimization budget written into scheduled_config.yaml when the inventory gives none:
#: the shipped corridor configuration's (30 simulated minutes per candidate plan).
DEFAULT_OPTIMIZATION = {"sim_end": 1800, "max_trials": 24, "patience": 16, "n_workers": 4}
DEFAULT_VSS = {
    "namespace": "vss",
    "host": "https://<vss-route-host>",
    # A recorded feed served by the VSS chart's rtsp-test-source pods.
    "test_feed_url": "rtsp://rtsp-test-source-{feed}.{namespace}.svc.cluster.local:8554/test",
    "fanout_name": "rtsp-fanout",
    # Pinned: the fan-out configuration below is written for (and tested with) this release.
    "fanout_image": "bluenviron/mediamtx:1.21.2",
    "calibration_city": "",
}
#: Where the terasim-phase1 chart runs: namespace and Helm release name. ``fullname``
#: (optional) mirrors the chart's fullnameOverride; otherwise the chart's own rule names
#: the Deployments (see :func:`chart_fullname`).
DEFAULT_TERASIM = {"namespace": "terasim", "release": "terasim-phase1"}
#: Name of the Helm chart the generated values and scripts are written for.
CHART_NAME = "terasim-phase1"


class InventoryError(ValueError):
    """The inventory cannot be read at all (as opposed to issues listed in the report)."""


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def movement(entry: str, exit_: str) -> Optional[str]:
    """TMC movement of a vehicle entering with heading ``entry`` and leaving with ``exit_``.

    Headings are directions of travel (South = southbound). U-turns are not a TMC movement.
    """
    if exit_ == entry:
        return "Straight"
    if exit_ == _CLOCKWISE[entry]:
        return "Right"
    if exit_ == _COUNTERCLOCKWISE[entry]:
        return "Left"
    return None


def parse_approach(value: Any) -> Optional[str]:
    """``North``/``NB``/``n``/``northbound`` -> ``North``; ``none``/null -> None."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _NO_APPROACH:
        return None
    if text not in _HEADING_ALIASES:
        raise ValueError(f"approach {value!r}: use North/East/South/West (or NB/EB/SB/WB), or none")
    return _HEADING_ALIASES[text]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((phi2 - phi1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing from point 1 to point 2 (0 = north, clockwise)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def cardinal(bearing: float) -> str:
    """Nearest of North/East/South/West."""
    return HEADINGS[int(((bearing % 360.0) + 45.0) // 90.0) % 4]


def diagonal_margin(bearing: float) -> float:
    """Degrees between ``bearing`` and the nearest diagonal (0 = exactly NE/SE/SW/NW)."""
    return abs((bearing % 90.0) - 45.0)


_DIRECTION_PREFIX = re.compile(r"^(north|south|east|west|n|s|e|w)\.?\s+(?=\S+\s+\S)", re.I)


def street_key(name: Optional[str]) -> str:
    """Comparable street name: "West Main Street" and "East Main Street" are one street
    ("main street"); "N 1st St" is "1st st"."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    return _DIRECTION_PREFIX.sub("", text).lower()


def k8s_name(text: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", str(text).lower()).strip("-")
    return name[:63].rstrip("-") or "corridor"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_text(path: Path):
    path = Path(path)
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


# --------------------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------------------


@dataclass
class Camera:
    sensor: str
    approach: Optional[str]
    rtsp_url: Optional[str]
    test_feed: Optional[str]
    calibration: Any
    note: str = ""


@dataclass
class Intersection:
    id: str
    name: str
    lat: float
    lon: float
    cameras: list
    streets: list = field(default_factory=list)
    tls_id: Optional[str] = None
    headings_radius_m: Optional[float] = None


@dataclass
class Inventory:
    path: Path
    corridor: dict
    map: dict
    road_graph: dict
    optimization: dict
    vss: dict
    terasim: dict
    intersections: list

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def resolve(self, value: Optional[str]) -> Optional[Path]:
        if not value:
            return None
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else (self.base_dir / path)

    def cameras(self) -> Iterable[tuple[Intersection, Camera]]:
        for intersection in self.intersections:
            for camera in intersection.cameras:
                yield intersection, camera


def _mapping(value: Any, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InventoryError(f"{where} must be a mapping, got {type(value).__name__}")
    return dict(value)


def load_inventory(path: Path | str) -> Inventory:
    """Read and validate the inventory; structural problems raise :class:`InventoryError`."""
    if yaml is None:
        raise InventoryError("PyYAML is required: pip install pyyaml")
    path = Path(path).resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InventoryError(f"cannot read inventory {path}: {exc}") from exc
    raw = _mapping(raw, "the inventory")
    version = raw.get("version", INVENTORY_VERSION)
    if version != INVENTORY_VERSION:
        raise InventoryError(
            f"inventory version {version!r} is not supported (use {INVENTORY_VERSION})"
        )

    corridor = _mapping(raw.get("corridor"), "corridor")
    corridor_id = str(corridor.get("id") or "").strip()
    if not _ID_RE.match(corridor_id):
        raise InventoryError("corridor.id is required: letters, digits, '.', '_' or '-'")
    corridor["id"] = corridor_id
    corridor.setdefault("name", corridor_id)
    corridor.setdefault("description", "")
    corridor.setdefault("observation_interval", "10m")
    if not re.match(r"^\d+[smhd]$", str(corridor["observation_interval"])):
        raise InventoryError("corridor.observation_interval must look like 10m, 600s or 1h")
    corridor["city"] = _mapping(corridor.get("city"), "corridor.city")

    camera_defaults = _mapping(raw.get("camera_defaults"), "camera_defaults")
    intersections: list[Intersection] = []
    seen_ids: set[str] = set()
    seen_sensors: dict[str, str] = {}
    raw_intersections = raw.get("intersections")
    if not isinstance(raw_intersections, list) or not raw_intersections:
        raise InventoryError("intersections must be a non-empty list")
    for index, item in enumerate(raw_intersections, start=1):
        where = f"intersections[{index}]"
        item = _mapping(item, where)
        iid = str(item.get("id") or "").strip()
        if not _ID_RE.match(iid):
            raise InventoryError(f"{where}.id is required: letters, digits, '.', '_' or '-'")
        if iid in seen_ids:
            raise InventoryError(f"{where}: intersection id {iid!r} is listed twice")
        seen_ids.add(iid)
        try:
            lat, lon = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            raise InventoryError(f"{where} ({iid}): lat and lon are required numbers") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise InventoryError(f"{where} ({iid}): lat/lon out of range")
        name = str(item.get("name") or iid)
        cameras: list[Camera] = []
        approaches_seen: dict[str, str] = {}
        raw_cameras = item.get("cameras")
        if not isinstance(raw_cameras, list) or not raw_cameras:
            raise InventoryError(f"{where} ({iid}): cameras must be a non-empty list")
        for cindex, cam in enumerate(raw_cameras, start=1):
            cwhere = f"{where} ({iid}).cameras[{cindex}]"
            cam = {**camera_defaults, **_mapping(cam, cwhere)}
            sensor = str(cam.get("sensor") or "").strip()
            if not _SENSOR_RE.match(sensor):
                raise InventoryError(
                    f"{cwhere}: sensor is required (the VST stream name): letters, digits, '.', '_', '-'"
                )
            if sensor in seen_sensors:
                raise InventoryError(
                    f"{cwhere}: sensor {sensor!r} is already used at {seen_sensors[sensor]}"
                )
            seen_sensors[sensor] = iid
            try:
                approach = parse_approach(cam.get("approach"))
            except ValueError as exc:
                raise InventoryError(f"{cwhere}: {exc}") from None
            if approach is not None:
                if approach in approaches_seen:
                    raise InventoryError(
                        f"{cwhere}: approach {approach} is already counted by "
                        f"{approaches_seen[approach]} (one camera per approach)"
                    )
                approaches_seen[approach] = sensor
            rtsp_url = cam.get("rtsp_url")
            test_feed = cam.get("test_feed")
            if bool(rtsp_url) == bool(test_feed):
                raise InventoryError(
                    f"{cwhere}: give exactly one of rtsp_url (a camera) or test_feed (a recorded feed)"
                )
            if rtsp_url and not re.match(r"^rtsps?://", str(rtsp_url)):
                raise InventoryError(f"{cwhere}: rtsp_url must start with rtsp:// or rtsps://")
            if test_feed and not _SENSOR_RE.match(str(test_feed)):
                raise InventoryError(f"{cwhere}: test_feed must be a feed name such as cam1")
            calibration = cam.get("calibration", "placeholder")
            if calibration not in (None, "placeholder"):
                calibration = _mapping(calibration, f"{cwhere}.calibration")
                if not calibration.get("copy_from") or not calibration.get("sensor"):
                    raise InventoryError(
                        f"{cwhere}.calibration: use placeholder, or copy_from: <calibration.json> with sensor: <id>"
                    )
            cameras.append(
                Camera(
                    sensor=sensor,
                    approach=approach,
                    rtsp_url=str(rtsp_url) if rtsp_url else None,
                    test_feed=str(test_feed) if test_feed else None,
                    calibration=calibration or "placeholder",
                    note=str(cam.get("note") or ""),
                )
            )
        radius = item.get("headings_radius_m")
        intersections.append(
            Intersection(
                id=iid,
                name=name,
                lat=lat,
                lon=lon,
                cameras=cameras,
                streets=[str(s) for s in (item.get("streets") or [])],
                tls_id=str(item["tls_id"]) if item.get("tls_id") else None,
                headings_radius_m=float(radius) if radius is not None else None,
            )
        )

    optimization = {**DEFAULT_OPTIMIZATION, **_mapping(raw.get("optimization"), "optimization")}
    vss = {**DEFAULT_VSS, **_mapping(raw.get("vss"), "vss")}
    terasim = {**DEFAULT_TERASIM, **_mapping(raw.get("terasim"), "terasim")}
    return Inventory(
        path=path,
        corridor=corridor,
        map=_mapping(raw.get("map"), "map"),
        road_graph=_mapping(raw.get("road_graph"), "road_graph"),
        optimization=optimization,
        vss=vss,
        terasim=terasim,
        intersections=intersections,
    )


# --------------------------------------------------------------------------------------
# OSM road graph (what VSS map-matches against)
# --------------------------------------------------------------------------------------

#: osmnx's network_type="drive" filter (the behavior-analytics road network uses it).
_EXCLUDED_HIGHWAYS = {
    "abandoned",
    "bridleway",
    "bus_guideway",
    "construction",
    "corridor",
    "cycleway",
    "elevator",
    "escalator",
    "footway",
    "no",
    "path",
    "pedestrian",
    "planned",
    "platform",
    "proposed",
    "raceway",
    "razed",
    "service",
    "steps",
    "track",
}
_EXCLUDED_SERVICE = {"alley", "driveway", "emergency_access", "parking", "parking_aisle", "private"}


def _drivable(tags: Mapping[str, str]) -> bool:
    highway = tags.get("highway")
    if not highway or highway in _EXCLUDED_HIGHWAYS:
        return False
    if tags.get("area") == "yes" or tags.get("access") == "private":
        return False
    if tags.get("motor_vehicle") == "no" or tags.get("motorcar") == "no":
        return False
    return tags.get("service") not in _EXCLUDED_SERVICE


def _oneway(tags: Mapping[str, str]) -> int:
    """0 both directions, 1 along the way's node order, -1 against it (osmnx semantics)."""
    value = str(tags.get("oneway", "")).strip().lower()
    if value in ("-1", "reverse"):
        return -1
    if value in ("yes", "true", "1"):
        return 1
    if tags.get("junction") in ("roundabout", "circular") and value != "no":
        return 1
    return 0


@dataclass(frozen=True)
class OsmWay:
    id: int
    nodes: tuple
    name: str
    key: str
    highway: str
    oneway: int


@dataclass
class OsmGraph:
    nodes: dict  # node id -> (lat, lon)
    signals: set  # node ids tagged highway=traffic_signals
    ways: list  # drivable OsmWay
    node_ways: dict  # node id -> [way index]
    source: str = ""

    def directed_edges(self, way: OsmWay) -> Iterable[tuple[int, int]]:
        for a, b in zip(way.nodes, way.nodes[1:]):
            if way.oneway >= 0:
                yield a, b
            if way.oneway <= 0:
                yield b, a


def load_osm_graph(path: Path | str) -> OsmGraph:
    """Drivable OSM graph from OSM XML (.osm, .osm.gz) or an Overpass JSON response."""
    path = Path(path)
    with _open_text(path) as handle:
        head = handle.read(512).lstrip()
    nodes: dict[int, tuple[float, float]] = {}
    signals: set[int] = set()
    raw_ways: list[tuple[int, list[int], dict]] = []
    if head.startswith("{") or head.startswith("["):
        with _open_text(path) as handle:
            payload = json.load(handle)
        elements = payload.get("elements", []) if isinstance(payload, Mapping) else []
        for element in elements:
            kind = element.get("type")
            if kind == "node" and "lat" in element:
                nodes[int(element["id"])] = (float(element["lat"]), float(element["lon"]))
                if (element.get("tags") or {}).get("highway") == "traffic_signals":
                    signals.add(int(element["id"]))
            elif kind == "way":
                raw_ways.append(
                    (
                        int(element["id"]),
                        [int(n) for n in element.get("nodes", [])],
                        element.get("tags") or {},
                    )
                )
    else:
        with _open_text(path) as handle:
            for _event, element in ET.iterparse(handle, events=("end",)):
                if element.tag == "node":
                    nid = int(element.get("id"))
                    nodes[nid] = (float(element.get("lat")), float(element.get("lon")))
                    for tag in element.findall("tag"):
                        if tag.get("k") == "highway" and tag.get("v") == "traffic_signals":
                            signals.add(nid)
                    element.clear()
                elif element.tag == "way":
                    tags = {tag.get("k"): tag.get("v") for tag in element.findall("tag")}
                    refs = [int(nd.get("ref")) for nd in element.findall("nd")]
                    raw_ways.append((int(element.get("id")), refs, tags))
                    element.clear()
    ways: list[OsmWay] = []
    node_ways: dict[int, list[int]] = {}
    for way_id, refs, tags in raw_ways:
        if not _drivable(tags):
            continue
        refs = [ref for ref in refs if ref in nodes]
        if len(refs) < 2:
            continue
        way = OsmWay(
            id=way_id,
            nodes=tuple(refs),
            name=str(tags.get("name") or ""),
            key=street_key(tags.get("name")),
            highway=str(tags.get("highway")),
            oneway=_oneway(tags),
        )
        for ref in refs:
            node_ways.setdefault(ref, []).append(len(ways))
        ways.append(way)
    if not ways:
        raise InventoryError(f"{path}: no drivable OSM ways found (is it an OSM road extract?)")
    return OsmGraph(nodes=nodes, signals=signals, ways=ways, node_ways=node_ways, source=str(path))


@dataclass
class HeadingsResult:
    source: str  # "osm" | "sumo"
    center: str  # junction node id(s) the table is centred on
    center_distance_m: float
    streets: list
    headings: dict
    approaches: list
    exits: list
    edges_per_heading: dict
    near_diagonal: list
    notes: list = field(default_factory=list)

    def movements(self, assigned: Iterable[str] = ()) -> dict:
        """Turn table over the approaches and exits found. ``assigned`` headings get rows
        too, even without inbound traffic: the TMC collector reads a row for every
        Assignments heading and fails its whole tick on a missing one."""
        entries = set(self.approaches) | set(assigned)
        table: dict[str, str] = {}
        for entry in HEADINGS:
            if entry not in entries:
                continue
            for name in ("Right", "Straight", "Left"):
                for exit_ in HEADINGS:
                    if exit_ in self.exits and movement(entry, exit_) == name:
                        table[f"{entry} - {exit_}"] = name
        return table


def _osm_streets_at(graph: OsmGraph, node: int) -> dict[str, str]:
    """street key -> display name of the named drivable ways through ``node``."""
    streets: dict[str, str] = {}
    for index in graph.node_ways.get(node, ()):
        way = graph.ways[index]
        if way.key:
            streets.setdefault(way.key, way.name)
    return streets


def find_osm_junction(graph: OsmGraph, lat: float, lon: float, *, max_m: float = JUNCTION_SEARCH_M):
    """The OSM junction of an intersection: the nearest node within ``max_m`` where two
    differently named streets meet, else the nearest traffic-signal node.

    Returns ``(node id, distance m, {street key: name})``; raises ValueError when none.
    """
    best = None
    signal = None
    for node, (nlat, nlon) in graph.nodes.items():
        if abs(nlat - lat) > 0.001 or abs(nlon - lon) > 0.0013:
            continue  # cheap prefilter (~110 m)
        if node not in graph.node_ways:
            continue
        distance = haversine_m(lat, lon, nlat, nlon)
        if distance > max_m:
            continue
        streets = _osm_streets_at(graph, node)
        if len(streets) >= 2 and (best is None or distance < best[1]):
            best = (node, distance, streets)
        if node in graph.signals and (signal is None or distance < signal[1]):
            signal = (node, distance, streets)
    if best is not None:
        return best
    if signal is not None:
        node, distance, streets = signal
        # Junction split over several OSM nodes: gather the names around the signal.
        slat, slon = graph.nodes[node]
        for other, (olat, olon) in graph.nodes.items():
            if other in graph.node_ways and haversine_m(slat, slon, olat, olon) <= 25.0:
                for key, name in _osm_streets_at(graph, other).items():
                    streets.setdefault(key, name)
        return node, distance, streets
    raise ValueError(f"no OSM junction or traffic signal within {max_m:.0f} m")


def derive_headings_osm(
    graph: OsmGraph,
    lat: float,
    lon: float,
    *,
    radius_m: float = DEFAULT_HEADINGS_RADIUS_M,
    streets: Iterable[str] = (),
) -> HeadingsResult:
    """Headings table of one intersection from the OSM road graph VSS map-matches on."""
    center, distance, found = find_osm_junction(graph, lat, lon)
    wanted = {street_key(name): name for name in streets} if streets else found
    if not wanted:
        raise ValueError("no street names at the junction; list them under streets:")
    clat, clon = graph.nodes[center]

    def dist(node: int) -> float:
        nlat, nlon = graph.nodes[node]
        return haversine_m(clat, clon, nlat, nlon)

    # Walk the intersection's streets outward from the junction: only segments connected
    # to it count (a same-named piece of street elsewhere in the radius does not).
    neighbours: dict[int, list[tuple[int, OsmWay, int, int]]] = {}
    for way in graph.ways:
        if way.key not in wanted:
            continue
        for a, b in zip(way.nodes, way.nodes[1:]):
            neighbours.setdefault(a, []).append((b, way, a, b))
            neighbours.setdefault(b, []).append((a, way, a, b))
    seen_nodes = {center}
    frontier = [center]
    segments: dict[tuple[int, int, int], OsmWay] = {}
    while frontier:
        node = frontier.pop()
        for other, way, a, b in neighbours.get(node, ()):
            segments[(way.id, a, b)] = way
            if other not in seen_nodes and dist(other) <= radius_m:
                seen_nodes.add(other)
                frontier.append(other)

    headings: dict[str, str] = {}
    approaches: set[str] = set()
    exits: set[str] = set()
    near_diagonal: list[str] = []
    for (_way_id, a, b), way in segments.items():
        if min(dist(a), dist(b)) > radius_m:
            continue
        directions = ([(a, b)] if way.oneway >= 0 else []) + ([(b, a)] if way.oneway <= 0 else [])
        for u, v in directions:
            ulat, ulon = graph.nodes[u]
            vlat, vlon = graph.nodes[v]
            bearing = bearing_deg(ulat, ulon, vlat, vlon)
            heading = cardinal(bearing)
            key = f"{u}-{v}"
            headings[key] = heading
            if diagonal_margin(bearing) < DIAGONAL_WARN_DEG:
                near_diagonal.append(key)
            du, dv = dist(u), dist(v)
            if dv < du:
                approaches.add(heading)
            elif dv > du:
                exits.add(heading)
    counts = {heading: sum(1 for h in headings.values() if h == heading) for heading in HEADINGS}
    reached = {way.key for way in segments.values()}
    notes = [
        f"street {wanted[key]!r} is not connected to the junction in the OSM graph"
        for key in sorted(set(wanted) - reached)
    ]
    return HeadingsResult(
        source="osm",
        center=str(center),
        center_distance_m=round(distance, 1),
        streets=sorted(wanted.values()),
        headings=dict(
            sorted(headings.items(), key=lambda item: (HEADINGS.index(item[1]), item[0]))
        ),
        approaches=[h for h in HEADINGS if h in approaches],
        exits=[h for h in HEADINGS if h in exits],
        edges_per_heading=counts,
        near_diagonal=sorted(near_diagonal),
        notes=notes,
    )


# --------------------------------------------------------------------------------------
# SUMO map (what TeraSim simulates)
# --------------------------------------------------------------------------------------


def load_sumo_net(path: Path | str):
    try:
        import sumolib  # noqa: PLC0415 - optional dependency
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise InventoryError("reading a SUMO map needs sumolib: pip install sumolib") from exc
    return sumolib.net.readNet(str(path))


def node_to_tls_map(net) -> dict[str, str]:
    """Every controlled junction id -> its TLS id (terasim_city.binding.node_to_tls_map)."""
    mapping: dict[str, str] = {}
    for tls in net.getTrafficLights():
        tls_id = tls.getID()
        if net.hasNode(tls_id):
            mapping.setdefault(tls_id, tls_id)
        for in_lane, _out_lane, _link in tls.getConnections():
            mapping.setdefault(in_lane.getEdge().getToNode().getID(), tls_id)
    return mapping


def signal_candidates(
    net, node_to_tls: Mapping[str, str], lat: float, lon: float, radius_m: float = BINDING_RADIUS_M
):
    """[(tls_id, node_id, distance)] within the radius, nearest first, one per TLS
    (terasim_city.binding.signal_candidates)."""
    x, y = net.convertLonLat2XY(lon, lat)
    best: dict[str, tuple[str, str, float]] = {}
    for node_id, tls_id in node_to_tls.items():
        nx, ny = net.getNode(node_id).getCoord()
        distance = math.hypot(nx - x, ny - y)
        if distance > radius_m:
            continue
        if tls_id not in best or distance < best[tls_id][2]:
            best[tls_id] = (tls_id, node_id, round(distance, 2))
    return sorted(best.values(), key=lambda item: (item[2], item[0]))


def _xy_heading(x1: float, y1: float, x2: float, y2: float) -> float:
    return (math.degrees(math.atan2(x2 - x1, y2 - y1)) + 360.0) % 360.0


_TURN_CODES = {"s": "Straight", "r": "Right", "R": "Right", "l": "Left", "L": "Left"}


def sumo_approaches(net, tls_id: str, node_to_tls: Mapping[str, str]) -> dict[str, list[str]]:
    """Approach heading -> legal turns of the passenger edges entering a TLS's junctions."""
    nodes = {node for node, tls in node_to_tls.items() if tls == tls_id}
    result: dict[str, set[str]] = {}
    for node_id in nodes:
        for edge in net.getNode(node_id).getIncoming():
            if edge.getID().startswith(":") or not edge.allows("passenger"):
                continue
            if edge.getFromNode().getID() in nodes:
                continue  # inside a junction split over several nodes
            shape = edge.getShape()
            (x1, y1), (x2, y2) = (
                (shape[-2], shape[-1])
                if len(shape) >= 2
                else (edge.getFromNode().getCoord(), edge.getToNode().getCoord())
            )
            heading = cardinal(_xy_heading(x1, y1, x2, y2))
            turns = result.setdefault(heading, set())
            for _out, connections in edge.getOutgoing().items():
                for connection in connections:
                    name = _TURN_CODES.get(connection.getDirection())
                    if name:
                        turns.add(name)
    return {
        h: sorted(result[h], key=("Right", "Straight", "Left").index)
        for h in HEADINGS
        if h in result
    }


def _junction_osm_ids(junction_id: str) -> list[str]:
    """OSM node ids a SUMO junction id names: the id itself, or a cluster's listed members
    (netconvert truncates long clusters with "#<n>more")."""
    if junction_id.isdigit():
        return [junction_id]
    if junction_id.startswith("cluster_"):
        return [part for part in junction_id[len("cluster_") :].split("_") if part.isdigit()]
    return []


def derive_headings_sumo(
    net, lat: float, lon: float, *, radius_m: float, streets: Iterable[str]
) -> HeadingsResult:
    """Approximate Headings from a SUMO map built from OSM: junction ids are OSM node ids,
    but the geometry nodes between junctions are gone, so edges through them are missing."""
    x0, y0 = net.convertLonLat2XY(lon, lat)
    wanted: dict[str, str] = {street_key(name): name for name in streets}
    if not wanted:
        # The street names of the junction nearest to the point.
        best = None
        for node in net.getNodes():
            if node.getID().startswith(":"):
                continue
            nx, ny = node.getCoord()
            distance = math.hypot(nx - x0, ny - y0)
            names = {
                street_key(e.getName()): e.getName()
                for e in node.getIncoming() + node.getOutgoing()
                if e.getName()
            }
            if (
                len(names) >= 2
                and distance <= BINDING_RADIUS_M
                and (best is None or distance < best[0])
            ):
                best = (distance, names)
        if best is None:
            raise ValueError(
                "no junction of two named streets near the intersection in the SUMO map"
            )
        wanted = best[1]
    headings: dict[str, str] = {}
    approaches: set[str] = set()
    exits: set[str] = set()
    near_diagonal: list[str] = []
    for edge in net.getEdges():
        if edge.getID().startswith(":") or not edge.allows("passenger"):
            continue
        if street_key(edge.getName()) not in wanted:
            continue
        flon, flat = net.convertXY2LonLat(*edge.getFromNode().getCoord())
        tlon, tlat = net.convertXY2LonLat(*edge.getToNode().getCoord())
        du = haversine_m(lat, lon, flat, flon)
        dv = haversine_m(lat, lon, tlat, tlon)
        if min(du, dv) > radius_m:
            continue
        bearing = bearing_deg(flat, flon, tlat, tlon)
        heading = cardinal(bearing)
        for u in _junction_osm_ids(edge.getFromNode().getID()):
            for v in _junction_osm_ids(edge.getToNode().getID()):
                if u != v:
                    headings[f"{u}-{v}"] = heading
                    if diagonal_margin(bearing) < DIAGONAL_WARN_DEG:
                        near_diagonal.append(f"{u}-{v}")
        if dv < du:
            approaches.add(heading)
        elif dv > du:
            exits.add(heading)
    counts = {heading: sum(1 for h in headings.values() if h == heading) for heading in HEADINGS}
    return HeadingsResult(
        source="sumo",
        center=f"{lat:.6f},{lon:.6f}",
        center_distance_m=0.0,
        streets=sorted(wanted.values()),
        headings=dict(
            sorted(headings.items(), key=lambda item: (HEADINGS.index(item[1]), item[0]))
        ),
        approaches=[h for h in HEADINGS if h in approaches],
        exits=[h for h in HEADINGS if h in exits],
        edges_per_heading=counts,
        near_diagonal=sorted(near_diagonal),
        notes=[
            "approximate: derived from the SUMO map, which keeps OSM junction ids only "
            "(geometry nodes between junctions are removed and long junction clusters are "
            "truncated). VSS behaviors whose edges fall on the missing node pairs are not "
            "counted; give road_graph.osm (the OSM data VSS map-matches against) for the exact table."
        ],
    )


# --------------------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------------------


@dataclass
class Issue:
    level: str  # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper()} {self.where}: {self.message}"


@dataclass
class IntersectionResult:
    intersection: Intersection
    headings: Optional[HeadingsResult] = None
    tls_id: Optional[str] = None
    tls_distance_m: Optional[float] = None
    sumo_approaches: dict = field(default_factory=dict)
    assignments: dict = field(default_factory=dict)


@dataclass
class Generation:
    inventory: Inventory
    results: list
    issues: list
    files: dict  # relative path -> text
    map_info: dict

    @property
    def errors(self) -> list:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list:
        return [issue for issue in self.issues if issue.level == "warning"]


def _map_settings(inv: Inventory, issues: list) -> dict:
    """Where the TeraSim map lives in the service, and which local copy to read."""
    settings = dict(inv.map)
    store_root = str(settings.get("store_root") or DEFAULT_STORE_ROOT).rstrip("/")
    info: dict[str, Any] = {
        "sumocfg": settings.get("sumocfg"),
        "sumo_net": None,
        "osm_extract": None,
        "map_id": None,
    }
    extract = inv.resolve(settings.get("osm_extract"))
    if extract is not None:
        if not extract.is_file():
            issues.append(Issue("error", "map.osm_extract", f"file not found: {extract}"))
        else:
            map_id = file_sha256(extract)[:MAP_ID_LEN]
            info.update(
                osm_extract=extract,
                map_id=map_id,
                sumocfg=info["sumocfg"] or f"{store_root}/maps/{map_id}/base.sumocfg",
            )
    if not info["sumocfg"]:
        info["sumocfg"] = DEFAULT_SUMOCFG
    net = inv.resolve(settings.get("sumo_net"))
    if net is not None and not net.is_file():
        issues.append(Issue("error", "map.sumo_net", f"file not found: {net}"))
        net = None
    info["sumo_net"] = net
    return info


def _build_net_from_extract(extract: Path, work: Path):
    """Convert an OSM extract with TeraSim's own import (same netconvert flags)."""
    try:
        from terasim_city.maps.osm import OsmMapProvider  # noqa: PLC0415 - optional
    except ImportError:
        OsmMapProvider = None
    work.mkdir(parents=True, exist_ok=True)
    if OsmMapProvider is not None:
        return OsmMapProvider(extract).build(work).net_file
    import subprocess  # noqa: PLC0415

    net_file = work / "net.net.xml"
    flags = [
        "--geometry.remove",
        "--junctions.join",
        "--tls.guess-signals",
        "--tls.discard-simple",
        "--ramps.guess",
        "--tls.default-type",
        "static",
        "--output.street-names",
    ]
    subprocess.run(
        ["netconvert", "--osm-files", str(extract), *flags, "-o", str(net_file)],
        check=True,
        capture_output=True,
        text=True,
    )
    return net_file


def generate(
    inv: Inventory,
    *,
    out_dir: Optional[Path] = None,
    osm_graph: Optional[OsmGraph] = None,
    sumo_net=None,
) -> Generation:
    """Derive every table and render every output file (nothing is written here)."""
    issues: list[Issue] = []
    map_info = _map_settings(inv, issues)

    # --- the two maps ------------------------------------------------------------------
    if osm_graph is None and inv.road_graph.get("osm"):
        osm_path = inv.resolve(inv.road_graph["osm"])
        if osm_path is None or not osm_path.is_file():
            issues.append(Issue("error", "road_graph.osm", f"file not found: {osm_path}"))
        else:
            osm_graph = load_osm_graph(osm_path)
    if sumo_net is None:
        net_path = map_info["sumo_net"]
        if net_path is None and map_info["osm_extract"] is not None:
            # Converted once per extract into the temp directory (not into the output).
            work = Path(tempfile.gettempdir()) / "terasim-camcfg" / map_info["map_id"]
            net_path = work / "net.net.xml"
            if not net_path.is_file():
                net_path = _build_net_from_extract(map_info["osm_extract"], work)
            map_info["sumo_net"] = net_path
        if net_path is not None:
            sumo_net = load_sumo_net(net_path)
    if sumo_net is None:
        issues.append(
            Issue(
                "error",
                "map",
                "no SUMO map to bind the signals to: set map.sumo_net (a local copy of the map the "
                "service uses, e.g. examples/corridor_demo/demo.net.xml for the image's built-in "
                "demo map) or map.osm_extract",
            )
        )
    if osm_graph is None:
        issues.append(
            Issue(
                "warning",
                "road_graph.osm",
                "no OSM road graph given: Headings are approximated from the SUMO map (see each "
                "intersection's note); give the OSM data VSS map-matches against for exact tables",
            )
        )
    n2t = node_to_tls_map(sumo_net) if sumo_net is not None else {}
    default_radius = float(inv.road_graph.get("headings_radius_m") or DEFAULT_HEADINGS_RADIUS_M)

    # --- per intersection --------------------------------------------------------------
    results: list[IntersectionResult] = []
    for it in inv.intersections:
        where = f"intersection {it.id}"
        res = IntersectionResult(intersection=it)
        radius = it.headings_radius_m or default_radius
        try:
            if osm_graph is not None:
                res.headings = derive_headings_osm(
                    osm_graph, it.lat, it.lon, radius_m=radius, streets=it.streets
                )
            elif sumo_net is not None:
                res.headings = derive_headings_sumo(
                    sumo_net, it.lat, it.lon, radius_m=radius, streets=it.streets
                )
        except ValueError as exc:
            issues.append(Issue("error", where, f"Headings: {exc}"))
        if res.headings is not None:
            h = res.headings
            for note in h.notes:
                issues.append(Issue("warning", where, note))
            if len(h.streets) < 2:
                issues.append(
                    Issue(
                        "warning",
                        where,
                        f"only one street found at the junction ({', '.join(h.streets)}); list both under streets:",
                    )
                )
            if h.near_diagonal:
                issues.append(
                    Issue(
                        "warning",
                        where,
                        f"{len(h.near_diagonal)} edge(s) run within {DIAGONAL_WARN_DEG:.0f} deg of a diagonal, "
                        "their compass direction is a close call: check the Headings table",
                    )
                )
            if h.center_distance_m > 25:
                issues.append(
                    Issue(
                        "warning",
                        where,
                        f"the OSM junction is {h.center_distance_m:.0f} m from the given lat/lon",
                    )
                )

        # signal binding in TeraSim's map
        if sumo_net is not None:
            if it.tls_id:
                if it.tls_id not in {t.getID() for t in sumo_net.getTrafficLights()}:
                    issues.append(
                        Issue(
                            "error",
                            where,
                            f"tls_id {it.tls_id!r} is not a traffic light of the SUMO map",
                        )
                    )
                else:
                    res.tls_id = it.tls_id
            else:
                candidates = signal_candidates(sumo_net, n2t, it.lat, it.lon)
                if not candidates:
                    issues.append(
                        Issue(
                            "error",
                            where,
                            f"no traffic light within {BINDING_RADIUS_M:.0f} m in the SUMO map "
                            f"({map_info['sumo_net']}): the map does not cover this signal; use a map "
                            "that does (map.osm_extract) or remove the intersection",
                        )
                    )
                else:
                    best = candidates[0]
                    if len(candidates) > 1 and candidates[1][2] <= best[2] * AMBIGUITY_CLEARANCE:
                        issues.append(
                            Issue(
                                "error",
                                where,
                                f"ambiguous signal: {best[0]} ({best[2]} m) and {candidates[1][0]} "
                                f"({candidates[1][2]} m) are comparably close; set tls_id",
                            )
                        )
                    else:
                        res.tls_id, res.tls_distance_m = best[0], best[2]
            if res.tls_id:
                res.sumo_approaches = sumo_approaches(sumo_net, res.tls_id, n2t)
                if res.headings is not None:
                    missing = [a for a in res.headings.approaches if a not in res.sumo_approaches]
                    if missing:
                        issues.append(
                            Issue(
                                "warning",
                                where,
                                f"the SUMO map holds only part of this junction (approaches "
                                f"{'/'.join(SHORT[a] for a in res.sumo_approaches) or 'none'} of "
                                f"{'/'.join(SHORT[a] for a in res.headings.approaches)}; the map's edge "
                                f"may cut it): {', '.join(missing)} traffic cannot be simulated",
                            )
                        )

        # cameras -> Assignments
        for cam in it.cameras:
            if cam.approach is None:
                continue
            res.assignments[cam.approach] = cam.sensor
            if res.headings is not None and cam.approach not in res.headings.approaches:
                issues.append(
                    Issue(
                        "warning",
                        f"{where} camera {cam.sensor}",
                        f"approach {cam.approach}: the OSM graph has no {cam.approach.lower()}bound "
                        f"traffic entering this junction (one-way street?), so the camera counts nothing",
                    )
                )
            if res.sumo_approaches and cam.approach not in res.sumo_approaches:
                issues.append(
                    Issue(
                        "warning",
                        f"{where} camera {cam.sensor}",
                        f"approach {cam.approach}: the SUMO map has no {cam.approach.lower()}bound approach "
                        "at this signal, so its counts cannot be simulated",
                    )
                )
        if not res.assignments:
            issues.append(
                Issue("error", where, "no camera has an approach: the TMC spec needs at least one")
            )
        if res.headings is not None:
            uncovered = [a for a in res.headings.approaches if a not in res.assignments]
            if uncovered:
                issues.append(
                    Issue(
                        "warning",
                        where,
                        f"approach(es) without a camera: {', '.join(uncovered)} (their traffic is not counted)",
                    )
                )
        results.append(res)

    gen = Generation(inventory=inv, results=results, issues=issues, files={}, map_info=map_info)
    gen.files = render_files(gen)
    return gen


# --------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------

_HEADER = "Generated by camcfg.py from {name}; edit the inventory and regenerate instead of editing this file."


def _header(inv: Inventory, comment: str = "#") -> str:
    return f"{comment} " + _HEADER.format(name=inv.path.name)


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join((pad + line) if line else line for line in text.rstrip("\n").split("\n"))


def build_spec(gen: Generation) -> dict:
    inv = gen.inventory
    intersections = []
    for res in gen.results:
        it = res.intersection
        headings = res.headings.headings if res.headings else {}
        movements = res.headings.movements(res.assignments) if res.headings else {}
        intersections.append(
            {
                "intersection_id": it.id,
                "lat": it.lat,
                "lon": it.lon,
                "Headings": headings,
                "Movements": movements,
                "Assignments": dict(res.assignments),
            }
        )
    return {
        "corridor_id": inv.corridor["id"],
        "observation_interval": str(inv.corridor["observation_interval"]),
        "intersections": intersections,
    }


def build_scheduled_config(gen: Generation) -> str:
    inv = gen.inventory
    tls_ids = [res.tls_id for res in gen.results if res.tls_id]
    entry: dict[str, Any] = {
        "base_sumocfg": gen.map_info["sumocfg"],
        "corridor": {"tls_ids": tls_ids},
    }
    for key, value in inv.optimization.items():
        entry[key] = value
    body = yaml.safe_dump(
        {"corridors": {inv.corridor["id"]: entry}},
        sort_keys=False,
        default_flow_style=False,
        width=200,
    )
    lines = [
        _header(inv),
        f"# Corridor {inv.corridor['id']}: {len(tls_ids)} signal(s), west/first to east/last as listed in the inventory.",
        "# Signals (tls_ids) bound in "
        + (Path(str(gen.map_info["sumo_net"])).name if gen.map_info.get("sumo_net") else "<no map>")
        + f"; the service reads the map at {gen.map_info['sumocfg']}.",
    ]
    for res in gen.results:
        lines.append(f"#   {res.intersection.id}: {res.tls_id or 'NOT IN THE MAP'}")
    return "\n".join(lines) + "\n" + body


def _calibration_entries(gen: Generation) -> tuple[dict, dict, list]:
    """(upload document, todo document, status rows)."""
    inv = gen.inventory
    sources: dict[Path, dict] = {}
    upload = {"version": "1.0", "osmURL": "", "calibrationType": "geo", "sensors": []}
    todo = {
        "note": "Placeholder calibration entries. Replace each with the camera's calibration "
        "from VSS Studio (image <-> geo point pairs, ROI), then add it to calibration.json. "
        "Do not upload this file.",
        "sensors": [],
    }
    rows = []
    city = inv.vss.get("calibration_city") or inv.corridor["city"].get("id") or inv.corridor["id"]
    for it, cam in inv.cameras():
        cal = cam.calibration
        if isinstance(cal, Mapping):
            path = inv.resolve(cal["copy_from"])
            try:
                if path not in sources:
                    sources[path] = json.loads(Path(path).read_text(encoding="utf-8"))
                    for key in ("version", "osmURL", "calibrationType"):
                        if key in sources[path]:
                            upload[key] = sources[path][key]
                doc = sources[path]
                original = next(s for s in doc.get("sensors", []) if s.get("id") == cal["sensor"])
            except (OSError, ValueError, StopIteration) as exc:
                gen.issues.append(
                    Issue(
                        "error",
                        f"camera {cam.sensor}",
                        f"calibration copy_from {path} sensor {cal['sensor']}: {exc or 'sensor not found'}",
                    )
                )
                continue
            entry = copy.deepcopy(original)
            entry["id"] = cam.sensor
            place = [dict(p) for p in entry.get("place", [])]
            origin_place = next(
                (p.get("value") for p in place if p.get("name") == "intersection"), None
            )
            if any(p.get("name") == "intersection" for p in place):
                for p in place:
                    if p.get("name") == "intersection":
                        p["value"] = it.id
            else:
                place.append({"name": "intersection", "value": it.id})
            entry["place"] = place
            upload["sensors"].append(entry)
            same = origin_place in (None, it.id)
            rows.append(
                {
                    "sensor": cam.sensor,
                    "intersection": it.id,
                    "status": "copied" if same else "copied from another intersection",
                    "source": f"{Path(path).name}:{cal['sensor']}",
                    "note": (
                        ""
                        if same
                        else (
                            f"image<->geo points still describe {origin_place}: detections are placed at "
                            f"{origin_place}, not at {it.id}"
                        )
                    ),
                }
            )
        else:
            entry = {
                "id": cam.sensor,
                "type": "camera",
                "imageCoordinates": [],
                "globalCoordinates": [],
                "geoLocation": {"lat": it.lat, "lng": it.lon},
                "rois": [],
                "place": [
                    {"name": "city", "value": city},
                    {"name": "intersection", "value": it.id},
                ],
                "tripwires": [],
                "attributes": [
                    {"name": "fps", "value": ""},
                    {"name": "source", "value": "vst"},
                    {"name": "frameWidth", "value": ""},
                    {"name": "frameHeight", "value": ""},
                    {"name": "approach", "value": cam.approach or ""},
                ],
            }
            todo["sensors"].append(entry)
            rows.append(
                {
                    "sensor": cam.sensor,
                    "intersection": it.id,
                    "status": "placeholder",
                    "source": "",
                    "note": "needs a calibration from VSS Studio",
                }
            )
    return upload, todo, rows


def _stream_url(inv: Inventory, cam: Camera) -> str:
    if cam.rtsp_url:
        return cam.rtsp_url
    ns = inv.vss["namespace"]
    if cam.test_feed == cam.sensor:  # the recorded feed's own source pod
        return inv.vss["test_feed_url"].format(feed=cam.test_feed, namespace=ns)
    return f"rtsp://{inv.vss['fanout_name']}.{ns}.svc.cluster.local:8554/{cam.sensor}"


def _fanout_manifest(inv: Inventory) -> Optional[str]:
    feeds = [
        (it, cam) for it, cam in inv.cameras() if cam.test_feed and cam.test_feed != cam.sensor
    ]
    if not feeds:
        return None
    ns = inv.vss["namespace"]
    name = inv.vss["fanout_name"]
    lines = [
        "logLevel: info",
        "rtsp: yes",
        "rtspAddress: :8554",
        "rtspTransports: [tcp]",
        "rtmp: no",
        "hls: no",
        "webrtc: no",
        "srt: no",
        "api: no",
        "metrics: no",
        "playback: no",
        "moq: no",
        "paths:",
    ]
    for it, cam in feeds:
        source = inv.vss["test_feed_url"].format(feed=cam.test_feed, namespace=ns)
        lines += [
            f"  {cam.sensor}:  # {it.id} {cam.approach or 'no approach'}, replays feed {cam.test_feed}",
            f"    source: {source}",
            "    rtspTransport: tcp",
            "    sourceOnDemand: no",
        ]
    config = "\n".join(lines)
    return "\n".join(
        [
            _header(inv),
            "# TEST ONLY: re-serves the recorded feeds of the rtsp-test-source pods under one RTSP",
            f"# path per camera, rtsp://{name}.{ns}.svc.cluster.local:8554/<camera>, so every camera",
            "# is its own stream URL in VST (VST refuses a URL that is already registered).",
            "# One pod, no extra video copies; it pulls each recorded feed once per path.",
            f"#   oc apply -n {ns} -f rtsp-fanout.yaml",
            "apiVersion: v1",
            "kind: ConfigMap",
            "metadata:",
            f"  name: {name}",
            f"  labels: {{app: {name}}}",
            "data:",
            "  mediamtx.yml: |",
            _indent(config, 4),
            "---",
            "apiVersion: apps/v1",
            "kind: Deployment",
            "metadata:",
            f"  name: {name}",
            f"  labels: {{app: {name}}}",
            "spec:",
            "  replicas: 1",
            f"  selector: {{matchLabels: {{app: {name}}}}}",
            "  template:",
            "    metadata:",
            f"      labels: {{app: {name}}}",
            "      annotations:",
            f"        checksum/config: {hashlib.sha256(config.encode()).hexdigest()[:16]}",
            "    spec:",
            "      containers:",
            "        - name: mediamtx",
            f"          image: {inv.vss['fanout_image']}",
            "          args: [/config/mediamtx.yml]",
            "          ports:",
            "            - {name: rtsp, containerPort: 8554}",
            "          # restricted-v2 compatible (OpenShift assigns the uid); plain Kubernetes: add runAsUser.",
            "          securityContext:",
            "            allowPrivilegeEscalation: false",
            "            runAsNonRoot: true",
            "            capabilities: {drop: [ALL]}",
            "            seccompProfile: {type: RuntimeDefault}",
            "          resources:",
            "            requests: {cpu: 100m, memory: 64Mi}",
            '            limits: {cpu: "1", memory: 256Mi}',
            "          volumeMounts:",
            "            - {name: config, mountPath: /config, readOnly: true}",
            "      volumes:",
            "        - name: config",
            f"          configMap: {{name: {name}}}",
            "---",
            "apiVersion: v1",
            "kind: Service",
            "metadata:",
            f"  name: {name}",
            f"  labels: {{app: {name}}}",
            "spec:",
            f"  selector: {{app: {name}}}",
            "  ports:",
            "    - {name: rtsp, port: 8554, targetPort: 8554}",
            "",
        ]
    )


def chart_fullname(terasim: Mapping[str, Any]) -> str:
    """Name prefix of the chart's objects, by the chart's own rule (its fullname helper):
    ``fullname`` when given (the chart's fullnameOverride); the release name when it
    already contains the chart name; else ``<release>-terasim-phase1``. The Deployments
    are ``<prefix>-service``, ``<prefix>-mcp`` and ``<prefix>-tmc``."""
    if terasim.get("fullname"):
        name = str(terasim["fullname"])
    else:
        release = str(terasim.get("release") or CHART_NAME)
        name = release if CHART_NAME in release else f"{release}-{CHART_NAME}"
    return name[:63].rstrip("-")


def _register_streams_script(inv: Inventory) -> str:
    rows = [(cam.sensor, _stream_url(inv, cam)) for _it, cam in inv.cameras()]
    body = "\n".join(f"{name} {url}" for name, url in rows)
    names = " ".join(name for name, _ in rows)
    return f"""#!/usr/bin/env bash
{_header(inv)}
# Registers the corridor's {len(rows)} camera streams in VST through the VSS API, one
# POST /api/v1/rtsp-streams/add {{"sensorUrl": ..., "name": ...}} per camera (the same
# records as vst-streams.json). The name is the camera id everywhere else: the TMC spec's
# Assignments and the VA-API calibration.
#
#   H=https://<vss-route-host> ./register-vst-streams.sh            # add
#   H=https://<vss-route-host> ./register-vst-streams.sh --replace  # delete each name first
#
# Register only after the CV pipeline logs "Active sources" (streams added while its engine
# builds are dropped). VST refuses a URL that is already registered under another name.
set -euo pipefail
H="${{H:?set H to the VSS route, e.g. https://<vss-route-host>}}"
PAUSE_S="${{PAUSE_S:-2}}"
REPLACE=0
[ "${{1:-}}" = "--replace" ] && REPLACE=1
while read -r name url; do
  [ -n "$name" ] || continue
  if [ "$REPLACE" = 1 ]; then
    curl -sk -o /dev/null -X DELETE "$H/api/v1/rtsp-streams/delete/$name" || true
  fi
  printf '%s: ' "$name"
  curl -sk -X POST "$H/api/v1/rtsp-streams/add" -H 'Content-Type: application/json' \\
    -d "{{\\"sensorUrl\\":\\"$url\\",\\"name\\":\\"$name\\"}}"
  echo
  sleep "$PAUSE_S"
done <<'STREAMS'
{body}
STREAMS

# Check: every name listed by VST (name, sensorId, state).
EXPECTED="{names}"
LIST=$(curl -sk "$H/vst/api/v1/sensor/list")
missing=0
for name in $EXPECTED; do
  if ! printf '%s' "$LIST" | grep -q "\\"name\\": *\\"$name\\""; then echo "missing in VST: $name"; missing=$((missing + 1)); fi
done
echo "$(( {len(rows)} - missing )) of {len(rows)} streams listed by VST"
"""


def _register_intersections_script(inv: Inventory) -> str:
    calls = "\n".join(
        f"register {shlex.quote(it.id)} {shlex.quote(it.name)} {it.lat} {it.lon}"
        for it in inv.intersections
    )
    prefix = chart_fullname(inv.terasim)
    return f"""#!/usr/bin/env bash
{_header(inv)}
# Registers the corridor's {len(inv.intersections)} intersection(s) with the TeraSim TMC collector,
# which pulls camera counts only for registered intersections (once; the registry survives
# restarts, re-registering an id updates it). Run after the chart is installed with this
# corridor's Metadata.json. Needs `oc` logged in to the cluster (KUBECTL=kubectl for plain
# Kubernetes).
set -euo pipefail
KUBECTL="${{KUBECTL:-oc}}"
TNS="${{TNS:-{inv.terasim['namespace']}}}"                                   # TeraSim namespace
TMC="${{TMC:-deploy/{prefix}-tmc}}"   # the TMC collector Deployment
register() {{  # <intersection_id> <name> <lat> <lon>
  "$KUBECTL" exec -n "$TNS" "$TMC" -- python -c 'import json,sys,urllib.request as u
body=json.dumps({{"intersection_id":sys.argv[1],"name":sys.argv[2],"lat":float(sys.argv[3]),"lon":float(sys.argv[4])}}).encode()
req=u.Request("http://127.0.0.1:8200/v1/tmc/intersections",body,{{"Content-Type":"application/json"}})
print(u.urlopen(req).read().decode())' "$1" "$2" "$3" "$4"
}}
{calls}
"$KUBECTL" exec -n "$TNS" "$TMC" -- python -c 'import urllib.request as u;print(u.urlopen("http://127.0.0.1:8200/v1/tmc/health").read().decode())'
"""


def _register_map_script(inv: Inventory, map_info: dict) -> Optional[str]:
    extract = map_info.get("osm_extract")
    if extract is None:
        return None
    name = Path(extract).name
    prefix = chart_fullname(inv.terasim)
    return f"""#!/usr/bin/env bash
{_header(inv)}
# Stores the corridor's map in TeraSim (POST /v1/city/maps): copies the OpenStreetMap extract
# {name} into the service's volume and lets the service convert it with its own
# OpenStreetMap import. Nothing is downloaded; the image is not rebuilt. Expected map id
# {map_info['map_id']} (the first 12 hex digits of the file's SHA-256), which scheduled_config.yaml names:
#   {map_info['sumocfg']}
# Usage: ./register-map.sh <path to {name}>   (KUBECTL=kubectl for plain Kubernetes)
set -euo pipefail
FILE="${{1:?usage: $0 <path to {name}>}}"
KUBECTL="${{KUBECTL:-oc}}"
TNS="${{TNS:-{inv.terasim['namespace']}}}"
SVC="${{SVC:-deploy/{prefix}-service}}"
"$KUBECTL" exec -n "$TNS" "$SVC" -- mkdir -p /data/output/uploads
"$KUBECTL" exec -i -n "$TNS" "$SVC" -- sh -c 'cat > /data/output/uploads/{name}' < "$FILE"
"$KUBECTL" exec -n "$TNS" "$SVC" -- python -c 'import json,urllib.request as u
req=u.Request("http://127.0.0.1:8000/v1/city/maps",json.dumps({{"path":"/data/output/uploads/{name}"}}).encode(),{{"Content-Type":"application/json"}})
print(u.urlopen(req,timeout=600).read().decode())'
echo "expected map_id: {map_info['map_id']}"
"$KUBECTL" exec -n "$TNS" "$SVC" -- test -f {map_info['sumocfg']} && echo "map ready: {map_info['sumocfg']}"
"""


def _terasim_values(inv: Inventory, spec_text: str, scheduled_text: str) -> str:
    return "\n".join(
        [
            _header(inv),
            f"# terasim-phase1 chart values (chart 0.6.0 or later) for corridor {inv.corridor['id']}: the TMC spec and the",
            "# scheduled configuration replace the files shipped in the chart. Use on top of your site values:",
            "#   helm upgrade --install <release> <terasim-phase1 chart> -n <ns> -f <site values> -f values-corridor.yaml",
            "# The service and tmc pods restart on the upgrade (their pod template carries the files' checksum).",
            "corridorConfig:",
            "  metadataJson: |",
            _indent(spec_text, 4),
            "  scheduledConfigYaml: |",
            _indent(scheduled_text, 4),
            "",
        ]
    )


def _terasim_configmap(inv: Inventory, spec_text: str, scheduled_text: str) -> str:
    name = k8s_name(f"{inv.corridor['id']}-corridor")
    prefix = chart_fullname(inv.terasim)
    namespace = inv.terasim["namespace"]
    return "\n".join(
        [
            _header(inv),
            "# The corridor's two files as a ConfigMap you manage (e.g. in GitOps), instead of chart values:",
            f"#   oc apply -n {namespace} -f corridor-configmap.yaml",
            f"#   helm upgrade ... --set corridorConfig.existingConfigMap={name}",
            "# After changing it, restart the service and tmc Deployments (the chart cannot see its content):",
            f"#   oc rollout restart -n {namespace} deploy/{prefix}-service deploy/{prefix}-tmc",
            "apiVersion: v1",
            "kind: ConfigMap",
            "metadata:",
            f"  name: {name}",
            "data:",
            "  Metadata.json: |",
            _indent(spec_text, 4),
            "  scheduled_config.yaml: |",
            _indent(scheduled_text, 4),
            "",
        ]
    )


def render_files(gen: Generation) -> dict:
    inv = gen.inventory
    spec = build_spec(gen)
    spec_text = json.dumps(spec, indent=2) + "\n"
    scheduled_text = build_scheduled_config(gen)
    files = {
        "terasim/Metadata.json": spec_text,
        "terasim/scheduled_config.yaml": scheduled_text,
        "terasim/values-corridor.yaml": _terasim_values(inv, spec_text, scheduled_text),
        "terasim/corridor-configmap.yaml": _terasim_configmap(inv, spec_text, scheduled_text),
        "terasim/register-intersections.sh": _register_intersections_script(inv),
        "vss/vst-streams.json": json.dumps(
            [
                {"name": cam.sensor, "sensorUrl": _stream_url(inv, cam)}
                for _it, cam in inv.cameras()
            ],
            indent=1,
        )
        + "\n",
        "vss/register-vst-streams.sh": _register_streams_script(inv),
    }
    map_script = _register_map_script(inv, gen.map_info)
    if map_script:
        files["terasim/register-map.sh"] = map_script
    fanout = _fanout_manifest(inv)
    if fanout:
        files["vss/rtsp-fanout.yaml"] = fanout
    upload, todo, calibration_rows = _calibration_entries(gen)
    if upload["sensors"]:
        files["vss/calibration.json"] = json.dumps(upload, indent=1) + "\n"
    if todo["sensors"]:
        files["vss/calibration-todo.json"] = json.dumps(todo, indent=1) + "\n"
    report = build_report(gen, calibration_rows)
    files["report.json"] = json.dumps(report, indent=1, default=str) + "\n"
    files["report.md"] = render_report_md(gen, report, sorted([*files, "report.md"]))
    return files


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------


def _relative(inv: Inventory, value: Any) -> Any:
    """A path relative to the inventory's directory when inside it (portable reports)."""
    if not isinstance(value, (str, Path)) or not value or not Path(str(value)).is_absolute():
        return value
    try:
        return str(Path(str(value)).resolve().relative_to(inv.base_dir.resolve()))
    except ValueError:
        return str(value)


def _report_map(inv: Inventory, map_info: dict) -> dict:
    """map_info with portable paths; a network converted from the extract is named as such."""
    out = {k: _relative(inv, str(v) if isinstance(v, Path) else v) for k, v in map_info.items()}
    cache = Path(tempfile.gettempdir()) / "terasim-camcfg"
    net = map_info.get("sumo_net")
    if net is not None and map_info.get("osm_extract") is not None and cache in Path(net).parents:
        out["sumo_net"] = f"converted from {out['osm_extract']} with TeraSim's OpenStreetMap import"
    return out


def build_report(gen: Generation, calibration_rows: list) -> dict:
    inv = gen.inventory
    intersections = []
    for res in gen.results:
        it = res.intersection
        h = res.headings
        intersections.append(
            {
                "id": it.id,
                "name": it.name,
                "lat": it.lat,
                "lon": it.lon,
                "tls_id": res.tls_id,
                "tls_distance_m": res.tls_distance_m,
                "headings_source": h.source if h else None,
                "osm_junction": h.center if h else None,
                "streets": h.streets if h else [],
                "edges_per_heading": h.edges_per_heading if h else {},
                "approaches_osm": h.approaches if h else [],
                "exits_osm": h.exits if h else [],
                "approaches_sumo": res.sumo_approaches,
                "movements": len(h.movements()) if h else 0,
                "cameras": [
                    {
                        "sensor": cam.sensor,
                        "approach": cam.approach,
                        "feed": cam.test_feed or "camera",
                    }
                    for cam in it.cameras
                ],
            }
        )
    return {
        "inventory": inv.path.name,
        "corridor_id": inv.corridor["id"],
        "intersections_count": len(inv.intersections),
        "cameras_count": sum(len(it.cameras) for it in inv.intersections),
        "counting_cameras": sum(1 for _it, cam in inv.cameras() if cam.approach),
        "signals_bound": sum(1 for res in gen.results if res.tls_id),
        "map": _report_map(inv, gen.map_info),
        "road_graph": (
            _relative(inv, str(inv.resolve(inv.road_graph.get("osm"))))
            if inv.road_graph.get("osm")
            else None
        ),
        "intersections": intersections,
        "calibration": calibration_rows,
        "issues": [{"level": i.level, "where": i.where, "message": i.message} for i in gen.issues],
        "ready": not gen.errors,
    }


def render_report_md(gen: Generation, report: dict, file_names: Iterable[str] = ()) -> str:
    inv = gen.inventory
    lines = [
        f"# Corridor {report['corridor_id']}: generated configuration",
        "",
        f"{report['intersections_count']} intersections, {report['cameras_count']} cameras "
        f"({report['counting_cameras']} counting), {report['signals_bound']} signals bound in the SUMO map. "
        + ("Ready." if report["ready"] else f"NOT READY: {len(gen.errors)} error(s), see Issues."),
        "",
        f"- Inventory: `{inv.path.name}`",
        f"- TeraSim map (service path): `{gen.map_info['sumocfg']}`",
    ]
    if gen.map_info.get("map_id"):
        lines.append(
            f"- Map extract: `{Path(gen.map_info['osm_extract']).name}`, map id `{gen.map_info['map_id']}` (register-map.sh)"
        )
    lines.append(
        f"- Road graph for Headings: `{Path(report['road_graph']).name if report['road_graph'] else 'none (approximated from the SUMO map)'}`"
    )
    lines += [
        "",
        "## Intersections",
        "",
        "| Intersection | Signal (SUMO) | Approaches (OSM) | Approaches (SUMO) | Headings edges N/E/S/W | Cameras |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["intersections"]:
        counts = row["edges_per_heading"]
        cams = ", ".join(
            f"{c['sensor']}={SHORT[c['approach']] if c['approach'] else 'none'}"
            for c in row["cameras"]
        )
        tls = row["tls_id"] or "**not in the map**"
        if len(tls) > 40:
            tls = tls[:37] + "..."
        lines.append(
            f"| {row['id']} | `{tls}` | {'/'.join(SHORT[a] for a in row['approaches_osm']) or '-'} | "
            f"{'/'.join(SHORT[a] for a in row['approaches_sumo']) or '-'} | "
            f"{'/'.join(str(counts.get(h, 0)) for h in HEADINGS) if counts else '-'} | {cams} |"
        )
    lines += [
        "",
        "## Calibration",
        "",
        "| Camera | Intersection | Status | Source | Note |",
        "|---|---|---|---|---|",
    ]
    for row in report["calibration"]:
        lines.append(
            f"| {row['sensor']} | {row['intersection']} | {row['status']} | {row['source']} | {row['note']} |"
        )
    lines += ["", "## Issues", ""]
    if not gen.issues:
        lines.append("None.")
    for issue in gen.issues:
        lines.append(f"- **{issue.level}** {issue.where}: {issue.message}")
    lines += ["", "## Files", ""]
    for name in file_names:
        lines.append(f"- `{name}`")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# OpenStreetMap extract
# --------------------------------------------------------------------------------------


def osm_bbox(
    points: Iterable[tuple[float, float]], margin_m: float = OSM_MARGIN_M
) -> tuple[float, float, float, float]:
    """(south, west, north, east) around the points plus ``margin_m``, as TeraSim computes it."""
    points = list(points)
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    center = (min(lats) + max(lats)) / 2.0
    dlat = math.degrees(margin_m / _EARTH_RADIUS_M)
    dlon = math.degrees(margin_m / (_EARTH_RADIUS_M * math.cos(math.radians(center))))
    return min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon


def download_osm_extract(
    inv: Inventory, destination: Path, *, endpoint: str = OVERPASS_URL, timeout_s: float = 120.0
) -> Path:
    import urllib.parse  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    south, west, north, east = osm_bbox((it.lat, it.lon) for it in inv.intersections)
    bounds = f"{south},{west},{north},{east}"
    query = (
        f"[out:xml][timeout:{max(10, int(timeout_s * 0.75))}];"
        f'(way["highway"]({bounds});node["highway"="traffic_signals"]({bounds}););'
        "(._;>;);out body;"
    )
    data = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(
        endpoint, data=data, headers={"User-Agent": "TeraSim-City/0.1 map-cache"}
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        content = response.read()
    if b"</osm>" not in content[-200:]:
        raise RuntimeError("Overpass returned an incomplete extract; retry or raise the timeout")
    destination = Path(destination)
    destination.write_bytes(content)
    return destination


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def write_files(files: Mapping[str, str], out_dir: Path) -> None:
    out_dir = Path(out_dir)
    for name, text in files.items():
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if name.endswith(".sh"):
            path.chmod(0o755)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    gen_p = sub.add_parser("generate", help="write every config file of the corridor")
    gen_p.add_argument("inventory", type=Path)
    gen_p.add_argument("-o", "--out", type=Path, required=True, help="output directory")
    gen_p.add_argument(
        "--sumo-net",
        type=Path,
        help="override map.sumo_net: bind the signals in this local SUMO network "
        "(map.sumocfg / map.osm_extract still decide the path written for the service)",
    )
    gen_p.add_argument("--osm", type=Path, help="override road_graph.osm")
    gen_p.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    ext_p = sub.add_parser(
        "osm-extract", help="download the OpenStreetMap extract TeraSim would use for the corridor"
    )
    ext_p.add_argument("inventory", type=Path)
    ext_p.add_argument("-o", "--out", type=Path, required=True, help="the .osm file to write")
    ext_p.add_argument("--endpoint", default=OVERPASS_URL)
    args = parser.parse_args(argv)

    try:
        inv = load_inventory(args.inventory)
        if args.command == "osm-extract":
            path = download_osm_extract(inv, args.out, endpoint=args.endpoint)
            print(f"{path}: {path.stat().st_size} bytes, map id {file_sha256(path)[:MAP_ID_LEN]}")
            return 0
        if args.sumo_net:
            inv.map["sumo_net"] = str(args.sumo_net.resolve())
        if args.osm:
            inv.road_graph["osm"] = str(args.osm.resolve())
        gen = generate(inv, out_dir=args.out)
    except InventoryError as exc:
        print(f"camcfg: {exc}", file=sys.stderr)
        return 2
    write_files(gen.files, args.out)
    for issue in gen.issues:
        print(issue, file=sys.stderr)
    report = json.loads(gen.files["report.json"])
    print(
        f"{args.out}: corridor {report['corridor_id']}, {report['intersections_count']} intersections, "
        f"{report['cameras_count']} cameras, {report['signals_bound']} signals bound; "
        f"{len(gen.errors)} error(s), {len(gen.warnings)} warning(s)"
    )
    if gen.errors or (args.strict and gen.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
