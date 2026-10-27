"""camcfg.py: one camera inventory -> every corridor config file.

* The turn table and the approach aliases follow the TMC spec's conventions.
* Headings built from the OSM road graph VSS map-matches against reproduce the demo
  corridor's shipped table edge for edge, follow one-way streets and streets closed to
  cars, and ignore same-named street pieces not connected to the junction.
* The demo-corridor inventory regenerates the chart's shipped spec and scheduled
  configuration; the 32-camera inventory yields 32 distinct VST streams, a valid spec /
  scheduled configuration / calibration, and the signals the TeraSim service itself binds.
* Problems are reported: signals the map does not cover are errors, Headings without an
  OSM graph are flagged as approximate, invalid inventories are refused.

Every input is synthetic (apps/deploy/city/camera-config/examples/inputs).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CAMCFG_DIR = REPO_ROOT / "apps" / "deploy" / "city" / "camera-config"
EXAMPLES = CAMCFG_DIR / "examples"
INPUTS = EXAMPLES / "inputs"
DEMO_INVENTORY = EXAMPLES / "demo_arterial.yaml"
INVENTORY_32 = EXAMPLES / "arterial_32.yaml"
DEMO_ROAD_GRAPH = INPUTS / "demo_arterial_road_graph.osm"
ARTERIAL_EXTRACT = INPUTS / "arterial_8.osm"
EXAMPLE_CALIBRATION = INPUTS / "calibration.sample.json"
CHART_CONFIGS = REPO_ROOT / "apps" / "deploy" / "city" / "helm" / "terasim-phase1" / "configs"
CALIBRATION_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "camera_config" / "calibration.sample.json"

#: OSM node id of the demo road graph -> node name in the shipped spec's road-segment ids
#: ("w1-j1" is the segment from node W1 to junction J1 of examples/corridor_demo).
DEMO_NODE_NAMES = {
    101: "w2", 102: "w1", 103: "j1", 104: "j2", 105: "j3", 106: "e1", 107: "e2",
    111: "j1n1", 112: "j1n2", 113: "j1s1", 114: "j1s2",
    121: "j2n1", 122: "j2n2", 123: "j2s1", 124: "j2s2",
    131: "j3n1", 132: "j3n2", 133: "j3s1", 134: "j3s2",
}
ARTERIAL_IDS = ["oak", "elm", "pine", "cedar", "maple", "birch", "ash", "walnut"]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


camcfg = _load_module("camcfg", CAMCFG_DIR / "camcfg.py")

needs_sumolib = pytest.mark.skipif(
    importlib.util.find_spec("sumolib") is None, reason="sumolib is not installed"
)
needs_netconvert = pytest.mark.skipif(
    shutil.which("netconvert") is None, reason="netconvert is not installed"
)


def _demo_names(headings: dict) -> dict:
    """Road-segment ids of the demo road graph renamed to the shipped spec's node names."""
    return {
        "-".join(DEMO_NODE_NAMES[int(node)] for node in key.split("-")): heading
        for key, heading in headings.items()
    }


