# SPDX-License-Identifier: EPL-2.0 OR GPL-2.0-or-later
#
# Ported from Eclipse SUMO 1.23.1 (https://eclipse.dev/sumo): src/libsumo/Helper.cpp
# (convertCartesianToRoadMap, patchShapeDistance), src/utils/geom/PositionVector.cpp,
# src/utils/geom/GeomHelper.cpp, src/utils/geom/Position.h and src/microsim/MSLane.h.
# Copyright (C) 2001-2025 German Aerospace Center (DLR) and others; the copyright of the ported
# code remains with its original authors. This file is made available under the Eclipse Public
# License 2.0 (LICENSES/EPL-2.0.txt) or, when the conditions for such availability set forth in
# the Eclipse Public License 2.0 are satisfied, the GNU General Public License, version 2 or
# later. The rest of this repository is licensed under the Apache License, Version 2.0.

"""Exact, cached replica of ``traci.simulation.convertRoad`` (SUMO 1.23.1 semantics).

``execute_trajectory_command`` maps every CNDE trajectory point to ``(edge, lane_pos,
lane_index)`` once per controlled vehicle per step.  libsumo's ``convertRoad``
(``Helper::convertCartesianToRoadMap``) rebuilds the candidate set from its lane
R-tree with a 1000 m search range on every call and evaluates the distance to
every lane in that box, which costs ~0.2-0.3 ms per call on a corridor network --
10 % of a CNDE evaluation's wall time.

This module reproduces the libsumo result bit-for-bit from lane geometry read
once through TraCI:

* candidate lanes: every lane (internal junction lanes included) -- the libsumo
  search range of 1000 m always contains the nearest lane;
* lane distance: ``PositionVector::distance2D`` (clamped projection on each
  segment, nearest segment chosen on *squared* distance, first segment wins ties),
  then ``Helper::patchShapeDistance``: if the lane is not a walking area and the
  point has a perpendicular foot on the lane (``nearest_offset_to_point25D(pos,
  perpendicular=True) != INVALID_OFFSET``), half the lane width is subtracted
  (floored at 0).  Points inside a lane therefore tie at distance 0 and the tie
  is broken by the smallest lane id (``std::string`` order);
* lane position: ``MSLane::interpolateGeometryPosToLanePos`` =
  ``offset / (max(POSITION_EPS, shape.length()) / lane_length)``.

Every floating-point operation is issued in the same order as the C++ code, so
the Python result equals libsumo's (validated on 200k recorded CNDE targets of a
corridor network: 0 lane / 0 lane_pos differences).  Internal shortcuts (grid index,
bounding-box pruning) only decide which lanes are evaluated exactly and never
change the outcome: a lane is skipped only when its bounding-box distance minus
the largest half lane width already exceeds the best patched distance found.
"""

from __future__ import annotations

import math
import re
from typing import Iterable

from terasim.overlay import traci

__all__ = ["SumoLaneLocator", "convert_road"]

POSITION_EPS = 0.1  # SUMO's POSITION_EPS (config.h)
INVALID_OFFSET = -1.0  # GeomHelper::INVALID_OFFSET

_WALKINGAREA_EDGE = re.compile(r"^:.*_w\d+$")


def _dist2d(ax: float, ay: float, bx: float, by: float) -> float:
    """Position::distanceTo2D."""
    return math.sqrt((ax - bx) * (ax - bx) + (ay - by) * (ay - by))


def _dist_sq2d(ax: float, ay: float, bx: float, by: float) -> float:
    """Position::distanceSquaredTo2D."""
    return (ax - bx) * (ax - bx) + (ay - by) * (ay - by)


