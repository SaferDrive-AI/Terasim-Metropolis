"""Hand-built, minimal HD-map inputs for the OpenDRIVE / Lanelet2 provider tests.

Nothing here comes from a customer or vendor map: both builders write a
three-arm signalized junction (a two-way main road crossed by a two-way side
road from the south) from first principles, in a few kilometres-free metres.
"""

from __future__ import annotations

import math

#: Junction centre of the right-hand-traffic fixtures (the demo corridor's
#: centre signal J2; UTM zone 10).
RHT_ORIGIN = (37.3350, -121.9000)
#: Junction centre of the left-hand-traffic fixtures (a left-hand-traffic
#: location in UTM zone 30).
LHT_ORIGIN = (51.5000, -0.1200)
#: UTM zone 10 easting/northing of the absolute-coordinate OpenDRIVE fixture.
XODR_UTM_ORIGIN = (597_000.0, 4_130_000.0)
UTM10 = "+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs"


# ---------------------------------------------------------------------------
# OpenDRIVE
# ---------------------------------------------------------------------------

def _xodr_lanes(both: bool, speed_kmh: float, width: float = 3.5) -> str:
    # Lane-level <speed>: netconvert 1.23 ignores the road-level <type><speed>.
    body = (
        f'<link/><width sOffset="0" a="{width}" b="0" c="0" d="0"/>'
        f'<speed sOffset="0" max="{speed_kmh}" unit="km/h"/>'
    )
    left = f'<left><lane id="1" type="driving" level="false">{body}</lane></left>' if both else ""
    return (
        f'<lanes><laneSection s="0">{left}'
        '<center><lane id="0" type="none" level="false"/></center>'
        f'<right><lane id="-1" type="driving" level="false">{body}</lane></right>'
        "</laneSection></lanes>"
    )


def _xodr_road(rid, x, y, hdg, length, *, junction="-1", link="", curvature=None,
               both=True, signal=None, speed_kmh=50) -> str:
    geometry = "<line/>" if curvature is None else f'<arc curvature="{curvature}"/>'
    signals = ""
    if signal is not None:
        s, t, sid, orientation = signal
        signals = (
            f'<signals><signal s="{s}" t="{t}" id="{sid}" name="{sid}" dynamic="yes" '
            f'orientation="{orientation}" zOffset="5" country="OpenDRIVE" type="1000001" '
            'subtype="-1" value="-1"/></signals>'
        )
    return (
        f'<road name="r{rid}" length="{length:.6f}" id="{rid}" junction="{junction}">'
        f'<link>{link}</link><type s="0" type="town"><speed max="{speed_kmh}" unit="km/h"/></type>'
        f'<planView><geometry s="0" x="{x:.4f}" y="{y:.4f}" hdg="{hdg:.12f}" '
        f'length="{length:.6f}">{geometry}</geometry></planView>'
        f"{_xodr_lanes(both, speed_kmh)}{signals}</road>"
    )


