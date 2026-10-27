"""Lanelet2 (.osm) provider: detection, junction collapsing, signals, driving side, coordinates.

Inputs are the hand-built fixtures of :mod:`tests.test_city.hd_map_fixtures`
(no customer or vendor map). Test names avoid the substrings ``sumo`` and
``simulation`` so the repo-root conftest does not auto-mark them; the tests
that shell out to ``netconvert`` check for the binary themselves.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from terasim_city.maps import GeoReferenceError, Lanelet2MapProvider, is_lanelet2_osm
from terasim_city.maps.lanelet2 import MAPPING_FILE_NAME, PROJECTOR_INFO_NAME

from tests.test_city.hd_map_fixtures import (
    LHT_ORIGIN,
    RHT_ORIGIN,
    tiny_lanelet2,
    two_lane_road_lanelet2,
)

requires_netconvert = pytest.mark.skipif(
    shutil.which("netconvert") is None, reason="netconvert not on PATH"
)

PLAIN_OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="terasim-test">
  <node id="1" lat="37.3350" lon="-121.9030"><tag k="note" v="lanelet"/></node>
  <node id="2" lat="37.3350" lon="-121.8970"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="secondary"/></way>
  <relation id="20"><member type="way" ref="10" role=""/><tag k="type" v="route"/></relation>
</osm>
"""

#: UTM zone 10 shifted so the fixture's local_x/local_y are small numbers.
LOCAL_UTM10 = ("+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs", 597_000.0, 4_131_000.0)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _bbox_contains(boundary, lat, lon) -> bool:
    lon_min, lat_min, lon_max, lat_max = boundary
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_lanelet_relations_identify_a_lanelet2_map(tmp_path):
    assert is_lanelet2_osm(_write(tmp_path / "ll2.osm", tiny_lanelet2()))
    # The value "lanelet" elsewhere (a tag, a route relation) is not enough.
    assert not is_lanelet2_osm(_write(tmp_path / "plain.osm", PLAIN_OSM))


# ---------------------------------------------------------------------------
# Conversion (netconvert)
# ---------------------------------------------------------------------------

@requires_netconvert
def test_junction_lanelets_collapse_into_one_signalized_junction(tmp_path):
    osm = _write(tmp_path / "t.osm", tiny_lanelet2())
    out = tmp_path / "map"
    pkg = Lanelet2MapProvider(osm).build(out)

    assert pkg.source == "lanelet2"
    assert pkg.geo_referenced is True
    assert _bbox_contains(pkg.metadata["orig_boundary"], *RHT_ORIGIN)
    summary = pkg.metadata["conversion"]
    # 6 approach/departure lanelets -> 6 edges; the 6 junction lanelets -> 1 junction.
    assert (summary["edges"], summary["intersections"], summary["traffic_lights"]) == (6, 1, 1)
    assert summary["tls_source"] == "lanelet2_traffic_lights"
    assert summary["largest_component_share"] == 1.0
    ll2 = summary["lanelet2"]
    assert ll2["lanelets_used"] == 12
    assert ll2["lanelets_skipped"] == {"subtype=crosswalk": 1}
    assert (ll2["junction_lanelets"], ll2["junction_clusters"]) == (6, 1)
    assert ll2["driving_side"] == "right"
    assert ll2["coordinate_source"] == "lat/lon"

    mapping = json.loads((out / MAPPING_FILE_NAME).read_text(encoding="utf-8"))
    assert len(mapping["edges"]) == 6
    assert list(mapping["junctions"]) == ["J0"] and len(mapping["junctions"]["J0"]) == 6


@requires_netconvert
def test_the_converted_junction_has_cardinal_approaches_and_turns(tmp_path):
    """What the corridor matcher sees: one junction, approaches, left/through/right."""
    import sumolib
    from terasim_city.flow.network.matcher import build_approaches

    pkg = Lanelet2MapProvider(_write(tmp_path / "t.osm", tiny_lanelet2())).build(tmp_path / "m")
    net = sumolib.net.readNet(str(pkg.net_file))
    junction = net.getNode("J0")
    assert junction.getType() == "traffic_light"
    approaches = build_approaches(net, junction)
    assert set(approaches) == {"EB", "WB", "NB"}
    assert set(approaches["EB"]["movements"]) == {"through", "right"}
    assert set(approaches["WB"]["movements"]) == {"through", "left"}
    assert set(approaches["NB"]["movements"]) == {"left", "right"}


@requires_netconvert
@pytest.mark.parametrize("style", ["interior", "approach"])
def test_signal_on_junction_or_approach_lanelets_signalizes_the_junction(tmp_path, style):
    osm = _write(tmp_path / f"{style}.osm", tiny_lanelet2(signal_style=style))
    summary = Lanelet2MapProvider(osm).build(tmp_path / "map").metadata["conversion"]
    assert summary["traffic_lights"] == 1
    assert summary["lanelet2"]["signalized_nodes"] == 1


