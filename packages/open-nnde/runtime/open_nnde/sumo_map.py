"""SUMO network -> MTR map polylines.

MTR, like the Waymo Open Motion Dataset it was trained on, describes the map as polylines of
points [x, y, z, dir_x, dir_y, dir_z, type] spaced 0.5 m apart. The direction of a point is the
unit vector from the previous point of its polyline (zero for the first point), as in MTR's
``data_preprocess.get_polyline_dir``; the types are MTR's ``waymo_types.polyline_type``.

What a SUMO network gives:
- lane centre lines, also inside junctions (freeway, surface street or bike lane);
- lines between neighbouring lanes of an edge (broken single white);
- the left side of an edge that has an edge in the opposite direction (solid double yellow),
  every other outer side of the drivable lanes (road edge boundary);
- pedestrian crossings, as closed outlines (crosswalk).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

LANE_FREEWAY = 1
LANE_SURFACE_STREET = 2
LANE_BIKE = 3
LINE_BROKEN_SINGLE_WHITE = 6
LINE_SOLID_DOUBLE_YELLOW = 12
ROAD_EDGE_BOUNDARY = 15
CROSSWALK = 18

POINT_SPACING_M = 0.5
FREEWAY_SPEED_MPS = 25.0


@dataclass
class MtrMap:
    """The polylines of one SUMO network, in the network's own coordinates."""

    polylines: list[np.ndarray]  # each (n, 7) float32
    pos_offset: np.ndarray  # centre of the network's bounding box
    lane_ids: list[str] = field(default_factory=list)

    @property
    def num_points(self) -> int:
        return int(sum(len(p) for p in self.polylines))


def densify(points, spacing: float = POINT_SPACING_M) -> np.ndarray:
    """Resample a polyline at a fixed arc-length spacing, keeping both end points."""
    pts = np.asarray(points, dtype=np.float64)[:, :2]
    if len(pts) < 2:
        return pts
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    keep = np.concatenate([[True], seg > 1e-9])
    pts, seg = pts[keep], seg[seg > 1e-9]
    if len(pts) < 2:
        return pts[:1]
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    s = np.arange(0.0, cum[-1], spacing)
    if cum[-1] - s[-1] > 1e-6:
        s = np.append(s, cum[-1])
    return np.stack([np.interp(s, cum, pts[:, 0]), np.interp(s, cum, pts[:, 1])], axis=1)


def polyline(points_xy: np.ndarray, type_id: int) -> np.ndarray:
    """(n, 2) points -> (n, 7) MTR polyline points, direction from the previous point."""
    out = np.zeros((len(points_xy), 7), dtype=np.float32)
    out[:, 0:2] = points_xy
    prev = np.roll(out[:, 0:3], shift=1, axis=0)
    prev[0] = out[0, 0:3]
    diff = out[:, 0:3] - prev
    out[:, 3:6] = diff / np.clip(np.linalg.norm(diff, axis=-1)[:, None], 1e-6, None)
    out[:, 6] = type_id
    return out


def offset_side(points_xy: np.ndarray, distance: float) -> np.ndarray:
    """The polyline shifted sideways: positive distance to the left of the travel direction."""
    if len(points_xy) < 2:
        return points_xy.copy()
    tangent = np.gradient(points_xy, axis=0)
    tangent /= np.clip(np.linalg.norm(tangent, axis=1, keepdims=True), 1e-9, None)
    normal = np.stack([-tangent[:, 1], tangent[:, 0]], axis=1)
    return points_xy + distance * normal


def _drivable(lane) -> bool:
    return lane.allows("passenger") or lane.allows("bicycle")


def _lane_type(lane) -> int:
    if not lane.allows("passenger") and lane.allows("bicycle"):
        return LANE_BIKE
    return LANE_FREEWAY if lane.getSpeed() >= FREEWAY_SPEED_MPS else LANE_SURFACE_STREET


