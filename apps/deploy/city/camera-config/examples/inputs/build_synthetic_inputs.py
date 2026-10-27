#!/usr/bin/env python3
"""Write the synthetic inputs of the camera-configuration examples.

Every input of ``examples/`` is synthetic: generated here from a few constants, with
no real street, coordinate or camera data. Run it again to regenerate the files in place
(the output is deterministic):

  demo_arterial_road_graph.osm  OpenStreetMap road graph of the demo corridor of
                                ``examples/corridor_demo`` (the map baked into the image):
                                the same nodes as its SUMO network (W2 .. E2 on the
                                arterial, the cross streets J1/J2/J3 north and south), as
                                numeric OSM nodes, with the street names the demo uses.
  arterial_8.osm                an eight-signal arterial about 2.2 km north of the demo
                                corridor, used both as the TeraSim map (an OpenStreetMap
                                extract the service converts on upload) and as the road graph
                                VSS map-matches against. It exercises what real extracts
                                contain: one-way cross streets, a leg closed to cars (a
                                pedestrian mall), a plaza, and a same-named street piece
                                that is not connected to its junction.
  calibration.sample.json       a VA-API calibration file (``calibrationType: geo``) for the
                                four reference cameras cam1-cam4 of the eight-signal arterial
                                (intersection ``pine``), in the format VSS Studio exports.

Usage: python3 build_synthetic_inputs.py [-o OUT_DIR]   (standard library only)
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import quoteattr

HERE = Path(__file__).resolve().parent

#: Degrees per metre, the approximations examples/corridor_demo/build_demo_map.py uses.
M_PER_DEG_LAT = 110_950.0
M_PER_DEG_LON = 88_500.0


def _dlat(metres: float) -> float:
    return metres / M_PER_DEG_LAT


def _dlon(metres: float) -> float:
    return metres / M_PER_DEG_LON


class OsmWriter:
    """Collects nodes and ways and writes an OSM XML document (API 0.6 layout)."""

    def __init__(self) -> None:
        self.nodes: dict[int, tuple[float, float, dict]] = {}
        self.ways: list[tuple[int, list[int], dict]] = []

    def node(self, node_id: int, lat: float, lon: float, **tags: str) -> int:
        self.nodes[node_id] = (round(lat, 7), round(lon, 7), tags)
        return node_id

    def way(self, way_id: int, refs: list[int], **tags: str) -> None:
        self.ways.append((way_id, refs, tags))

    def write(self, path: Path, note: str) -> None:
        lats = [lat for lat, _lon, _tags in self.nodes.values()]
        lons = [lon for _lat, lon, _tags in self.nodes.values()]
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f"<!-- {note} -->",
            '<osm version="0.6" generator="build_synthetic_inputs.py">',
            f'  <bounds minlat="{min(lats):.7f}" minlon="{min(lons):.7f}" '
            f'maxlat="{max(lats):.7f}" maxlon="{max(lons):.7f}"/>',
        ]
        for node_id in sorted(self.nodes):
            lat, lon, tags = self.nodes[node_id]
            attrs = f'id="{node_id}" lat="{lat:.7f}" lon="{lon:.7f}" version="1" visible="true"'
            if tags:
                lines.append(f"  <node {attrs}>")
                lines += [f"    <tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in tags.items()]
                lines.append("  </node>")
            else:
                lines.append(f"  <node {attrs}/>")
        for way_id, refs, tags in sorted(self.ways):
            lines.append(f'  <way id="{way_id}" version="1" visible="true">')
            lines += [f'    <nd ref="{ref}"/>' for ref in refs]
            lines += [f"    <tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in sorted(tags.items())]
            lines.append("  </way>")
        lines.append("</osm>")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The demo corridor's road graph (examples/corridor_demo, signals J1, J2, J3)
# ---------------------------------------------------------------------------

DEMO_LAT = 37.3350
DEMO_J2_LON = -121.9000
DEMO_SPACING_M = 300.0
DEMO_CHAIN = ["W2", "W1", "J1", "J2", "J3", "E1", "E2"]
#: OSM node id of every node of the demo network (SUMO junction id -> OSM id).
DEMO_NODE_IDS = {
    "W2": 101, "W1": 102, "J1": 103, "J2": 104, "J3": 105, "E1": 106, "E2": 107,
    "J1_N1": 111, "J1_N2": 112, "J1_S1": 113, "J1_S2": 114,
    "J2_N1": 121, "J2_N2": 122, "J2_S1": 123, "J2_S2": 124,
    "J3_N1": 131, "J3_N2": 132, "J3_S1": 133, "J3_S2": 134,
}
DEMO_CROSS_STREETS = {"J1": "1st Cross St", "J2": "2nd Cross St", "J3": "3rd Cross St"}


def build_demo_road_graph(path: Path) -> None:
    osm = OsmWriter()
    for index, name in enumerate(DEMO_CHAIN):
        lon = DEMO_J2_LON + (index - 3) * _dlon(DEMO_SPACING_M)
        tags = {"highway": "traffic_signals"} if name.startswith("J") else {}
        osm.node(DEMO_NODE_IDS[name], DEMO_LAT, lon, **tags)
    for junction in ("J1", "J2", "J3"):
        lon = DEMO_J2_LON + (DEMO_CHAIN.index(junction) - 3) * _dlon(DEMO_SPACING_M)
        for step in (1, 2):
            osm.node(DEMO_NODE_IDS[f"{junction}_N{step}"], DEMO_LAT + step * _dlat(DEMO_SPACING_M), lon)
            osm.node(DEMO_NODE_IDS[f"{junction}_S{step}"], DEMO_LAT - step * _dlat(DEMO_SPACING_M), lon)
    osm.way(1, [DEMO_NODE_IDS[n] for n in DEMO_CHAIN],
            highway="secondary", name="Arterial Ave", lanes="4", maxspeed="50")
    for way_id, (junction, street) in enumerate(DEMO_CROSS_STREETS.items(), start=2):
        refs = [f"{junction}_S2", f"{junction}_S1", junction, f"{junction}_N1", f"{junction}_N2"]
        osm.way(way_id, [DEMO_NODE_IDS[n] for n in refs],
                highway="tertiary", name=street, lanes="2", maxspeed="40")
    osm.write(path, "Synthetic road graph of the demo corridor (examples/corridor_demo); not real map data.")


# ---------------------------------------------------------------------------
# 2. The eight-signal arterial (32-camera example)
# ---------------------------------------------------------------------------

ART_LAT = 37.3550
ART_WEST_LON = -121.9115          # the first signal
ART_SPACING_M = 250.0
LEG_MID_M = 80.0                  # a mid-block node on every cross-street leg
LEG_END_M = 220.0
#: Signal -> (cross street, north leg, south leg). Leg kinds: "two_way", "one_way_north",
#: "one_way_south" (the way runs through the junction), "pedestrian" (closed to cars),
#: "plaza" (a pedestrian plaza, then a same-named street piece not connected to it).
ART_SIGNALS = [
    ("oak", "Oak St", "two_way", "two_way"),
    ("elm", "Elm St", "pedestrian", "two_way"),
    ("pine", "Pine St", "two_way", "two_way"),
    ("cedar", "Cedar St", "one_way_north", "one_way_north"),
    ("maple", "Maple St", "one_way_south", "one_way_south"),
    ("birch", "Birch St", "one_way_north", "one_way_north"),
    ("ash", "Ash St", "one_way_south", "one_way_south"),
    ("walnut", "Walnut St", "two_way", "plaza"),
]


def art_signal_lon(index: int) -> float:
    return ART_WEST_LON + index * _dlon(ART_SPACING_M)


def build_arterial(path: Path) -> None:
    osm = OsmWriter()
    chain = [osm.node(1000, ART_LAT, art_signal_lon(-1))]
    for index in range(len(ART_SIGNALS)):
        chain.append(osm.node(1001 + index, ART_LAT, art_signal_lon(index), highway="traffic_signals"))
    chain.append(osm.node(1009, ART_LAT, art_signal_lon(len(ART_SIGNALS))))
    osm.way(10, chain, highway="secondary", name="Arterial Blvd", lanes="4", maxspeed="50")

    for index, (_sid, street, north, south) in enumerate(ART_SIGNALS):
        junction = 1001 + index
        base = 1000 + 100 * (index + 1)
        lon = art_signal_lon(index)
        way_id = 100 + 10 * (index + 1)
        street_tags = {"highway": "residential", "name": street, "maxspeed": "40"}

        def leg_node(offset: int, metres: float) -> int:
            return osm.node(base + offset, ART_LAT + _dlat(metres), lon)

        if north == south and north.startswith("one_way"):
            # One way through the junction, in the direction of travel.
            n1, n2 = leg_node(11, LEG_MID_M), leg_node(12, LEG_END_M)
            s1, s2 = leg_node(21, -LEG_MID_M), leg_node(22, -LEG_END_M)
            refs = [s2, s1, junction, n1, n2] if north == "one_way_north" else [n2, n1, junction, s1, s2]
            osm.way(way_id, refs, oneway="yes", lanes="1", **street_tags)
            continue
        for offset, kind, sign in ((1, north, 1.0), (2, south, -1.0)):
            node_base = 10 * offset
            if kind == "two_way":
                refs = [junction, leg_node(node_base + 1, sign * LEG_MID_M), leg_node(node_base + 2, sign * LEG_END_M)]
                osm.way(way_id + offset, refs, lanes="2", **street_tags)
            elif kind == "pedestrian":
                refs = [junction, leg_node(node_base + 1, sign * LEG_MID_M), leg_node(node_base + 2, sign * LEG_END_M)]
                osm.way(way_id + offset, refs, highway="pedestrian", name=street)
            elif kind == "plaza":
                # A pedestrian plaza next to the junction, then a residential piece of the
                # same street that starts beyond it: not connected to the junction by road.
                osm.way(way_id + offset, [junction, leg_node(31, sign * 60.0)],
                        highway="pedestrian", name=f"{street.split()[0]} Plaza")
                piece = [leg_node(32, sign * 75.0), leg_node(33, sign * 140.0), leg_node(34, sign * LEG_END_M)]
                osm.way(way_id + 5, piece, lanes="2", **{**street_tags, "name": f"S {street}"})
            else:  # pragma: no cover - table above
                raise ValueError(kind)
    osm.write(path, "Synthetic eight-signal arterial for the 32-camera example; not real map data.")


# ---------------------------------------------------------------------------
# 3. Calibration of the reference cameras cam1-cam4 at pine (VA-API format)
# ---------------------------------------------------------------------------

#: Camera -> (approach it counts, offset of the camera from the junction in metres
#: east/north, viewing direction in degrees). cam1 counts southbound traffic, so it sits
#: north of the junction and looks south; and so on.
REFERENCE_CAMERAS = {
    "cam1": ((8.0, 18.0), 180),
    "cam2": ((18.0, -8.0), 270),
    "cam3": ((-8.0, -18.0), 0),
    "cam4": ((-18.0, 8.0), 90),
}


def calibration_entry(sensor: str, intersection: str, lat: float, lon: float,
                      offset: tuple[float, float], direction: int) -> dict:
    east, north = offset
    cam_lat, cam_lon = lat + _dlat(north), lon + _dlon(east)
    # Four image points and the ground points they map to, along the camera's view.
    image = [{"x": 120.0 + 100.0 * i, "y": 400.0 - 80.0 * i} for i in range(4)]
    ground = []
    for i in range(4):
        reach = 10.0 + 10.0 * i
        d_east = {0: 0.0, 90: reach, 180: 0.0, 270: -reach}[direction]
        d_north = {0: reach, 90: 0.0, 180: -reach, 270: 0.0}[direction]
        ground.append({"x": round(cam_lon + _dlon(d_east), 7), "y": round(cam_lat + _dlat(d_north), 7)})
    roi = [ground[0], ground[3], {"x": round(lon, 7), "y": round(lat, 7)}]
    return {
        "id": sensor,
        "type": "camera",
        "imageCoordinates": image,
        "globalCoordinates": ground,
        "origin": {"lat": round(lat, 7), "lng": round(lon, 7)},
        "geoLocation": {"lat": round(cam_lat, 7), "lng": round(cam_lon, 7)},
        "rois": [{"id": f"{sensor}-roi-1", "roiCoordinates": roi}],
        "place": [{"name": "city", "value": "example"}, {"name": "intersection", "value": intersection}],
        "tripwires": [],
        "attributes": [
            {"name": "fps", "value": "15"},
            {"name": "direction", "value": str(direction)},
            {"name": "source", "value": "vst"},
        ],
        "coordinates": {"x": 0, "y": 0},
        "scaleFactor": 1,
    }


def calibration_document(intersection: str, lat: float, lon: float, sensors: dict) -> dict:
    return {
        "version": "1.0",
        "osmURL": "",
        "calibrationType": "geo",
        "sensors": [
            calibration_entry(sensor, intersection, lat, lon, offset, direction)
            for sensor, (offset, direction) in sensors.items()
        ],
    }


def build_calibration(path: Path) -> None:
    index = [sid for sid, *_rest in ART_SIGNALS].index("pine")
    document = calibration_document("pine", ART_LAT, art_signal_lon(index), REFERENCE_CAMERAS)
    path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")


#: The files main() writes.
OUTPUTS = ("demo_arterial_road_graph.osm", "arterial_8.osm", "calibration.sample.json")


def main(out_dir: Path = HERE) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    build_demo_road_graph(out_dir / OUTPUTS[0])
    build_arterial(out_dir / OUTPUTS[1])
    build_calibration(out_dir / OUTPUTS[2])
    return [out_dir / name for name in OUTPUTS]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-o", "--out", type=Path, default=HERE, help="output directory (default: this one)")
    for written in main(parser.parse_args().out):
        print(f"wrote {written}")