def tiny_opendrive(*, geo_reference: str | None = UTM10, origin=XODR_UTM_ORIGIN,
                   signals: bool = True, speed_kmh: float = 50) -> str:
    """A three-arm junction: west/east arms (two-way) and a south arm, 90 m each.

    ``origin`` is the junction centre in the file's x/y frame — UTM metres by
    default, ``(0, 0)`` for a local-coordinate variant. ``geo_reference=None``
    omits the ``<geoReference>`` header (the vendor pitfall). With ``signals``
    each approach carries an OpenDRIVE traffic light (type 1000001).
    ``speed_kmh`` is written as lane-level ``<speed>`` (netconvert ignores the
    road-level type speed).
    """
    x0, y0 = origin
    quarter = math.pi / 2 * 10.0  # 90° arc of radius 10 m

    def pred(road, contact):
        return f'<predecessor elementType="road" elementId="{road}" contactPoint="{contact}"/>'

    def succ(road, contact):
        return f'<successor elementType="road" elementId="{road}" contactPoint="{contact}"/>'

    to_junction = '<successor elementType="junction" elementId="100"/>'
    from_junction = '<predecessor elementType="junction" elementId="100"/>'
    road = lambda *a, **k: _xodr_road(*a, speed_kmh=speed_kmh, **k)  # noqa: E731
    roads = [
        road(1, x0 - 100, y0, 0.0, 90, link=to_junction,
                   signal=(85, -5, "s1", "+") if signals else None),
        road(2, x0 + 10, y0, 0.0, 90, link=from_junction,
                   signal=(5, 5, "s2", "-") if signals else None),
        road(3, x0, y0 - 100, math.pi / 2, 90, link=to_junction,
                   signal=(85, -5, "s3", "+") if signals else None),
        road(10, x0 - 10, y0, 0.0, 20, junction="100", link=pred(1, "end") + succ(2, "start")),
        road(11, x0, y0 - 10, math.pi / 2, quarter, junction="100",
                   link=pred(3, "end") + succ(2, "start"), curvature=-0.1, both=False),
        road(12, x0, y0 - 10, math.pi / 2, quarter, junction="100",
                   link=pred(3, "end") + succ(1, "end"), curvature=0.1, both=False),
        road(13, x0 - 10, y0, 0.0, quarter, junction="100",
                   link=pred(1, "end") + succ(3, "end"), curvature=-0.1, both=False),
        road(14, x0 + 10, y0, math.pi, quarter, junction="100",
                   link=pred(2, "start") + succ(3, "end"), curvature=0.1, both=False),
    ]
    links = [  # (incoming road, connecting road, contact point, from lane, to lane)
        (1, 10, "start", -1, -1), (2, 10, "end", 1, 1), (3, 11, "start", -1, -1),
        (3, 12, "start", -1, -1), (1, 13, "start", -1, -1), (2, 14, "start", 1, -1),
    ]
    junction = '<junction id="100" name="J">' + "".join(
        f'<connection id="{i}" incomingRoad="{inc}" connectingRoad="{con}" contactPoint="{cp}">'
        f'<laneLink from="{fl}" to="{tl}"/></connection>'
        for i, (inc, con, cp, fl, tl) in enumerate(links)
    ) + "</junction>"
    georef = f"<geoReference><![CDATA[{geo_reference}]]></geoReference>" if geo_reference else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OpenDRIVE><header revMajor="1" revMinor="6" name="tiny" version="1.0">'
        f"{georef}</header>" + "".join(roads) + junction + "</OpenDRIVE>\n"
    )


# ---------------------------------------------------------------------------
# Lanelet2
# ---------------------------------------------------------------------------

def _arc(cx, cy, r, a0, a1, n=8):
    return [
        (cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n))
        for i in range(n + 1)
    ]


def _t_junction_lanelets():
    """Right-hand-traffic T junction in metres: {name: (left_pts, right_pts, tags)}.

    Main road along y=0 (eastbound south of it), side road along x=0 from the
    south (northbound east of it); junction box |x|,|y| <= 10, lanes 3.5 m.
    Bounds are listed in driving direction, left/right as seen by the driver.
    """
    w = 3.5
    h = math.pi / 2
    lanes = {
        # approaches / departures, 90 m long
        "EB_in": ([(-100, 0), (-10, 0)], [(-100, -w), (-10, -w)]),
        "WB_out": ([(-10, 0), (-100, 0)], [(-10, w), (-100, w)]),
        "EB_out": ([(10, 0), (100, 0)], [(10, -w), (100, -w)]),
        "WB_in": ([(100, 0), (10, 0)], [(100, w), (10, w)]),
        "NB_in": ([(0, -100), (0, -10)], [(w, -100), (w, -10)]),
        "SB_out": ([(0, -10), (0, -100)], [(-w, -10), (-w, -100)]),
        # junction interior
        "EB_through": ([(-10, 0), (10, 0)], [(-10, -w), (10, -w)]),
        "WB_through": ([(10, 0), (-10, 0)], [(10, w), (-10, w)]),
        "EB_right": (_arc(-10, -10, 10, h, 0), _arc(-10, -10, 10 - w, h, 0)),
        "NB_left": (_arc(-10, -10, 10, 0, h), _arc(-10, -10, 10 + w, 0, h)),
        "NB_right": (_arc(10, -10, 10, math.pi, h), _arc(10, -10, 10 - w, math.pi, h)),
        "WB_left": (_arc(10, -10, 10, h, math.pi), _arc(10, -10, 10 + w, h, math.pi)),
    }
    return lanes