def _has_opposite(edge) -> bool:
    origin = edge.getFromNode()
    return any(
        other.getToNode() is origin and other.getFunction() == ""
        for other in edge.getToNode().getOutgoing()
    )


def _crosswalk_outlines(lanes) -> list[np.ndarray]:
    """Closed outlines of pedestrian crossing lanes."""
    out = []
    for lane in lanes:
        centre = densify(lane.getShape())
        if len(centre) < 2:
            continue
        half = lane.getWidth() / 2.0
        outline = np.concatenate(
            [offset_side(centre, half), offset_side(centre, -half)[::-1]], axis=0
        )
        out.append(polyline(densify(np.vstack([outline, outline[:1]])), CROSSWALK))
    return out


def _edge_lines(edge, lanes, internal: bool) -> list[np.ndarray]:
    """Centre lines of the drivable lanes; for a normal edge also its lane lines and sides."""
    out, centres = [], {}
    for lane in lanes:
        if not _drivable(lane):
            continue
        centre = densify(lane.getShape())
        if len(centre) >= 2:
            centres[lane.getIndex()] = (lane, centre)
            out.append(polyline(centre, _lane_type(lane)))
    if internal or not centres:
        return out

    order = sorted(centres)
    (right_lane, right), (left_lane, left) = centres[order[0]], centres[order[-1]]
    left_type = LINE_SOLID_DOUBLE_YELLOW if _has_opposite(edge) else ROAD_EDGE_BOUNDARY
    out.append(
        polyline(densify(offset_side(right, -right_lane.getWidth() / 2.0)), ROAD_EDGE_BOUNDARY)
    )
    out.append(polyline(densify(offset_side(left, left_lane.getWidth() / 2.0)), left_type))
    for index in order[:-1]:
        lane, centre = centres[index]
        out.append(
            polyline(densify(offset_side(centre, lane.getWidth() / 2.0)), LINE_BROKEN_SINGLE_WHITE)
        )
    return out


def map_from_net(net_xml: str | Path) -> MtrMap:
    """Read a SUMO network and return its MTR polylines."""
    import sumolib

    net = sumolib.net.readNet(str(net_xml), withInternal=True, withPedestrianConnections=True)
    (xmin, ymin), (xmax, ymax) = net.getBBoxXY()
    polylines: list[np.ndarray] = []
    lane_ids: list[str] = []
    for edge in net.getEdges(withInternal=True):
        function, lanes = edge.getFunction(), edge.getLanes()
        lane_ids.extend(lane.getID() for lane in lanes)
        if function == "crossing":
            polylines.extend(_crosswalk_outlines(lanes))
        elif function in ("", "internal"):
            polylines.extend(_edge_lines(edge, lanes, internal=function == "internal"))
    return MtrMap(
        polylines=[p for p in polylines if len(p) >= 2],
        pos_offset=np.array([(xmin + xmax) / 2.0, (ymin + ymax) / 2.0]),
        lane_ids=lane_ids,
    )


def batch_polylines(polylines: list[np.ndarray], num_points: int = 20):
    """Split every polyline into pieces of at most ``num_points`` points.

    Same output as MTR's ``WaymoDataset.generate_batch_polylines_from_map`` but split on the
    given polyline boundaries instead of on 1 m gaps between consecutive points.
    Returns (num_pieces, num_points, 7) float32 and (num_pieces, num_points) int32.
    """
    pieces, masks = [], []
    for line in polylines:
        for start in range(0, len(line), num_points):
            chunk = line[start : start + num_points]
            piece = np.zeros((num_points, 7), dtype=np.float32)
            mask = np.zeros(num_points, dtype=np.int32)
            piece[: len(chunk)] = chunk
            mask[: len(chunk)] = 1
            pieces.append(piece)
            masks.append(mask)
    if not pieces:
        return np.zeros((1, num_points, 7), np.float32), np.zeros((1, num_points), np.int32)
    return np.stack(pieces), np.stack(masks)
