"""Derive TMC-style turning counts from a SUMO scenario's own route files.

A customer who brings a calibrated route file has no turning-movement-count
document. The counts are read off the routes instead: every pair of
consecutive edges of a route that runs through one of a signal's controlled
connections is one vehicle using that connection's movement (``EB-through``,
``NB-left``, ... see :mod:`terasim_city.plans.signal_groups`). The result is the
TMC payload the rest of the pipeline already consumes: observation window
``"1h"``, one intersection per signal with ``intersection_id == tls_id``,
positioned at the signal's own junction (so the net must be georeferenced).

Only demand inside the counting window ``[T0, T1)`` counts: the span the
scenario is simulated over, the base sumocfg's ``<time>`` unless the caller
passes ``horizon_s`` (the optimization trials simulate ``[0, sim_end)``). In
veh/h, inside that window:

* A ``<flow>`` runs from its ``begin`` (default 0) to its ``end`` (without
  one: ``begin + number * period`` when it has both, else the end of the
  window), clipped to the window, at its rate: ``vehsPerHour`` / ``perHour``,
  ``3600 / period`` (``exp(r)`` is ``3600 * r``), ``probability * 3600``, or
  ``number`` spread over its own ``begin``..``end`` (never the clipped span). A
  flow that does not overlap the window is dropped.
* A ``<vehicle>`` or ``<trip>`` counts when it departs inside the window, as
  one vehicle spread uniformly over it: ``3600 / (T1 - T0)`` veh/h.

Flows that run one after another do not add up. The count of a movement is the
peak, over the window, of the summed rates of the demand active at the same
moment (a single vehicle adds its uniform rate everywhere): the busiest volume
the Webster seed has to serve.

Demand without route edges is routed along the shortest passenger path between
its ``from`` and ``to`` edges (through its ``via`` edges), once per pair of
edges. What cannot be counted (no route, no path, a ``depart`` that is not a
time) is skipped, with one warning per file and kind of element.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterator, Mapping, Sequence

import sumolib
from lxml import etree as ET

from terasim_city.binding import junction_latlon
from terasim_city.flow.descriptors import TMC_DIRECTION_TO_CANONICAL, TMC_TURN_TO_CANONICAL
from terasim_city.flow.sources.scenario_routes import _departure_s
from terasim_city.plans.signal_groups import derive_link_movements
from terasim_city.scenario.sumocfg_io import parse_sumocfg

__all__ = ["derive_tmc_from_routes"]

logger = logging.getLogger(__name__)

_CANONICAL_TO_TMC_DIRECTION = {v: k for k, v in TMC_DIRECTION_TO_CANONICAL.items()}
_CANONICAL_TO_TMC_TURN = {v: k for k, v in TMC_TURN_TO_CANONICAL.items()}

_DEMAND_TAGS = ("flow", "vehicle", "trip")
_DEFAULT_WINDOW_S = 3600.0
#: SUMO's Poisson spelling of a flow period: ``period="exp(<vehicles per second>)"``.
_POISSON_PERIOD = re.compile(r"exp\(([^)]*)\)")

#: Why a demand element could not be counted (these texts end up in the warning).
_NO_ROUTE = "no route edges"
_NO_PATH = "no path between from and to"
_NO_TIME = "begin, end or depart is not a time"

_Window = tuple[float, float]
#: ``(tls_id, direction, turn)``.
_Movement = tuple[str, str, str]
#: ``(start, end, veh/h)`` of a flow inside the window; ``None`` stands for one vehicle.
_Interval = tuple[float, float, float]


class _Skip(Exception):
    """A demand element that cannot be counted; ``str(self)`` says why."""


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def _time_s(raw: str | None) -> float | None:
    """Seconds of a SUMO time value (``"3600"``, ``"01:00:00"``); ``None`` if missing or symbolic.

    Delegates to the parser of the ``scenario_routes`` flow source, which reads a
    vehicle's ``depart`` (it accepts plain seconds and ``H:M:S`` / ``D:H:M:S``).
    """
    return None if raw is None else _departure_s(ET.Element("vehicle", depart=raw))


def _time_value_s(root, tag: str) -> float | None:
    node = root.find(f".//time/{tag}")
    return None if node is None else _time_s(node.get("value"))


def _sim_window_s(base_sumocfg: Path) -> _Window:
    """``(begin, end)`` of the base sumocfg's ``<time>``, in seconds.

    One hour from ``begin`` when there is no ``<end>`` or it does not lie after
    ``begin`` (SUMO's own ``-1`` means "until the last vehicle has left").
    """
    root = ET.parse(str(base_sumocfg)).getroot()
    begin = _time_value_s(root, "begin") or 0.0
    end = _time_value_s(root, "end")
    if end is None or end <= begin:
        return begin, begin + _DEFAULT_WINDOW_S
    return begin, end


def _counting_window_s(base_sumocfg: Path, horizon_s: tuple[float, float] | None) -> _Window:
    if horizon_s is None:
        return _sim_window_s(base_sumocfg)
    start, end = float(horizon_s[0]), float(horizon_s[1])
    if not end > start:
        raise ValueError(f"horizon_s must be (start, end) with end > start, got {horizon_s!r}")
    return start, end


# ---------------------------------------------------------------------------
# One demand element: when it runs, how much, and where
# ---------------------------------------------------------------------------

def _period_s(flow) -> float | None:
    """Seconds between two vehicles of a ``period`` flow (``exp(r)``: the mean headway 1 / r)."""
    period = flow.get("period")
    if not period:
        return None
    poisson = _POISSON_PERIOD.fullmatch(period.strip())
    if poisson:
        rate = float(poisson.group(1))
        return 1.0 / rate if rate > 0 else None
    return float(period)


def _flow_rate_vph(flow, begin: float, end: float) -> float:
    """A ``<flow>``'s hourly rate while it runs; ``[begin, end)`` is its own span (0: no rate)."""
    per_hour = flow.get("vehsPerHour") or flow.get("perHour")
    if per_hour:
        return float(per_hour)
    period = _period_s(flow)
    if period:
        return 3600.0 / period
    if flow.get("probability"):
        return float(flow.get("probability")) * 3600.0
    if flow.get("number"):
        return float(flow.get("number")) * 3600.0 / max(end - begin, 1.0)
    return 0.0


def _flow_span_s(flow, window: _Window) -> _Window:
    """``(begin, end)`` of a ``<flow>`` as SUMO runs it, before any clipping.

    ``begin`` defaults to 0. ``end`` is the ``end`` attribute; without one a flow
    with a ``number`` and a ``period`` stops after its last vehicle
    (``begin + number * period``) and any other runs to the end of the window (so
    one that begins after the window is empty). An ``end`` before the ``begin`` is
    an error: SUMO rejects such a flow.
    """
    begin = _time_s(flow.get("begin", "0"))
    end = _time_s(flow.get("end"))
    if begin is None or (end is None and flow.get("end") is not None):
        raise _Skip(_NO_TIME)
    if end is not None:
        if end < begin:
            raise ValueError(
                f"<flow id={flow.get('id')!r}> ends ({end:g} s) before it begins ({begin:g} s)"
            )
        return begin, end
    period = _period_s(flow)
    if flow.get("number") and period:
        return begin, begin + float(flow.get("number")) * period
    return begin, max(begin, window[1])


class _Router:
    """Shortest passenger paths between edge ids, cached per ``(from, to)`` pair."""

    def __init__(self, net) -> None:
        self._net = net
        self._paths: dict[tuple[str, str], list[str] | None] = {}

    def _path(self, from_edge: str, to_edge: str) -> list[str] | None:
        key = (from_edge, to_edge)
        if key not in self._paths:
            self._paths[key] = self._shortest(from_edge, to_edge)
        return self._paths[key]

    def _shortest(self, from_edge: str, to_edge: str) -> list[str] | None:
        if not (self._net.hasEdge(from_edge) and self._net.hasEdge(to_edge)):
            return None
        edges, _cost = self._net.getShortestPath(
            self._net.getEdge(from_edge), self._net.getEdge(to_edge), vClass="passenger"
        )
        return [edge.getID() for edge in edges] if edges else None

    def route(self, elem) -> list[str]:
        """The route of an element given by ``from``, optional ``via`` edges, and ``to``."""
        waypoints = [elem.get("from"), *(elem.get("via") or "").split(), elem.get("to")]
        route: list[str] = []
        for start, stop in zip(waypoints, waypoints[1:]):
            leg = self._path(start, stop)
            if leg is None:
                raise _Skip(_NO_PATH)
            route.extend(leg if not route else leg[1:])  # a leg starts on the previous one's end
        return route


def _route_edges(elem, routes: Mapping[str, list[str]], router: _Router) -> list[str]:
    """The edges of a flow, vehicle or trip: explicit, a named route, or routed from -> to."""
    if elem.get("edges"):
        return elem.get("edges").split()
    child = elem.find("route")
    if child is not None and child.get("edges"):
        return child.get("edges").split()
    if elem.get("route") in routes:
        return routes[elem.get("route")]
    if elem.get("from") and elem.get("to"):
        return router.route(elem)
    raise _Skip(_NO_ROUTE)


def _element_demand(
    elem, window: _Window, routes: Mapping[str, list[str]], router: _Router
) -> tuple[list[str], _Interval | None] | None:
    """``(route edges, interval)`` of a demand element; ``None`` if it is outside the window.

    ``interval`` is ``(start, end, veh/h)`` clipped to the window for a flow and
    ``None`` for a single vehicle. An element is only routed once it is known to
    count. Raises ``_Skip`` for one that cannot be counted.
    """
    interval: _Interval | None = None
    if elem.tag == "flow":
        begin, end = _flow_span_s(elem, window)
        start, stop = max(begin, window[0]), min(end, window[1])
        if stop <= start:
            return None
        interval = (start, stop, _flow_rate_vph(elem, begin, end))
    else:
        depart = _time_s(elem.get("depart"))
        if depart is None:
            raise _Skip(_NO_TIME)
        if not window[0] <= depart < window[1]:
            return None
    return _route_edges(elem, routes, router), interval


def _demand(
    route_files: Sequence[Path], window: _Window, net
) -> Iterator[tuple[list[str], _Interval | None]]:
    """``(route edges, interval)`` of every demand element of the route files that counts.

    SUMO merges several route files into one demand, so a route defined in one
    file may be named by a flow in another: the route table spans all files.
    """
    roots = [(path, ET.parse(str(path)).getroot()) for path in route_files]
    routes = {
        route.get("id"): route.get("edges").split()
        for _, root in roots
        for route in root.iter("route")
        if route.get("id") and route.get("edges")
    }
    router = _Router(net)
    skipped: dict[tuple[Path, str], list[tuple[str, str]]] = defaultdict(list)
    for path, root in roots:
        for elem in root:
            if elem.tag not in _DEMAND_TAGS:
                continue  # comments (their tag is a function), vTypes, routes, persons, ...
            try:
                demand = _element_demand(elem, window, routes, router)
            except _Skip as skip:
                skipped[(path, elem.tag)].append((elem.get("id") or "?", str(skip)))
                continue
            except ValueError as exc:
                raise ValueError(f"{path}: {exc}") from exc
            if demand is not None:
                yield demand
    for (path, tag), items in skipped.items():
        logger.warning(
            "%s: skipped %d <%s> element(s) (%s), e.g. %s; they are not counted",
            path, len(items), tag, "; ".join(sorted({why for _, why in items})),
            ", ".join(item for item, _ in items[:3]),
        )


# ---------------------------------------------------------------------------
# Movements and their peak rates
# ---------------------------------------------------------------------------

def _controlled_movements(
    net, net_file: Path, tls_ids: Sequence[str], overrides: Mapping[str, Mapping[int, str]]
) -> tuple[dict[tuple[str, str], _Movement], dict[str, object]]:
    """The movement of every controlled connection of ``tls_ids``, and each signal's junction."""
    movement_at: dict[tuple[str, str], _Movement] = {}
    junction_of: dict[str, object] = {}
    for tls_id in tls_ids:
        link_movement = {
            lm.link_index: lm.movement
            for lm in derive_link_movements(net_file, tls_id, overrides=overrides.get(tls_id))
        }
        labelled: dict[tuple[str, str], list[str]] = defaultdict(list)
        for in_lane, out_lane, link_index in net.getTLS(tls_id).getConnections():
            label = link_movement[int(link_index)]
            direction, _, turn = label.partition("-")
            in_edge = in_lane.getEdge()
            pair = (in_edge.getID(), out_lane.getEdge().getID())
            if label not in labelled[pair]:
                labelled[pair].append(label)
            movement_at[pair] = (tls_id, direction, turn)  # the last link of a pair wins
            junction_of[tls_id] = in_edge.getToNode()
        for (from_edge, to_edge), labels in labelled.items():
            if len(labels) > 1:  # only possible through overrides: lanes of one pair differ
                logger.warning(
                    "TLS %s: the links from %s to %s carry different movements (%s); the demand "
                    "on that pair of edges is counted as %s",
                    tls_id, from_edge, to_edge, " and ".join(labels), labels[-1],
                )
    return movement_at, junction_of


def _peak_concurrent_vph(flows: Sequence[_Interval]) -> float:
    """The highest total rate of the ``(start, end, veh/h)`` flows running at the same moment."""
    # Intervals are half-open: at one instant an end is processed before a start.
    events = sorted(
        [(start, 1, rate) for start, _, rate in flows]
        + [(end, 0, -rate) for _, end, rate in flows]
    )
    peak = running = 0.0
    for _, is_start, change in events:
        running += change
        if is_start and running > peak:
            peak = running
    return peak


class _MovementDemand:
    """The demand on one movement: single vehicles, and the flows with their running spans."""

    def __init__(self) -> None:
        self.vehicles = 0
        self.flows: list[_Interval] = []

    def add(self, interval: _Interval | None) -> None:
        if interval is None:
            self.vehicles += 1
        else:
            self.flows.append(interval)

    def peak_vph(self, window: _Window) -> float:
        per_vehicle = 3600.0 / (window[1] - window[0])
        return self.vehicles * per_vehicle + _peak_concurrent_vph(self.flows)


def _require_georeferenced(net, net_file: Path) -> None:
    # sumolib keeps the <location> attributes in Net._location (no public accessor).
    location = getattr(net, "_location", None) or {}
    if str(location.get("projParameter", "!")).strip() in ("", "!"):
        raise ValueError(
            f"{net_file}: the network is not georeferenced (projParameter is '!'); "
            "turning counts need junction coordinates. Set a real <location> projection."
        )


def derive_tmc_from_routes(
    base_sumocfg: Path | str,
    tls_ids: Sequence[str],
    *,
    corridor_id: str,
    timestamp_start: str = "1970-01-01T00:00:00",
    overrides: Mapping[str, Mapping[int, str]] | None = None,
    horizon_s: tuple[float, float] | None = None,
) -> dict:
    """Hourly turning counts per TLS, in the TMC payload format (window ``"1h"``).

    Args:
        base_sumocfg: The scenario; its net and route files are read (any number
            of route files), and its ``<time>`` window unless ``horizon_s`` is given.
        tls_ids: The signals to count at, in output order.
        corridor_id: ``corridor_id`` of the payload.
        timestamp_start: ``timestamp_start`` of the payload; the routes carry no
            time of day, so the default is the epoch.
        overrides: Per-TLS link-movement overrides, ``{tls_id: {link_index:
            movement}}``, as :func:`~terasim_city.plans.signal_groups.load_overrides`
            returns them: the same reclassification the signal plans use.
        horizon_s: The counting window ``(start, end)`` in seconds, ``[start, end)``.
            Default: the sumocfg's ``<time>`` (``begin`` 0 when missing, one hour
            when there is no usable ``end``). Pass ``(0, sim_end)`` to count what a
            run simulated over ``[0, sim_end)`` will actually see.

    Raises:
        ValueError: No TLS given; the sumocfg declares no route files; ``horizon_s``
            is empty or inverted; the net is not georeferenced; a TLS is not in the
            net (or cannot be classified); a flow ends before it begins; or no
            demand reaches any requested signal within the window.
    """
    if not tls_ids:
        raise ValueError("tls_ids is empty: name the signals to derive counts for")
    base_sumocfg = Path(base_sumocfg)
    info = parse_sumocfg(base_sumocfg)
    if not info.route_files:
        raise ValueError(
            f"{base_sumocfg}: declares no <route-files>; the turning counts are derived "
            "from the scenario's own demand"
        )
    window = _counting_window_s(base_sumocfg, horizon_s)
    net = sumolib.net.readNet(str(info.net_file))
    _require_georeferenced(net, info.net_file)
    movement_at, junction_of = _controlled_movements(net, info.net_file, tls_ids, overrides or {})

    on_movement: dict[_Movement, _MovementDemand] = defaultdict(_MovementDemand)
    for edges, interval in _demand(info.route_files, window, net):
        for pair in zip(edges, edges[1:]):
            movement = movement_at.get(pair)
            if movement is not None:
                on_movement[movement].add(interval)
    peak_vph = {movement: demand.peak_vph(window) for movement, demand in on_movement.items()}

    intersections, empty = [], []
    for tls_id in tls_ids:
        lat, lon = junction_latlon(net, junction_of[tls_id])
        counts = {
            tmc_direction: {
                tmc_turn: int(round(peak_vph.get((tls_id, direction, turn), 0.0)))
                for turn, tmc_turn in _CANONICAL_TO_TMC_TURN.items()
            }
            for direction, tmc_direction in _CANONICAL_TO_TMC_DIRECTION.items()
        }
        if not any(count for turns in counts.values() for count in turns.values()):
            empty.append(tls_id)
        intersections.append(
            {"intersection_id": tls_id, "lat": lat, "lon": lon, "counts": counts}
        )
    if len(empty) == len(tls_ids):
        raise ValueError(
            "no demand in the route files reaches any of the requested signals within the "
            f"horizon [{window[0]:g}, {window[1]:g}) s (signals: {list(tls_ids)}; under half a "
            "vehicle per hour counts as none); check tls_ids, the horizon and that the routes "
            "belong to this net"
        )
    for tls_id in empty:
        logger.warning(
            "TLS %s: all turning counts are zero: no route of the scenario runs through its "
            "controlled connections within the horizon, or under half a vehicle per hour does "
            "(check tls_ids and that the routes belong to this net)",
            tls_id,
        )
    return {
        "corridor_id": corridor_id,
        "timestamp_start": timestamp_start,
        "observation_interval": "1h",
        "intersections": intersections,
    }