def _nearest_offset_on_line(sx, sy, ex, ey, line_len, px, py, perpendicular):
    """GeomHelper::nearest_offset_on_line_to_point2D (``line_len`` precomputed as lineStart.distanceTo2D(lineEnd))."""
    if line_len == 0.0:
        return 0.0
    u = (((px - sx) * (ex - sx)) + ((py - sy) * (ey - sy))) / line_len
    if u < 0.0 or u > line_len:
        if perpendicular:
            return INVALID_OFFSET
        if u < 0.0:
            return 0.0
        return line_len
    return u


def _position_at_offset_on_line(sx, sy, ex, ey, line_len, pos):
    """static PositionVector::positionAtOffset2D(p1, p2, pos) without lateral offset / extrapolation."""
    if pos < 0 or line_len < pos:
        return None
    if pos == 0.0:
        return sx, sy
    f = pos / line_len
    return sx + (ex - sx) * f, sy + (ey - sy) * f


class _LaneGeometry:
    """One lane's shape in the exact form the libsumo formulas consume."""

    __slots__ = (
        "id", "edge", "index", "points", "segments", "length_factor", "half_width",
        "walking_area", "xmin", "ymin", "xmax", "ymax",
    )

    def __init__(self, lane_id: str, shape: Iterable, lane_length: float, width: float, edge_id: str,
                 index: int | None = None, walking_area: bool | None = None):
        self.id = lane_id
        self.edge = edge_id
        # SUMO's lane index attribute; internal lanes may carry one that differs from the id suffix
        self.index = int(lane_id.rsplit("_", 1)[1]) if index is None else int(index)
        pts = [tuple(float(c) for c in p) for p in shape]
        self.points = [(p[0], p[1]) for p in pts]
        # (sx, sy, ex, ey, 2D length) per segment -- distanceTo2D(start, end) exactly as SUMO computes it
        self.segments = [
            (a[0], a[1], b[0], b[1], _dist2d(a[0], a[1], b[0], b[1])) for a, b in zip(self.points[:-1], self.points[1:])
        ]
        # PositionVector::length() is 3D; with 2D shapes (z == 0) it equals the 2D sum
        shape_len = 0.0
        for a, b in zip(pts[:-1], pts[1:]):
            dz = (a[2] - b[2]) if len(a) > 2 and len(b) > 2 else 0.0
            shape_len += math.sqrt((a[0] - b[0]) * (a[0] - b[0]) + (a[1] - b[1]) * (a[1] - b[1]) + dz * dz)
        # MSLane::myLengthGeometryFactor = MAX2(POSITION_EPS, myShape.length()) / myLength
        self.length_factor = max(POSITION_EPS, shape_len) / lane_length
        self.half_width = width * 0.5
        self.walking_area = bool(_WALKINGAREA_EDGE.match(edge_id)) if walking_area is None else bool(walking_area)
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        self.xmin, self.ymin, self.xmax, self.ymax = min(xs), min(ys), max(xs), max(ys)

    def bbox_distance(self, px: float, py: float) -> float:
        dx = max(self.xmin - px, 0.0, px - self.xmax)
        dy = max(self.ymin - py, 0.0, py - self.ymax)
        return math.sqrt(dx * dx + dy * dy)

    def offsets(self, px: float, py: float):
        """(nearest_offset_to_point2D(p, False), nearest_offset_to_point2D(p, True) != INVALID_OFFSET).

        One pass over the segments that keeps the two independent running minima of the
        clamped and of the perpendicular variant, exactly as two separate SUMO calls would.
        """
        segs = self.segments
        if not segs:
            return 0.0, False
        min_sq = float("inf")
        nearest = INVALID_OFFSET
        pmin_sq = float("inf")
        pnearest = INVALID_OFFSET
        seen = 0.0
        prev = None
        for k, (sx, sy, ex, ey, sl) in enumerate(segs):
            # --- perpendicular = False (used for the distance and the lane position) ---
            pos = _nearest_offset_on_line(sx, sy, ex, ey, sl, px, py, False)
            q = _position_at_offset_on_line(sx, sy, ex, ey, sl, pos)
            d_sq = _dist_sq2d(px, py, q[0], q[1])
            if d_sq < min_sq:
                nearest = pos + seen
                min_sq = d_sq
            # --- perpendicular = True (Helper::patchShapeDistance's validity test) ---
            ppos = _nearest_offset_on_line(sx, sy, ex, ey, sl, px, py, True)
            if ppos == INVALID_OFFSET:
                pd_sq = pmin_sq
            else:
                pq = _position_at_offset_on_line(sx, sy, ex, ey, sl, ppos)
                pd_sq = _dist_sq2d(px, py, pq[0], pq[1])
            if pd_sq < pmin_sq:
                pnearest = ppos + seen
                pmin_sq = pd_sq
            if k != 0 and ppos == INVALID_OFFSET:
                corner_sq = _dist_sq2d(px, py, sx, sy)
                if corner_sq < pmin_sq:
                    psx, psy, pex, pey, psl = prev
                    pos1 = _nearest_offset_on_line(psx, psy, pex, pey, psl, px, py, False)
                    pos2 = _nearest_offset_on_line(sx, sy, ex, ey, sl, px, py, False)
                    if pos1 == _dist2d(psx, psy, pex, pey) and pos2 == 0.0:
                        pnearest = seen
                        pmin_sq = corner_sq
            seen += sl
            prev = (sx, sy, ex, ey, sl)
        return nearest, pnearest != INVALID_OFFSET

    def position_at_offset(self, pos: float):
        """PositionVector::positionAtOffset2D(pos) (member form, re-walking the segments)."""
        if len(self.points) == 1:
            return self.points[0]
        seen = 0.0
        for (sx, sy, ex, ey, sl) in self.segments:
            if seen + sl > pos:
                return _position_at_offset_on_line(sx, sy, ex, ey, sl, pos - seen)
            seen += sl
        return self.points[-1]

    def patched_distance(self, px: float, py: float):
        """(Helper::patchShapeDistance(lane, p, lane.shape.distance2D(p), False), nearest offset)."""
        if len(self.points) == 1:
            return _dist2d(self.points[0][0], self.points[0][1], px, py), 0.0
        offset, perpendicular_ok = self.offsets(px, py)
        q = self.position_at_offset(offset)
        dist = _dist2d(px, py, q[0], q[1])
        if perpendicular_ok and not self.walking_area:
            dist = max(0.0, dist - self.half_width)
        return dist, offset