def tiny_lanelet2(*, lefthand: bool = False, latlon: bool = True, local_frame=None,
                  signal_style: str = "both", extra_crosswalk: bool = True) -> str:
    """A Lanelet2 T junction (12 road lanelets) with one traffic light.

    * ``lefthand`` mirrors the layout (y -> -y and left/right swapped), giving a
      left-hand-traffic map whose opposite lanes share their RIGHT bounds.
    * ``latlon=False`` writes ``lat=0 lon=0`` (Autoware maps without GPS);
      ``local_frame`` = ``(proj4, x_offset, y_offset)`` adds ``local_x``/``local_y``
      tags: the node's coordinates in that projection minus the offsets.
    * ``signal_style``: ``"interior"`` (Autoware: the light hangs on the junction
      lanelets), ``"approach"`` (on the approach lanelets) or ``"both"``.
    * ``extra_crosswalk`` adds a crosswalk lanelet across the side road that the
      converter must ignore.
    """
    lat0, lon0 = LHT_ORIGIN if lefthand else RHT_ORIGIN
    ky = 111_195.0
    kx = ky * math.cos(math.radians(lat0))
    lanes = _t_junction_lanelets()
    if extra_crosswalk:
        lanes["crosswalk"] = ([(-6, -30), (6, -30)], [(-6, -33), (6, -33)])
    if lefthand:
        lanes = {
            name: ([(x, -y) for x, y in right], [(x, -y) for x, y in left])
            for name, (left, right) in lanes.items()
        }

    transformer = None
    if local_frame is not None:
        from pyproj import CRS, Transformer

        transformer = Transformer.from_crs(
            CRS.from_epsg(4326), CRS.from_proj4(local_frame[0]), always_xy=True
        )

    node_ids: dict[tuple[float, float], int] = {}
    node_xml: list[str] = []

    def node(p) -> int:
        key = (round(p[0], 3), round(p[1], 3))
        if key not in node_ids:
            nid = len(node_ids) + 1
            node_ids[key] = nid
            lat = lat0 + key[1] / ky
            lon = lon0 + key[0] / kx
            tags = '<tag k="ele" v="10.0"/>'
            if transformer is not None:
                ex, ny = transformer.transform(lon, lat)
                tags += (
                    f'<tag k="local_x" v="{ex - local_frame[1]:.4f}"/>'
                    f'<tag k="local_y" v="{ny - local_frame[2]:.4f}"/>'
                )
            lat_s, lon_s = (f"{lat:.10f}", f"{lon:.10f}") if latlon else ("0", "0")
            node_xml.append(f'  <node id="{nid}" lat="{lat_s}" lon="{lon_s}">{tags}</node>')
        return node_ids[key]

    way_ids: dict[tuple[int, ...], int] = {}
    way_xml: list[str] = []

    def way(points) -> int:
        refs = tuple(node(p) for p in points)
        # A bound shared with a neighbour is ONE way (possibly referenced reversed).
        key = refs if refs[0] <= refs[-1] else refs[::-1]
        if key not in way_ids:
            wid = 1000 + len(way_ids)
            way_ids[key] = wid
            nds = "".join(f'<nd ref="{r}"/>' for r in key)
            way_xml.append(f'  <way id="{wid}">{nds}<tag k="type" v="line_thin"/></way>')
        return way_ids[key]

    interior = {"EB_through", "WB_through", "EB_right", "NB_left", "NB_right", "WB_left"}
    turn = {"EB_through": "straight", "WB_through": "straight", "EB_right": "right",
            "NB_left": "left", "NB_right": "right", "WB_left": "left"}
    signal_refs = set()
    if signal_style in ("interior", "both"):
        signal_refs |= interior
    if signal_style in ("approach", "both"):
        signal_refs |= {"EB_in", "WB_in", "NB_in"}

    relation_xml: list[str] = []
    for index, (name, (left, right)) in enumerate(lanes.items()):
        rid = 5000 + index
        subtype = "crosswalk" if name == "crosswalk" else "road"
        members = f'<member type="way" role="left" ref="{way(left)}"/>' \
                  f'<member type="way" role="right" ref="{way(right)}"/>'
        if name in signal_refs:
            members += '<member type="relation" role="regulatory_element" ref="9000"/>'
        tags = (f'<tag k="type" v="lanelet"/><tag k="subtype" v="{subtype}"/>'
                '<tag k="speed_limit" v="40"/><tag k="location" v="urban"/>'
                '<tag k="one_way" v="yes"/>')
        if name in turn:
            tags += f'<tag k="turn_direction" v="{turn[name]}"/>'
        relation_xml.append(f'  <relation id="{rid}">{members}{tags}</relation>')
    side = 1 if lefthand else -1
    stop_line = way([(-10, side * w) for w in (0.0, 3.5)])
    light = way([(-12, -8), (-12, -7.5)])
    relation_xml.append(
        '  <relation id="9000">'
        f'<member type="way" role="refers" ref="{light}"/>'
        f'<member type="way" role="ref_line" ref="{stop_line}"/>'
        '<tag k="type" v="regulatory_element"/><tag k="subtype" v="traffic_light"/></relation>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<osm generator="terasim-test">\n'
        + "\n".join(node_xml) + "\n" + "\n".join(way_xml) + "\n"
        + "\n".join(relation_xml) + "\n</osm>\n"
    )