def _raw(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_inventory(tmp_path: Path, raw: dict, name: str = "inventory.yaml") -> Path:
    """An inventory in tmp_path whose relative input paths still resolve (inputs/ copied)."""
    if not (tmp_path / "inputs").exists():
        shutil.copytree(INPUTS, tmp_path / "inputs", ignore=shutil.ignore_patterns("*.py"))
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _absolute_map(raw: dict) -> dict:
    """The demo inventory's map, with its sumo_net as an absolute path (for copies elsewhere)."""
    raw["map"]["sumo_net"] = str((EXAMPLES / raw["map"]["sumo_net"]).resolve())
    return raw


def _arterial_point(intersection_id: str) -> tuple[float, float]:
    for item in _raw(INVENTORY_32)["intersections"]:
        if item["id"] == intersection_id:
            return item["lat"], item["lon"]
    raise KeyError(intersection_id)


@pytest.fixture(scope="module")
def demo_road_graph():
    return camcfg.load_osm_graph(DEMO_ROAD_GRAPH)


@pytest.fixture(scope="module")
def arterial_graph():
    return camcfg.load_osm_graph(ARTERIAL_EXTRACT)


@pytest.fixture(scope="module")
def chart_spec():
    return json.loads((CHART_CONFIGS / "Metadata.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def generated_32():
    if importlib.util.find_spec("sumolib") is None or shutil.which("netconvert") is None:
        pytest.skip("sumolib and netconvert are needed to build the corridor map")
    return camcfg.generate(camcfg.load_inventory(INVENTORY_32))


# --- conventions ---------------------------------------------------------------------


def test_turn_table_matches_the_spec_convention(chart_spec):
    """Headings are directions of travel: southbound turning right leaves westbound."""
    table = {
        f"{entry} - {exit_}": camcfg.movement(entry, exit_)
        for entry in camcfg.HEADINGS
        for exit_ in camcfg.HEADINGS
        if camcfg.movement(entry, exit_)
    }
    assert table == chart_spec["intersections"][0]["Movements"]
    assert camcfg.movement("North", "South") is None  # U-turns are no TMC movement


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("South", "South"),
        ("SB", "South"),
        ("sb", "South"),
        ("southbound", "South"),
        ("s", "South"),
        ("EB", "East"),
        ("none", None),
        (None, None),
        ("exit", None),
    ],
)
def test_approach_aliases(value, expected):
    assert camcfg.parse_approach(value) == expected


def test_unknown_approach_is_refused():
    with pytest.raises(ValueError, match="approach 'NE'"):
        camcfg.parse_approach("NE")


def test_street_names_compare_without_the_direction_prefix():
    assert camcfg.street_key("West Main Street") == camcfg.street_key("East Main Street")
    assert camcfg.street_key("S Walnut St") == camcfg.street_key("Walnut St") == "walnut st"
    assert camcfg.street_key("North 1st Street") == "1st street"
    assert camcfg.street_key("East Street") == "east street"  # a bare name keeps its first word


@pytest.mark.parametrize(
    ("terasim", "prefix"),
    [
        ({"release": "terasim-phase1"}, "terasim-phase1"),
        ({"release": "prod-terasim-phase1"}, "prod-terasim-phase1"),
        ({"release": "t"}, "t-terasim-phase1"),
        ({"release": "t", "fullname": "tsim"}, "tsim"),
    ],
)
def test_deployment_names_follow_the_chart_fullname_rule(terasim, prefix):
    assert camcfg.chart_fullname(terasim) == prefix


# --- inventory validation ------------------------------------------------------------

_INTERSECTION = {"id": "a", "name": "A", "lat": 37.335, "lon": -121.90339}


@pytest.mark.parametrize(
    ("cameras", "message"),
    [
        (
            [
                {"sensor": "cam1", "approach": "South", "test_feed": "cam1"},
                {"sensor": "cam1", "approach": "North", "test_feed": "cam3"},
            ],
            "sensor 'cam1' is already used",
        ),
        (
            [
                {"sensor": "cam1", "approach": "South", "test_feed": "cam1"},
                {"sensor": "cam2", "approach": "SB", "test_feed": "cam2"},
            ],
            "approach South is already counted by cam1",
        ),
        (
            [
                {
                    "sensor": "cam1",
                    "approach": "South",
                    "test_feed": "cam1",
                    "rtsp_url": "rtsp://h/s",
                }
            ],
            "exactly one of rtsp_url",
        ),
        ([{"sensor": "cam1", "approach": "South"}], "exactly one of rtsp_url"),
        ([{"sensor": "cam1", "approach": "up", "test_feed": "cam1"}], "approach 'up'"),
        ([{"sensor": "cam 1", "approach": "South", "test_feed": "cam1"}], "sensor is required"),
        (
            [{"sensor": "cam1", "approach": "South", "rtsp_url": "http://h/s"}],
            "rtsp_url must start with rtsp://",
        ),
        (
            [
                {
                    "sensor": "cam1",
                    "approach": "South",
                    "test_feed": "cam1",
                    "calibration": {"copy_from": "x.json"},
                }
            ],
            "copy_from: <calibration.json> with sensor",
        ),
    ],
    ids=[
        "duplicate_sensor",
        "duplicate_approach",
        "url_and_feed",
        "no_source",
        "bad_approach",
        "bad_name",
        "not_rtsp",
        "copy_without_sensor",
    ],
)
def test_invalid_inventories_are_refused(tmp_path, cameras, message):
    inventory = {
        "version": 1,
        "corridor": {"id": "c"},
        "intersections": [{**_INTERSECTION, "cameras": cameras}],
    }
    path = tmp_path / "inventory.yaml"
    path.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    with pytest.raises(camcfg.InventoryError, match=message):
        camcfg.load_inventory(path)


# --- Headings from the OSM road graph ------------------------------------------------


def test_osm_headings_reproduce_the_shipped_demo_tables(demo_road_graph, chart_spec):
    for shipped in chart_spec["intersections"]:
        result = camcfg.derive_headings_osm(demo_road_graph, shipped["lat"], shipped["lon"])
        assert DEMO_NODE_NAMES[int(result.center)] == shipped["intersection_id"].replace("i", "j")
        assert _demo_names(result.headings) == shipped["Headings"]
        assert result.movements() == shipped["Movements"]
        assert result.approaches == result.exits == ["North", "East", "South", "West"]
        assert not result.near_diagonal and not result.notes


@pytest.mark.parametrize(
    ("intersection_id", "approaches", "exits"),
    [
        ("maple", ["East", "South", "West"], ["East", "South", "West"]),  # one-way south
        ("birch", ["North", "East", "West"], ["North", "East", "West"]),  # one-way north
        # Elm St: the leg north of the junction is closed to cars (a T-junction for traffic)
        ("elm", ["North", "East", "West"], ["East", "South", "West"]),
        # Walnut St: the leg south of the junction is a plaza; a same-named residential
        # piece further south is not connected and must not count as an approach
        ("walnut", ["East", "South", "West"], ["North", "East", "West"]),
    ],
    ids=["one_way_south", "one_way_north", "t_junction", "disconnected_piece"],
)
def test_headings_follow_the_drivable_street_network(arterial_graph, intersection_id, approaches, exits):
    lat, lon = _arterial_point(intersection_id)
    result = camcfg.derive_headings_osm(arterial_graph, lat, lon)
    assert result.approaches == approaches
    assert result.exits == exits
    movements = result.movements()
    assert {path.split(" - ")[0] for path in movements} == set(approaches)
    assert {path.split(" - ")[1] for path in movements} <= set(exits)


def test_a_same_named_piece_beyond_a_plaza_is_left_out(arterial_graph):
    lat, lon = _arterial_point("walnut")
    result = camcfg.derive_headings_osm(arterial_graph, lat, lon)
    piece_nodes = {
        int(n) for way in arterial_graph.ways if way.name == "S Walnut St" for n in way.nodes
    }
    assert piece_nodes
    used = {int(n) for key in result.headings for n in key.split("-")}
    assert not used & piece_nodes
    # the piece lies within the Headings radius: only its missing link keeps it out
    nearest = min(camcfg.haversine_m(lat, lon, *arterial_graph.nodes[n]) for n in piece_nodes)
    assert nearest < camcfg.DEFAULT_HEADINGS_RADIUS_M


def test_osm_bbox_matches_the_service_download_area():
    from terasim_city.flow.descriptors import TMCData
    from terasim_service.city.map_acquisition import bounding_box_for_tmc

    points = [_arterial_point(i) for i in ("oak", "pine", "walnut")]
    payload = {
        "corridor_id": "c",
        "observation_interval": "10m",
        "timestamp_start": "2026-10-20T00:00:00Z",
        "intersections": [
            {
                "intersection_id": f"i{n}",
                "lat": lat,
                "lon": lon,
                "counts": {"East": {"Straight": 1}},
            }
            for n, (lat, lon) in enumerate(points)
        ],
    }
    box = bounding_box_for_tmc(TMCData.from_payload(payload, source_path="<test>"))
    assert camcfg.osm_bbox(points) == pytest.approx((box.south, box.west, box.north, box.east))


def test_the_synthetic_inputs_are_reproducible(tmp_path):
    builder = _load_module("build_synthetic_inputs", INPUTS / "build_synthetic_inputs.py")
    for written in builder.main(tmp_path):
        assert written.read_bytes() == (INPUTS / written.name).read_bytes(), written.name


# --- generate: the demo corridor -----------------------------------------------------


@needs_sumolib
def test_demo_inventory_regenerates_the_chart_files(chart_spec):
    from terasim_service.city.scheduler import load_scheduled_config
    from terasim_tmc_service.spec import CorridorSpec

    gen = camcfg.generate(camcfg.load_inventory(DEMO_INVENTORY))
    assert gen.issues == []
    spec = json.loads(gen.files["terasim/Metadata.json"])
    assert (spec["corridor_id"], spec["observation_interval"]) == (
        chart_spec["corridor_id"],
        chart_spec["observation_interval"],
    )
    for generated, shipped in zip(spec["intersections"], chart_spec["intersections"], strict=True):
        assert generated["intersection_id"] == shipped["intersection_id"]
        assert (generated["lat"], generated["lon"]) == (shipped["lat"], shipped["lon"])
        assert generated["Assignments"] == shipped["Assignments"]
        assert generated["Movements"] == shipped["Movements"]
        # the same table; only the ids are the synthetic road graph's OSM node pairs
        assert _demo_names(generated["Headings"]) == shipped["Headings"]
    assert CorridorSpec.from_payload(spec).get("i1").sensors == CorridorSpec.from_payload(
        chart_spec
    ).get("i1").sensors

    shipped = load_scheduled_config(CHART_CONFIGS / "scheduled_config.yaml")["demo_arterial"]
    entry = yaml.safe_load(gen.files["terasim/scheduled_config.yaml"])["corridors"]["demo_arterial"]
    assert entry == yaml.safe_load(
        (CHART_CONFIGS / "scheduled_config.yaml").read_text(encoding="utf-8")
    )["corridors"]["demo_arterial"]
    for key in ("base_sumocfg", "corridor", "sim_end", "max_trials", "n_workers", "patience"):
        assert entry[key] == shipped[key], key


@needs_sumolib
def test_live_cameras_are_registered_as_given_and_need_no_fanout(tmp_path):
    raw = _absolute_map(_raw(DEMO_INVENTORY))
    for n, cam in enumerate(raw["intersections"][0]["cameras"], start=1):
        cam["rtsp_url"] = f"rtsp://viewer:secret@192.0.2.{10 + n}:554/stream1"
    gen = camcfg.generate(camcfg.load_inventory(_write_inventory(tmp_path, raw)))
    assert gen.issues == []
    streams = json.loads(gen.files["vss/vst-streams.json"])
    assert streams[0] == {
        "name": "i1-cam1",
        "sensorUrl": "rtsp://viewer:secret@192.0.2.11:554/stream1",
    }
    assert len(streams) == 12
    assert "vss/rtsp-fanout.yaml" not in gen.files
    todo = json.loads(gen.files["vss/calibration-todo.json"])
    assert [s["id"] for s in todo["sensors"]][:4] == ["i1-cam1", "i1-cam2", "i1-cam3", "i1-cam4"]
    assert len(todo["sensors"]) == 12
    assert "vss/calibration.json" not in gen.files


@needs_sumolib
def test_calibration_copied_to_another_intersection_says_where_its_points_are(tmp_path):
    raw = _absolute_map(_raw(DEMO_INVENTORY))
    shutil.copy(CALIBRATION_FIXTURE, tmp_path / "calibration.i1.json")
    for item in raw["intersections"][:2]:  # i1 copies its own, i2 copies i1's
        for cam in item["cameras"]:
            cam["calibration"] = {
                "copy_from": "calibration.i1.json",
                "sensor": "i1-" + cam["sensor"].split("-")[1],
            }
    gen = camcfg.generate(camcfg.load_inventory(_write_inventory(tmp_path, raw)))
    assert not gen.errors
    upload = json.loads(gen.files["vss/calibration.json"])
    places = {s["id"]: {p["name"]: p["value"] for p in s["place"]} for s in upload["sensors"]}
    assert places["i1-cam1"]["intersection"] == "i1" and places["i2-cam1"]["intersection"] == "i2"
    fixture = {
        s["id"]: s for s in json.loads(CALIBRATION_FIXTURE.read_text(encoding="utf-8"))["sensors"]
    }
    i2cam1 = next(s for s in upload["sensors"] if s["id"] == "i2-cam1")
    assert i2cam1["globalCoordinates"] == fixture["i1-cam1"]["globalCoordinates"]  # not moved
    rows = {r["sensor"]: r for r in json.loads(gen.files["report.json"])["calibration"]}
    assert rows["i1-cam1"]["status"] == "copied"
    assert rows["i2-cam1"]["status"] == "copied from another intersection"
    assert "detections are placed at i1, not at i2" in rows["i2-cam1"]["note"]
    assert len(json.loads(gen.files["vss/calibration-todo.json"])["sensors"]) == 4  # i3


@needs_sumolib
def test_command_line_exit_codes(tmp_path):
    assert camcfg.main(["generate", str(DEMO_INVENTORY), "-o", str(tmp_path / "ok")]) == 0
    assert (tmp_path / "ok" / "terasim" / "Metadata.json").is_file()
    assert (tmp_path / "ok" / "vss" / "register-vst-streams.sh").stat().st_mode & 0o111
    assert not (tmp_path / "ok" / "console").exists()
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\ncorridor: {id: c}\nintersections: []\n", encoding="utf-8")
    assert camcfg.main(["generate", str(bad), "-o", str(tmp_path / "bad")]) == 2


def test_osm_extract_uses_the_service_query(tmp_path, monkeypatch):
    import urllib.request

    seen = {}

    class _Response:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen["data"] = request.data.decode()
        seen["url"] = request.full_url
        return _Response(b'<?xml version="1.0"?><osm version="0.6"></osm>')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    inventory = camcfg.load_inventory(DEMO_INVENTORY)
    out = camcfg.download_osm_extract(
        inventory, tmp_path / "x.osm", endpoint="https://overpass.example/api/interpreter"
    )
    assert out.read_bytes().endswith(b"</osm>")
    assert seen["url"] == "https://overpass.example/api/interpreter"
    assert "way%5B%22highway%22%5D" in seen["data"] and "traffic_signals" in seen["data"]


# --- generate: the 32-camera corridor ------------------------------------------------


def test_32_cameras_give_32_distinct_vst_streams(generated_32):
    assert generated_32.issues == []
    streams = json.loads(generated_32.files["vss/vst-streams.json"])
    assert [s["name"] for s in streams] == [
        cam.sensor for _it, cam in generated_32.inventory.cameras()
    ]
    assert (
        len(streams)
        == len({s["name"] for s in streams})
        == len({s["sensorUrl"] for s in streams})
        == 32
    )
    by_name = {s["name"]: s["sensorUrl"] for s in streams}
    # cam1-cam4 keep their own recorded-feed pods, the others go through the fan-out
    assert by_name["cam1"] == "rtsp://rtsp-test-source-cam1.vss.svc.cluster.local:8554/test"
    assert by_name["cam9"] == "rtsp://rtsp-fanout.vss.svc.cluster.local:8554/cam9"


def test_32_camera_spec_and_scheduled_configuration_are_accepted(generated_32, tmp_path):
    from terasim_service.city.scheduler import load_scheduled_config
    from terasim_tmc_service.spec import CorridorSpec

    spec = CorridorSpec.from_payload(json.loads(generated_32.files["terasim/Metadata.json"]))
    assert spec.ids() == ARTERIAL_IDS
    assert sum(len(spec.get(i).sensors) for i in spec.ids()) == 26
    none = sorted(
        (cam.sensor for _it, cam in generated_32.inventory.cameras() if cam.approach is None),
        key=lambda s: int(s[3:]),
    )
    assert none == ["cam9", "cam13", "cam19", "cam21", "cam27", "cam31"]
    path = tmp_path / "scheduled_config.yaml"
    path.write_text(generated_32.files["terasim/scheduled_config.yaml"], encoding="utf-8")
    entry = load_scheduled_config(path)["arterial_blvd"]
    map_id = hashlib.sha256(ARTERIAL_EXTRACT.read_bytes()).hexdigest()[:12]
    assert entry["base_sumocfg"] == f"/data/output/store/maps/{map_id}/base.sumocfg"
    assert len(entry["corridor"]["tls_ids"]) == 8


def test_32_camera_map_is_the_one_the_service_stores(generated_32, tmp_path):
    """register-map.sh uploads the extract; the service's map store converts it to the map
    id scheduled_config.yaml names, with the same eight signals."""
    from terasim_city.maps.store import MapStore

    stored = MapStore(tmp_path / "store").ingest(ARTERIAL_EXTRACT)
    assert stored.map_id == generated_32.map_info["map_id"]
    assert stored.summary["traffic_lights"] == 8 and not stored.summary["warnings"]
    import sumolib

    net = sumolib.net.readNet(str(stored.package.net_file))
    assert {t.getID() for t in net.getTrafficLights()} == {
        r.tls_id for r in generated_32.results
    }


def test_32_camera_signals_are_the_ones_the_service_binds(generated_32):
    import sumolib
    from terasim_city.binding import node_to_tls_map, signal_candidates

    net = sumolib.net.readNet(str(generated_32.map_info["sumo_net"]))
    mapping = node_to_tls_map(net)
    for result in generated_32.results:
        it = result.intersection
        assert result.tls_id == signal_candidates(net, mapping, it.lat, it.lon)[0].tls_id, it.id
        assert set(result.sumo_approaches) == set(result.headings.approaches), it.id


def test_32_camera_calibration_copies_say_where_their_points_are(generated_32):
    upload = json.loads(generated_32.files["vss/calibration.json"])
    assert [s["id"] for s in upload["sensors"]] == [
        cam.sensor for _it, cam in generated_32.inventory.cameras()
    ]
    places = {s["id"]: {p["name"]: p["value"] for p in s["place"]} for s in upload["sensors"]}
    assert places["cam5"]["intersection"] == "oak" and places["cam1"]["intersection"] == "pine"
    source = {
        s["id"]: s for s in json.loads(EXAMPLE_CALIBRATION.read_text(encoding="utf-8"))["sensors"]
    }
    cam5 = next(s for s in upload["sensors"] if s["id"] == "cam5")
    assert cam5["globalCoordinates"] == source["cam1"]["globalCoordinates"]  # reused, not moved
    rows = json.loads(generated_32.files["report.json"])["calibration"]
    assert sum(r["status"] == "copied" for r in rows) == 4
    assert sum(r["status"] == "copied from another intersection" for r in rows) == 28
    assert "vss/calibration-todo.json" not in generated_32.files


def test_32_camera_fanout_manifest(generated_32):
    docs = list(yaml.safe_load_all(generated_32.files["vss/rtsp-fanout.yaml"]))
    assert [d["kind"] for d in docs] == ["ConfigMap", "Deployment", "Service"]
    config = yaml.safe_load(docs[0]["data"]["mediamtx.yml"])
    assert len(config["paths"]) == 28
    assert (
        config["paths"]["cam9"]["source"]
        == "rtsp://rtsp-test-source-cam1.vss.svc.cluster.local:8554/test"
    )
    assert all(p["sourceOnDemand"] is False for p in config["paths"].values())


def test_32_camera_values_file_carries_the_same_files(generated_32):
    values = yaml.safe_load(generated_32.files["terasim/values-corridor.yaml"])["corridorConfig"]
    assert json.loads(values["metadataJson"]) == json.loads(
        generated_32.files["terasim/Metadata.json"]
    )
    assert yaml.safe_load(values["scheduledConfigYaml"]) == yaml.safe_load(
        generated_32.files["terasim/scheduled_config.yaml"]
    )
    configmap_text = generated_32.files["terasim/corridor-configmap.yaml"]
    configmap = yaml.safe_load(configmap_text)
    assert configmap["metadata"]["name"] == "arterial-blvd-corridor"
    assert json.loads(configmap["data"]["Metadata.json"]) == json.loads(values["metadataJson"])
    # the restart hint names the chart's Deployments for the default release
    assert "deploy/terasim-phase1-service deploy/terasim-phase1-tmc" in configmap_text


def test_generated_scripts_are_valid_bash_and_name_the_chart_deployments(generated_32, tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    scripts = [name for name in generated_32.files if name.endswith(".sh")]
    assert sorted(scripts) == [
        "terasim/register-intersections.sh",
        "terasim/register-map.sh",
        "vss/register-vst-streams.sh",
    ]
    for name in scripts:
        path = tmp_path / Path(name).name
        path.write_text(generated_32.files[name], encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, (name, result.stderr)
    assert 'TMC="${TMC:-deploy/terasim-phase1-tmc}"' in generated_32.files[
        "terasim/register-intersections.sh"
    ]
    assert 'SVC="${SVC:-deploy/terasim-phase1-service}"' in generated_32.files[
        "terasim/register-map.sh"
    ]
    assert 'TNS="${TNS:-terasim}"' in generated_32.files["terasim/register-map.sh"]


def test_32_camera_generation_writes_no_console_files(generated_32):
    assert not [name for name in generated_32.files if name.startswith("console/")]


# --- errors ------------------------------------------------------------------------


def _west_part_of_the_extract(path: Path) -> Path:
    """The arterial extract cut just east of Birch St, without the leg north of Oak St:
    a map that does not cover Ash St and Walnut St and holds two junctions only in part."""
    root = ET.parse(ARTERIAL_EXTRACT).getroot()
    birch_lon = _arterial_point("birch")[1]
    dropped = {"1111", "1112"}  # the leg north of Oak St
    keep = set()
    for node in list(root.findall("node")):
        if float(node.get("lon")) > birch_lon + 1e-6 or node.get("id") in dropped:
            root.remove(node)
        else:
            keep.add(node.get("id"))
    for way in list(root.findall("way")):
        for nd in list(way.findall("nd")):
            if nd.get("ref") not in keep:
                way.remove(nd)
        if len(way.findall("nd")) < 2:
            root.remove(way)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


@needs_sumolib
@needs_netconvert
def test_signals_outside_the_map_are_errors(tmp_path):
    raw = _raw(INVENTORY_32)
    raw["map"] = {"osm_extract": str(_west_part_of_the_extract(tmp_path / "west.osm"))}
    gen = camcfg.generate(camcfg.load_inventory(_write_inventory(tmp_path, raw)))
    # The map ends just east of Birch St: Ash St and Walnut St are not in it, Birch St only
    # as a signal at the map's edge, Oak St without its north leg.
    failed = sorted(issue.where for issue in gen.errors)
    assert failed == ["intersection ash", "intersection walnut"]
    assert all("does not cover this signal" in issue.message for issue in gen.errors)
    partial = sorted(
        issue.where for issue in gen.warnings if "holds only part of this junction" in issue.message
    )
    assert partial == ["intersection birch", "intersection oak"]
    entry = yaml.safe_load(gen.files["terasim/scheduled_config.yaml"])["corridors"]["arterial_blvd"]
    assert len(entry["corridor"]["tls_ids"]) == 6
    assert not json.loads(gen.files["report.json"])["ready"]


@needs_sumolib
@needs_netconvert
def test_without_a_road_graph_headings_are_approximated_and_flagged(tmp_path):
    raw = _raw(INVENTORY_32)
    raw.pop("road_graph")
    gen = camcfg.generate(camcfg.load_inventory(_write_inventory(tmp_path, raw)))
    assert not gen.errors
    messages = " ".join(issue.message for issue in gen.warnings)
    assert "no OSM road graph given" in messages and "approximate" in messages
    intersections = json.loads(gen.files["terasim/Metadata.json"])["intersections"]
    assert all(i["Headings"] and i["Movements"] for i in intersections)
    assert {row["headings_source"] for row in json.loads(gen.files["report.json"])["intersections"]} == {
        "sumo"
    }


@needs_sumolib
@needs_netconvert
def test_a_camera_on_an_approach_without_traffic_never_breaks_the_collector(tmp_path):
    """Warned about, and still safe: its heading gets turn rows, so the collector's
    per-heading lookup finds them (it fails the whole tick on a missing one)."""
    from terasim_tmc_service.generator import build_ftmc
    from terasim_tmc_service.registry import RegisteredIntersection
    from terasim_tmc_service.spec import CorridorSpec

    raw = _raw(INVENTORY_32)
    # Maple St is one-way southbound: nothing enters it northbound
    (maple,) = [item for item in raw["intersections"] if item["id"] == "maple"]
    maple["cameras"] = [
        {"sensor": "cam17", "approach": "South", "test_feed": "cam1"},
        {"sensor": "cam19", "approach": "North", "test_feed": "cam3"},
    ]
    raw["intersections"] = [maple]
    gen = camcfg.generate(camcfg.load_inventory(_write_inventory(tmp_path, raw)))
    assert any("approach North" in i.message and i.where.endswith("cam19") for i in gen.warnings)
    spec = CorridorSpec.from_payload(json.loads(gen.files["terasim/Metadata.json"]))
    it = spec.get("maple")
    assert {path.split(" - ")[0] for path in it.movements} >= {"South", "North"}
    record = RegisteredIntersection(
        intersection_id="maple",
        name="Maple St & Arterial Blvd",
        lat=it.lat,
        lon=it.lon,
        corridor_id=spec.corridor_id,
        sensors=it.sensors,
        registered_at="",
    )
    generated = build_ftmc(spec, [record], {}, timestamp_start="2026-10-20T00:00:00Z")
    counts = generated.payload["intersections"][0]["counts"]
    assert sum(counts["North"].values()) == 0