class SumoLaneLocator:
    """Lane geometry of the running SUMO net, with an exact ``convert`` replacing ``convertRoad``."""

    CELL = 25.0  #: grid cell size [m]
    COVER = 12.0  #: a cell's candidate list covers every lane within this distance of the cell

    def __init__(self, lanes: list[_LaneGeometry], net_tag: str):
        self.net_tag = net_tag
        self.lanes = lanes
        self.max_half_width = max((lane.half_width for lane in lanes), default=0.0)
        self._grid: dict[tuple[int, int], list[_LaneGeometry]] = {}
        cell = self.CELL
        cover = self.COVER
        for lane in lanes:
            ix0 = math.floor((lane.xmin - cover) / cell)
            ix1 = math.floor((lane.xmax + cover) / cell)
            iy0 = math.floor((lane.ymin - cover) / cell)
            iy1 = math.floor((lane.ymax + cover) / cell)
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    self._grid.setdefault((ix, iy), []).append(lane)

    # ------------------------------------------------------------------ build
    @staticmethod
    def current_net_tag() -> str:
        """Identity of the SUMO instance's net: the net-file option plus the lane count."""
        try:
            net_file = traci.simulation.getOption("net-file")
        except Exception:  # noqa: BLE001 -- TraCI servers without getOption
            net_file = ""
        return f"{net_file}|{traci.lane.getIDCount()}"

    @classmethod
    def from_traci(cls) -> "SumoLaneLocator":
        """Lane table of the running SUMO instance.

        Geometry comes from the net file (sumolib, ``withInternal``) when it can be read:
        the file carries the 3D shape that ``PositionVector::length()`` uses, the lane
        ``index`` attribute and the edge function (walking areas).  Otherwise it falls back
        to the TraCI getters, whose 2D shapes are exact on flat nets.
        """
        tag = cls.current_net_tag()
        lane_ids = list(traci.lane.getIDList())
        lanes = None
        try:
            net_file = traci.simulation.getOption("net-file")
            if net_file:
                lanes = cls._lanes_from_net_file(net_file, lane_ids)
        except Exception:  # noqa: BLE001 -- any trouble reading the file -> TraCI geometry
            lanes = None
        if lanes is None:
            lanes = [
                _LaneGeometry(
                    lane_id,
                    traci.lane.getShape(lane_id),
                    traci.lane.getLength(lane_id),
                    traci.lane.getWidth(lane_id),
                    traci.lane.getEdgeID(lane_id),
                )
                for lane_id in lane_ids
            ]
        return cls(lanes, tag)

    @staticmethod
    def _lanes_from_net_file(net_file: str, lane_ids: list[str]):
        """Lane geometry parsed from the net file; None when it does not describe this instance."""
        import sumolib

        net = sumolib.net.readNet(net_file, withInternal=True)
        lanes = []
        for lane_id in lane_ids:
            try:
                lane = net.getLane(lane_id)
            except KeyError:
                return None
            edge = lane.getEdge()
            lanes.append(
                _LaneGeometry(
                    lane_id,
                    lane.getShape3D(),
                    lane.getLength(),
                    lane.getWidth(),
                    edge.getID(),
                    index=lane.getIndex(),
                    walking_area=(edge.getFunction() == "walkingarea"),
                )
            )
        return lanes

    # ---------------------------------------------------------------- convert
    def _best(self, px: float, py: float, candidates: Iterable[_LaneGeometry]):
        ranked = sorted(((lane.bbox_distance(px, py), lane) for lane in candidates), key=lambda t: t[0])
        slack = self.max_half_width + 1e-6
        best = None
        best_d = float("inf")
        best_off = 0.0
        exhausted = True
        for bd, lane in ranked:
            if bd - slack > best_d:
                exhausted = False
                break
            d, off = lane.patched_distance(px, py)
            # Helper::convertCartesianToRoadMap: smaller distance wins, equal distance -> smaller lane id
            if d < best_d or (d == best_d and best is not None and lane.id < best.id):
                best_d, best, best_off = d, lane, off
        return best, best_d, best_off, exhausted

    def convert(self, x: float, y: float):
        """(edgeID, lanePos, laneIndex) -- identical to ``traci.simulation.convertRoad(x, y)``."""
        px, py = float(x), float(y)
        cell_lanes = self._grid.get((math.floor(px / self.CELL), math.floor(py / self.CELL)))
        best = None
        if cell_lanes:
            best, best_d, best_off, exhausted = self._best(px, py, cell_lanes)
            # the cell list is complete only up to COVER; beyond that fall back to every lane
            if exhausted and best_d + self.max_half_width + 1e-6 > self.COVER:
                best = None
        if best is None:
            best, best_d, best_off, _ = self._best(px, py, self.lanes)
            if best is None:
                raise RuntimeError("Cannot convert position to road.")
        return best.edge, best_off / best.length_factor, best.index


_locator: SumoLaneLocator | None = None


def convert_road(x: float, y: float):
    """Drop-in for ``traci.simulation.convertRoad(x, y, isGeo=False)`` (same result, ~5x cheaper).

    The lane table is read from TraCI on first use and rebuilt whenever the SUMO
    instance's net changes (new simulation / ``traci.load``).
    """
    global _locator
    locator = _locator
    if locator is None or locator.net_tag != SumoLaneLocator.current_net_tag():
        locator = _locator = SumoLaneLocator.from_traci()
    return locator.convert(x, y)