def two_lane_road_lanelet2() -> str:
    """A straight one-way road: two side-by-side lanes, each split into two lanelets.

    The converter must chain each lane across the split and group both lanes
    into ONE two-lane SUMO edge.
    """
    lat0, lon0 = RHT_ORIGIN
    ky = 111_195.0
    kx = ky * math.cos(math.radians(lat0))
    xs = (0.0, 60.0, 120.0)
    ys = (0.0, -3.5, -7.0)  # left edge, shared boundary, right edge (driving east)
    nodes = []
    nid = {}
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            nid[(i, j)] = 1 + 3 * i + j
            lat, lon = lat0 + y / ky, lon0 + x / kx
            nodes.append(f'  <node id="{nid[(i, j)]}" lat="{lat:.10f}" lon="{lon:.10f}"/>')
    ways = []
    wid = {}
    for seg in range(2):
        for j in range(3):
            wid[(seg, j)] = 100 + 3 * seg + j
            refs = f'<nd ref="{nid[(seg, j)]}"/><nd ref="{nid[(seg + 1, j)]}"/>'
            ways.append(f'  <way id="{wid[(seg, j)]}">{refs}</way>')
    rels = []
    for seg in range(2):
        for lane in range(2):  # lane 0 = left (median side), lane 1 = right (curb side)
            rels.append(
                f'  <relation id="{200 + 2 * seg + lane}">'
                f'<member type="way" role="left" ref="{wid[(seg, lane)]}"/>'
                f'<member type="way" role="right" ref="{wid[(seg, lane + 1)]}"/>'
                '<tag k="type" v="lanelet"/><tag k="subtype" v="road"/>'
                '<tag k="speed_limit" v="50"/></relation>'
            )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<osm>\n' + "\n".join(nodes) + "\n"
        + "\n".join(ways) + "\n" + "\n".join(rels) + "\n</osm>\n"
    )
