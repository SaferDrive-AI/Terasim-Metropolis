"""Resolve a user-specified corridor to an ordered list of SUMO traffic lights.

Pure sumolib — no TraCI, no running simulation.

TMC<->junction binding is delegated to the SAME matcher the flow pipeline
uses (:func:`terasim_city.flow.network.matcher.map_intersections`,
junction-first ranking), so the junction that receives an intersection's
DEMAND is by construction the junction whose SIGNAL the optimizer controls.

Controllability comes from the NET: any junction whose node maps to a TLS in
the net is corridor-eligible, because every net TLS carries at least one
tlLogic program that materialized plans can replace. Candidate programs in
the sumocfg's additional files are only ONE possible seed source for the
optimization (see the seed ladder in
:mod:`terasim_city.orchestration.corridor_optimization`) — they are never a
resolution requirement, so pure-OSM maps whose sumocfg lists no additional
TLS files at all resolve fine.

Three resolution modes, in priority order:

1. Explicit ``tls_ids``: validated against the net's TLS ids, order preserved.
2. ``from_ref``/``to_ref`` endpoints (TMC intersection ids or ``"lat,lon"``
   strings): each endpoint snaps to the nearest signalized SUMO junction, then
   the corridor is the signalized junctions along the shortest passenger-car
   path between them, in path order.
3. Neither: single-TLS auto-resolve. For legacy compatibility the additional
   files are consulted first (``resolve_tls_id``: the unique TLS carrying
   candidate programs wins, several such TLS error); when the additional files
   define no TLS at all, the net decides — exactly one net TLS auto-selects,
   several error with the list.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from terasim_city.binding import (
    BINDING_RADIUS_M,
    AmbiguousMatchError,
    is_ambiguous,
    junction_latlon,
    node_to_tls_map,
    signal_candidates,
)
from terasim_city.flow.descriptors import TMCData
from terasim_city.flow.network.matcher import top_nodes
from terasim_city.scenario.sumocfg_io import parse_sumocfg
from terasim_city.signals import discover_tls_ids, resolve_tls_id


@dataclass(frozen=True)
class ResolvedIntersection:
    """One corridor intersection, pinned to a SUMO node and TLS.

    ``ref`` is what the caller passed (a TMC intersection_id or a ``"lat,lon"``
    string); for intersections the caller did not name explicitly it falls back
    to the matched TMC intersection_id, else the tls_id. ``intersection_id`` /
    ``lat`` / ``lon`` / ``match_distance_m`` are ``None`` when the TLS has no
    TMC counterpart — never 0.0 sentinels.

    ``anchor_lat``/``anchor_lon`` are the junction's OWN WGS84 position (the
    canonical signal anchor). Unlike ``lat``/``lon`` — which echo the TMC
    observation and may sit tens of metres off to the side — the anchor always
    exists and is what stored plans are matched against.
    """

    ref: str
    intersection_id: str | None
    lat: float | None
    lon: float | None
    node_id: str
    tls_id: str
    match_distance_m: float | None
    anchor_lat: float | None = None
    anchor_lon: float | None = None


@dataclass(frozen=True)
class ResolvedCorridor:
    """Ordered (from -> to) resolved intersections making up the corridor."""

    intersections: tuple[ResolvedIntersection, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "intersections", tuple(self.intersections))
        if not self.intersections:
            raise ValueError("ResolvedCorridor must contain at least one intersection")

    @property
    def tls_ids(self) -> tuple[str, ...]:
        return tuple(item.tls_id for item in self.intersections)


def resolve_corridor(
    base_sumocfg: Path,
    tmc_json: Path,
    *,
    from_ref: str | None = None,
    to_ref: str | None = None,
    tls_ids: Sequence[str] | None = None,
    max_match_distance_m: float = BINDING_RADIUS_M,
) -> ResolvedCorridor:
    """Resolve the corridor per the priority rules in the module docstring.

    Raises:
        ValueError: Unknown tls_id / ref (message lists the candidates), an
            endpoint farther than ``max_match_distance_m`` from any signalized
            junction, a signalized junction on the path without any TLS logic
            in the net, no drivable path between endpoints, or only one of
            ``from_ref``/``to_ref`` given.
    """
    import sumolib

    from terasim_city.flow.network.matcher import map_intersections

    base_sumocfg = Path(base_sumocfg)
    tmc = TMCData.from_json(Path(tmc_json))
    info = parse_sumocfg(base_sumocfg)
    net = sumolib.net.readNet(str(info.net_file))
    node_to_tls = _node_to_tls_map(net)

    # ONE authoritative TMC<->junction binding, shared with the flow pipeline:
    # the flow injects demand at exactly these nodes, so signal control must
    # anchor to the same nodes.
    flow_match = _FlowMatch.from_results(
        map_intersections(net, tmc, max_dist_m=max_match_distance_m),
        node_to_tls,
    )

    if tls_ids:
        return _resolve_explicit(
            base_sumocfg, net, node_to_tls, tmc, flow_match, tls_ids
        )

    if (from_ref is None) != (to_ref is None):
        raise ValueError(
            "from_ref and to_ref must be given together "
            f"(got from_ref={from_ref!r}, to_ref={to_ref!r})"
        )
    if from_ref is None:
        return _resolve_single(
            base_sumocfg, net, node_to_tls, tmc, flow_match
        )
    return _resolve_path(
        base_sumocfg, net, node_to_tls, tmc, flow_match,
        from_ref, to_ref, max_match_distance_m,
    )


# ---------------------------------------------------------------------------
# Resolution modes
# ---------------------------------------------------------------------------

def _resolve_explicit(
    base_sumocfg: Path,
    net,
    node_to_tls: dict[str, str],
    tmc: TMCData,
    flow_match: "_FlowMatch",
    tls_ids: Sequence[str],
) -> ResolvedCorridor:
    # Controllability comes from the net: validate against the net's own TLS
    # ids, NOT against additional-file program discovery.
    net_tls_ids = {tls.getID() for tls in net.getTrafficLights()}
    resolved: list[ResolvedIntersection] = []
    seen: set[str] = set()
    for tls_id in tls_ids:
        if tls_id in seen:
            raise ValueError(f"tls_ids contains duplicate entry {tls_id!r}")
        seen.add(tls_id)
        if tls_id not in net_tls_ids:
            raise ValueError(
                f"tls_id {tls_id!r} is not a traffic light of the net referenced "
                f"by {base_sumocfg}; net TLS ids: "
                f"{_format_tls_ids(sorted(net_tls_ids))}"
            )
        node = _node_for_tls(net, node_to_tls, tls_id)
        resolved.append(
            _build_intersection(net, tmc, flow_match, ref=tls_id, node=node,
                                tls_id=tls_id)
        )
    return ResolvedCorridor(intersections=tuple(resolved))


def _resolve_single(
    base_sumocfg: Path,
    net,
    node_to_tls: dict[str, str],
    tmc: TMCData,
    flow_match: "_FlowMatch",
) -> ResolvedCorridor:
    # Legacy compatibility FIRST: when the additional files declare TLS
    # programs, they disambiguate which intersection the run is about (a map
    # can have several net TLS but additional-file programs for only one).
    # Only when that discovery yields NOTHING does the net decide.
    if discover_tls_ids(base_sumocfg):
        tls_id = resolve_tls_id(base_sumocfg, None)
    else:
        net_tls_ids = sorted(tls.getID() for tls in net.getTrafficLights())
        if not net_tls_ids:
            raise ValueError(
                f"the net referenced by {base_sumocfg} defines no traffic "
                "lights; nothing to resolve"
            )
        if len(net_tls_ids) > 1:
            raise ValueError(
                f"the net referenced by {base_sumocfg} defines "
                f"{len(net_tls_ids)} traffic lights and the additional files "
                "declare none; specify the corridor via tls_ids or "
                f"from_ref/to_ref. Net TLS ids: {_format_tls_ids(net_tls_ids)}"
            )
        tls_id = net_tls_ids[0]
    node = _node_for_tls(net, node_to_tls, tls_id)
    item = _build_intersection(
        net, tmc, flow_match, ref=tls_id, node=node, tls_id=tls_id,
        # Legacy single-run compatibility: a single-intersection TMC payload
        # describes this TLS by construction (distance reported as None when
        # the flow matcher did not itself bind it here).
        force_single_tmc=True,
    )
    return ResolvedCorridor(intersections=(item,))


def _resolve_path(
    base_sumocfg: Path,
    net,
    node_to_tls: dict[str, str],
    tmc: TMCData,
    flow_match: "_FlowMatch",
    from_ref: str,
    to_ref: str,
    max_match_distance_m: float,
) -> ResolvedCorridor:
    a_node, a_dist = _snap_ref_to_signalized_node(
        net, node_to_tls, tmc, flow_match, from_ref, max_match_distance_m
    )
    b_node, b_dist = _snap_ref_to_signalized_node(
        net, node_to_tls, tmc, flow_match, to_ref, max_match_distance_m
    )

    if a_node.getID() == b_node.getID():
        junction_seq = [a_node]
    else:
        path = _shortest_passenger_path(net, a_node, b_node)
        if path is None:
            raise ValueError(
                f"no drivable passenger route found from {from_ref!r} "
                f"(junction {a_node.getID()}) to {to_ref!r} "
                f"(junction {b_node.getID()})"
            )
        junction_seq = [a_node] + [edge.getToNode() for edge in path]

    endpoint_meta = {
        a_node.getID(): (from_ref, a_dist),
        b_node.getID(): (to_ref, b_dist),
    }
    resolved: list[ResolvedIntersection] = []
    seen_tls: set[str] = set()
    for node in junction_seq:
        node_id = node.getID()
        tls_id = node_to_tls.get(node_id)
        is_endpoint = node_id in endpoint_meta
        if tls_id is None:
            if _is_signalized(node):
                # Signalized junction without any TLS object in the net —
                # cannot be controlled, and no plan could ever be applied.
                raise ValueError(
                    f"junction {node_id!r} on the corridor path is signalized "
                    "but has no traffic light logic in the net"
                )
            continue  # unsignalized in-between junction: skip
        if tls_id in seen_tls:
            continue  # cluster TLS spanning several junctions on the path
        # No additional-file program requirement here: the net's own tlLogic
        # makes the TLS controllable; additional-file programs are merely one
        # possible seed source for the optimization.
        seen_tls.add(tls_id)
        if is_endpoint:
            ref, dist = endpoint_meta[node_id]
            resolved.append(
                _build_intersection(
                    net, tmc, flow_match, ref=ref, node=node, tls_id=tls_id,
                    known_distance_m=dist,
                )
            )
        else:
            resolved.append(
                _build_intersection(
                    net, tmc, flow_match, ref=None, node=node, tls_id=tls_id,
                )
            )
    return ResolvedCorridor(intersections=tuple(resolved))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_LISTED_TLS_IDS = 10


def _format_tls_ids(tls_ids: Sequence[str]) -> str:
    """Render a TLS id list for error messages, capped at 10 with a count."""
    if not tls_ids:
        return "(none)"
    listed = ", ".join(tls_ids[:_MAX_LISTED_TLS_IDS])
    remainder = len(tls_ids) - _MAX_LISTED_TLS_IDS
    if remainder > 0:
        listed += f", ... ({len(tls_ids)} total)"
    return listed


# The controlled-junction -> TLS mapping lives in the shared binding module
# (TMC matching needs it too, to judge ambiguity between signals rather than
# between sibling nodes of one signal). Kept under the old private name for
# in-package callers and tests.
_node_to_tls_map = node_to_tls_map


def _node_for_tls(net, node_to_tls: dict[str, str], tls_id: str):
    """Return the SUMO node that hosts ``tls_id`` (fail loud when absent)."""
    if net.hasNode(tls_id):
        return net.getNode(tls_id)
    controlled = sorted(
        node_id for node_id, mapped in node_to_tls.items() if mapped == tls_id
    )
    if not controlled:
        raise ValueError(
            f"tls_id {tls_id!r} controls no junction in the net; "
            "the TLS has no connections to anchor it to a node"
        )
    return net.getNode(controlled[0])


def _is_signalized(node) -> bool:
    return str(node.getType()).startswith("traffic_light")


def _shortest_passenger_path(net, a_node, b_node):
    """Cheapest edge path from any passenger edge leaving A to any entering B."""
    best_path = None
    best_cost = math.inf
    out_edges = [e for e in a_node.getOutgoing() if e.allows("passenger")]
    in_edges = [e for e in b_node.getIncoming() if e.allows("passenger")]
    for e_out in out_edges:
        for e_in in in_edges:
            path, cost = net.getShortestPath(e_out, e_in, vClass="passenger")
            if path and cost < best_cost:
                best_path, best_cost = path, cost
    return best_path


def _parse_ref(tmc: TMCData, ref: str) -> tuple[str | None, float, float]:
    """Turn a corridor ref into ``(intersection_id | None, lat, lon)``."""
    for inter in tmc.intersections:
        if inter.intersection_id == ref:
            return inter.intersection_id, inter.lat, inter.lon
    parts = ref.split(",")
    if len(parts) == 2:
        try:
            return None, float(parts[0]), float(parts[1])
        except ValueError:
            pass
    known = ", ".join(i.intersection_id for i in tmc.intersections)
    raise ValueError(
        f"unknown corridor ref {ref!r}: not a TMC intersection_id and not a "
        f"'lat,lon' pair. TMC intersections available: {known}"
    )


class _FlowMatch:
    """The flow pipeline's authoritative TMC<->junction binding, indexed.

    Built from :func:`map_intersections` output. ``by_iid`` maps a TMC
    intersection_id to its match record; ``iid_by_node`` is the reverse map;
    ``iid_by_tls`` additionally covers cluster TLS spanning several junctions
    (the flow matcher may bind an intersection to junction J1 while the
    corridor walk arrives at sibling junction J2 of the SAME signal — the
    binding is still to the same controlled intersection). First match wins
    on the rare collision — the same record the flow source would use.
    """

    def __init__(
        self,
        by_iid: dict,
        iid_by_node: dict,
        iid_by_tls: dict,
        reason_by_iid: "dict[str, str] | None" = None,
    ):
        self.by_iid = by_iid
        self.iid_by_node = iid_by_node
        self.iid_by_tls = iid_by_tls
        #: Why a TMC intersection did NOT match (distance/ambiguity), for
        #: error messages when a named ref points at it.
        self.reason_by_iid = reason_by_iid or {}

    @classmethod
    def from_results(
        cls, results: list[dict], node_to_tls: dict[str, str]
    ) -> "_FlowMatch":
        by_iid: dict = {}
        iid_by_node: dict = {}
        iid_by_tls: dict = {}
        reason_by_iid: dict = {}
        for record in results:
            if record.get("status") != "matched":
                if record.get("reason"):
                    reason_by_iid[record["intersection_id"]] = record["reason"]
                continue
            iid = record["intersection_id"]
            node_id = record["matched_node_id"]
            by_iid[iid] = record
            iid_by_node.setdefault(node_id, iid)
            tls_id = node_to_tls.get(node_id)
            if tls_id is not None:
                iid_by_tls.setdefault(tls_id, iid)
        return cls(by_iid, iid_by_node, iid_by_tls, reason_by_iid)

    def lookup(self, node_id: str, tls_id: str) -> "str | None":
        """TMC intersection bound to this junction (or its cluster signal)."""
        iid = self.iid_by_node.get(node_id)
        if iid is not None:
            return iid
        return self.iid_by_tls.get(tls_id)


def _snap_ref_to_signalized_node(
    net,
    node_to_tls: dict[str, str],
    tmc: TMCData,
    flow_match: "_FlowMatch",
    ref: str,
    max_match_distance_m: float,
):
    """Resolve a ref to a signalized junction, failing loud past range.

    A NAMED ref (TMC intersection_id) uses the flow pipeline's own match —
    the junction that receives this intersection's demand — so control and
    demand can never anchor to different junctions. A raw ``"lat,lon"`` ref
    has no turning counts to rank with and snaps to the nearest signalized
    junction — unless a second signal is comparably close, which is reported
    as ambiguity instead of silently guessed (see
    :func:`terasim_city.binding.is_ambiguous`).
    """
    iid, lat, lon = _parse_ref(tmc, ref)
    if iid is not None:
        record = flow_match.by_iid.get(iid)
        if record is None:
            why = flow_match.reason_by_iid.get(iid)
            detail = f" ({why})" if why else ""
            raise ValueError(
                f"corridor ref {ref!r} ({lat}, {lon}) was not matched to any "
                f"junction within {max_match_distance_m}m by the flow "
                f"matcher{detail}; check the intersection's lat/lon against "
                "the map"
            )
        node_id = record["matched_node_id"]
        if node_id not in node_to_tls:
            raise ValueError(
                f"corridor ref {ref!r} is bound to junction {node_id!r} "
                "(where its traffic is injected), but that junction has no "
                "traffic light logic in the net — it cannot anchor a corridor"
            )
        return net.getNode(node_id), float(record["distance_to_node_m"])

    signals = signal_candidates(
        net, node_to_tls, lat, lon, radius_m=max_match_distance_m
    )
    if not signals:
        nearest = top_nodes(net, lon, lat, n=5)
        detail = ", ".join(
            f"{c['node_id']} ({c['distance_m']}m)" for c in nearest
        ) or "(no junctions in net)"
        raise ValueError(
            f"corridor ref {ref!r} ({lat}, {lon}) matches no signalized junction "
            f"within {max_match_distance_m}m; nearest candidates: {detail}"
        )
    if len(signals) > 1 and is_ambiguous(signals[0].distance_m, signals[1].distance_m):
        raise AmbiguousMatchError(
            subject=f"corridor ref {ref!r} ({lat}, {lon})",
            candidates=[
                f"{c.tls_id} (junction {c.node_id}, {c.distance_m} m)"
                for c in signals
            ],
            hint="Provide a more precise coordinate (or an explicit tls_id).",
        )
    best = signals[0]
    return net.getNode(best.node_id), best.distance_m


def _build_intersection(
    net,
    tmc: TMCData,
    flow_match: "_FlowMatch",
    *,
    ref: str | None,
    node,
    tls_id: str,
    known_distance_m: float | None = None,
    force_single_tmc: bool = False,
) -> ResolvedIntersection:
    """Assemble a :class:`ResolvedIntersection` with flow-consistent metadata.

    TMC metadata attaches iff the FLOW MATCHER bound a TMC intersection to
    exactly this junction — the same binding that injects its demand — so
    "which camera watches this signal" can never disagree between flow
    generation and signal control. Unmatched metadata is ``None`` (never 0.0
    sentinels). ``known_distance_m`` carries the ref->node snap distance for
    endpoints. ``force_single_tmc`` is the legacy single-TLS rule: a TMC
    payload with exactly one intersection describes this TLS by construction;
    when the flow matcher did not itself bind it here the distance is None.

    The junction's own WGS84 position always rides along as the anchor —
    plan lookup keys on it, so an intersection stays plan-addressable even
    when no TMC camera observes it.
    """
    node_id = node.getID()
    anchor_lat, anchor_lon = junction_latlon(net, node)
    iid = flow_match.lookup(node_id, tls_id)
    if iid is not None:
        record = flow_match.by_iid[iid]
        distance = (
            known_distance_m
            if known_distance_m is not None
            else float(record["distance_to_node_m"])
        )
        return ResolvedIntersection(
            ref=ref if ref is not None else iid,
            intersection_id=iid,
            lat=float(record["lat"]),
            lon=float(record["lon"]),
            node_id=node_id,
            tls_id=tls_id,
            match_distance_m=round(distance, 2),
            anchor_lat=anchor_lat,
            anchor_lon=anchor_lon,
        )
    if force_single_tmc and len(tmc.intersections) == 1:
        inter = tmc.intersections[0]
        return ResolvedIntersection(
            ref=ref if ref is not None else inter.intersection_id,
            intersection_id=inter.intersection_id,
            lat=inter.lat,
            lon=inter.lon,
            node_id=node_id,
            tls_id=tls_id,
            match_distance_m=None,
            anchor_lat=anchor_lat,
            anchor_lon=anchor_lon,
        )
    return ResolvedIntersection(
        ref=ref if ref is not None else tls_id,
        intersection_id=None,
        lat=None,
        lon=None,
        node_id=node_id,
        tls_id=tls_id,
        match_distance_m=(
            round(known_distance_m, 2) if known_distance_m is not None else None
        ),
        anchor_lat=anchor_lat,
        anchor_lon=anchor_lon,
    )