@requires_netconvert
def test_left_hand_traffic_is_inferred_from_shared_bounds(tmp_path):
    import sumolib
    from terasim_city.flow.network.matcher import build_approaches

    pkg = Lanelet2MapProvider(_write(tmp_path / "lht.osm", tiny_lanelet2(lefthand=True))).build(
        tmp_path / "map"
    )
    summary = pkg.metadata["conversion"]
    assert summary["lanelet2"]["driving_side"] == "left"
    assert "--lefthand" in pkg.metadata["netconvert_flags"]
    assert "+zone=30" in summary["proj"]
    assert _bbox_contains(pkg.metadata["orig_boundary"], *LHT_ORIGIN)
    head = pkg.net_file.read_text(encoding="utf-8")[:2000]
    assert 'lefthand="true"' in head
    # Mirrored layout: the side road arrives from the north, and turning into
    # it from the eastbound approach is a LEFT turn under left-hand traffic.
    net = sumolib.net.readNet(str(pkg.net_file))
    approaches = build_approaches(net, net.getNode("J0"))
    assert set(approaches) == {"EB", "WB", "SB"}
    assert set(approaches["EB"]["movements"]) == {"through", "left"}


@requires_netconvert
def test_side_by_side_lanes_become_one_multi_lane_edge(tmp_path):
    out = tmp_path / "map"
    pkg = Lanelet2MapProvider(_write(tmp_path / "road.osm", two_lane_road_lanelet2())).build(out)
    summary = pkg.metadata["conversion"]
    assert (summary["edges"], summary["lanes"]) == (1, 2)
    mapping = json.loads((out / MAPPING_FILE_NAME).read_text(encoding="utf-8"))
    # Both lanes are chained across the split; lane 0 is the curb (right) lane.
    assert mapping["edges"] == {"ll201": [["201", "203"], ["200", "202"]]}
    assert any("driving side could not be inferred" in w for w in summary["warnings"])


# ---------------------------------------------------------------------------
# Coordinates without lat/lon
# ---------------------------------------------------------------------------

def test_local_coordinates_without_a_projection_fail_loud(tmp_path):
    osm = _write(tmp_path / "local.osm", tiny_lanelet2(latlon=False, local_frame=LOCAL_UTM10))
    with pytest.raises(GeoReferenceError, match=PROJECTOR_INFO_NAME):
        Lanelet2MapProvider(osm).build(tmp_path / "map")


def test_projection_override_needs_local_coordinates(tmp_path):
    osm = _write(tmp_path / "latlon_only.osm", tiny_lanelet2())
    with pytest.raises(GeoReferenceError, match="no local_x/local_y"):
        Lanelet2MapProvider(osm, proj=LOCAL_UTM10[0]).build(tmp_path / "map")


@requires_netconvert
def test_projection_override_places_local_coordinates(tmp_path):
    proj, x0, y0 = LOCAL_UTM10
    shifted = (
        f"+proj=tmerc +lat_0=0 +lon_0=-123 +k=0.9996 +x_0={500_000 - x0:.1f} "
        f"+y_0={-y0:.1f} +datum=WGS84 +units=m +no_defs"
    )
    osm = _write(tmp_path / "local.osm", tiny_lanelet2(latlon=False, local_frame=LOCAL_UTM10))
    pkg = Lanelet2MapProvider(osm, proj=shifted).build(tmp_path / "map")
    assert _bbox_contains(pkg.metadata["orig_boundary"], *RHT_ORIGIN)
    assert pkg.metadata["conversion"]["lanelet2"]["coordinate_source"] == (
        "local_x/local_y + proj override"
    )


@requires_netconvert
def test_autoware_mgrs_projector_info_places_local_coordinates(tmp_path):
    # The demo corridor lies in MGRS square 10SEG: UTM 10N with its SW corner at
    # (500 km E, 4 100 km N); Autoware stores local_x/local_y inside it.
    osm = _write(
        tmp_path / "mgrs" / "lanelet2_map.osm",
        tiny_lanelet2(
            latlon=False,
            local_frame=(LOCAL_UTM10[0], 500_000.0, 4_100_000.0),
        ),
    )
    _write(
        tmp_path / "mgrs" / PROJECTOR_INFO_NAME,
        "projector_type: MGRS\nvertical_datum: WGS84\nmgrs_grid: 10SEG\n",
    )
    pkg = Lanelet2MapProvider(osm).build(tmp_path / "map")
    assert _bbox_contains(pkg.metadata["orig_boundary"], *RHT_ORIGIN)
    assert PROJECTOR_INFO_NAME in pkg.metadata["conversion"]["lanelet2"]["coordinate_source"]
