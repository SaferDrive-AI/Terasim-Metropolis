"""OD-based corridor flow source with turn-ratio peel-off.

This is the default empirical flow source. It replaced a legacy edge-level
BFS pattern enumeration (since removed) that expanded *every* downstream turn
combination and therefore exploded exponentially on dense real-world OSM
networks — producing thousands of very long routes that gridlocked SUMO.

Algorithm — peel-off OD on the INTERSECTION graph
=================================================

The nodes of the routing graph are the matched TMC intersections (not raw
network edges). Chaining is purely geometric and direction-agnostic: a through
movement continues to the NEAREST monitored intersection lying in its travel
direction (bearing within 45 degrees), connected by a SHORT, DIRECT
``sumolib`` shortest path between the through-exit edge of one intersection
and the arrival approach edge of the next. E-W corridors, N-S corridors and
grids all chain the same way.

For each EXTERNAL boundary inflow (an approach the boundary stage kept, i.e. a
place where traffic genuinely enters the network) carrying rate ``R`` we
propagate a single "trunk" downstream:

* At the current intersection ``R`` is split by turn ratios (left / through /
  right) derived from the TMC counts via
  :func:`terasim_city.flow.network.topology._turn_weights`.
* LEFT and RIGHT movements TERMINATE the stream (unless the network is a
  grid where a monitored intersection lies that way — then the next inflow's
  own counts cover it): we emit one route = accumulated trunk + the turn's out
  edge + a short straight exit tail, with rate ``R * turn_ratio``.
* THROUGH chains ahead. If a monitored intersection lies in the travel
  direction and is reachable
  (``getShortestPath`` is not ``None``) we append that direct connector to the
  trunk and CONTINUE propagation into the next intersection with the reduced
  rate. If it is unreachable (end of corridor, or a disconnected
  sub-corridor), the through stream TERMINATES as a network-exit route.

Because ``getShortestPath`` returning ``None`` prevents chaining, disconnected
sub-corridors self-separate: a boundary inflow only ever reaches its own
connected component.

Each inflow therefore yields at most ~2 peel-offs per intersection plus one
final terminal route — LINEAR in corridor length, not an exponential tree.

The emitted routes are handed to the existing
:func:`terasim_city.flow.writers.routes_xml.write_routes_xml`, which performs
sanitisation, upstream extension and min-length padding, so this module stays
focused on the intersection-graph routing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import sumolib

from terasim_city.binding import BINDING_RADIUS_M
from terasim_city.flow.descriptors import CANONICAL_TURNS, FlowDescriptor, TMCData
from terasim_city.flow.network.boundary import build_boundary_json
from terasim_city.flow.network.matcher import map_intersections
from terasim_city.flow.network.topology import _turn_weights, _walk_straightest
from terasim_city.flow.sources.base import FlowSource
from terasim_city.flow.writers.routes_xml import write_routes_xml

logger = logging.getLogger(__name__)

#: Compass heading (degrees, 0=north, clockwise) a vehicle travels when
#: approaching on each cardinal direction. Used to find the next monitored
#: intersection in a through movement's travel direction — chaining is a pure
#: geometry question, so any direction can chain (E-W, N-S, grids alike).
_DIRECTION_HEADINGS: dict[str, float] = {"NB": 0.0, "EB": 90.0, "SB": 180.0, "WB": 270.0}

#: A through movement chains only toward an intersection within this angular
#: deviation of its travel direction (strictly less, so exact grid diagonals
#: at 45 degrees do not chain sideways).
_CHAIN_BEARING_TOL_DEG = 45.0

#: When the nearest ahead candidate is not drivably reachable (one-way,
#: divided carriageway), try up to this many nearest candidates before the
#: through stream terminates as a network exit.
_CHAIN_MAX_CANDIDATES = 3

#: A connector is rejected when its driving length exceeds this multiple of
#: the straight-line distance between the two intersections — "reachable only
#: via a huge detour" must not fabricate absurd through routes.
_CHAIN_DETOUR_FACTOR = 3.0

#: Flows below this (in raw count units) are dropped as sub-noise.
_RATE_EPS = 1e-6


@dataclass
class _Stream:
    """A single terminated flow stream produced by peel-off propagation.

    ``edges`` is the full ordered edge sequence (spawn approach → exit); it is
    handed to the writer verbatim (the writer sanitises/pads). ``decisions`` is
    a breadcrumb of the ``(intersection, direction, turn)`` choices for audit.
    """

    edges: list[str]
    rate: float
    decisions: list[tuple[str, str, str]] = field(default_factory=list)


class CorridorGraphFlowSource(FlowSource):
    """OD peel-off flow source routed on the intersection graph.

    Registered as ``"corridor_graph"`` in
    :data:`terasim_city.api.FLOW_SOURCES`; future sources plug in through the
    same registry and constructor surface.
    """

    def __init__(
        self,
        tmc_json: Path | str,
        *,
        exit_hops: int = 2,
        spawn_upstream_hops: int = 6,
        max_corridor_hops: int = 8,
        allow_uturn: bool = True,
        max_match_distance_m: float = BINDING_RADIUS_M,
        flow_scale: float = 1.0,
        min_route_edges: int = 5,
        internal_axis_thresh_deg: float = 45.0,
        dump_intermediate: bool = True,
        base_sumocfg: Path | str | None = None,
    ):
        """Construct an OD corridor-graph flow source.


        Args:
            tmc_json: Path to the TMC JSON payload.
            exit_hops: Straight-walk length of the exit tail appended after a
                terminating (left/right/off-corridor/end) movement.
            spawn_upstream_hops: Forwarded to the writer — upstream edges
                prepended to each route so vehicles spawn away from the stop bar.
            max_corridor_hops: BFS hop cap for the boundary stage's
                internal-inflow identification (forwarded as
                ``internal_max_hops``); the OD chaining itself does not
                use it.
            allow_uturn: Forwarded to the writer / straight-walk helpers.
            max_match_distance_m: Max distance (m) for a TMC-to-junction match.
            flow_scale: Global multiplier applied to all generated flow rates.
            min_route_edges: Minimum route length enforced by the writer.
            internal_axis_thresh_deg: Heading tolerance used when identifying
                corridor-internal approaches in the boundary stage.
            dump_intermediate: Write per-stage JSON debug artifacts.
            base_sumocfg: Accepted for registry uniformity (every flow source
                is constructed with the base scenario's sumocfg); unused here.
        """
        self.tmc_json = Path(tmc_json)
        self.tmc = TMCData.from_json(self.tmc_json)
        self.exit_hops = exit_hops
        self.spawn_upstream_hops = spawn_upstream_hops
        self.max_corridor_hops = max_corridor_hops
        self.allow_uturn = allow_uturn
        self.max_match_distance_m = max_match_distance_m
        self.flow_scale = flow_scale
        self.min_route_edges = min_route_edges
        self.internal_axis_thresh_deg = internal_axis_thresh_deg
        self.dump_intermediate = dump_intermediate

    @property
    def source_name(self) -> str:
        """Stable identifier used for logging and CLI/registry lookups."""
        return "corridor_graph"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def generate(
        self,
        net_path: Path | str,
        output_path: Path | str,
        sim_end: float = 1800.0,
    ) -> Path:
        """Run the OD peel-off pipeline and write ``rou.xml``.

        Stage 1 matches TMC lat/lon to junctions, stage 2 identifies the
        boundary inflows, stage 3 is the peel-off routing implemented here,
        stages 4/5 (descriptor scaling + writing) reuse the shared writer.
        """
        net_path = Path(net_path)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        net = sumolib.net.readNet(str(net_path), withInternal=True)

        # Stage 1 — match TMC lat/lon to SUMO junctions (shared).
        matched = map_intersections(
            net, self.tmc, max_dist_m=self.max_match_distance_m
        )
        self._maybe_dump_json(output_path.parent / "matched.json", matched)
        self._require_positive_counts_matched(matched)

        # Stage 2 — identify corridor boundary inflows (shared).
        boundary_json = build_boundary_json(
            net,
            self.tmc,
            matched,
            internal_max_hops=self.max_corridor_hops,
            internal_axis_thresh_deg=self.internal_axis_thresh_deg,
        )
        self._maybe_dump_json(output_path.parent / "boundary.json", boundary_json)

        # Stage 3 — OD peel-off on the intersection graph.
        streams = build_streams(
            net=net,
            boundary_json=boundary_json,
            mapped_results=matched,
            tmc=self.tmc,
            exit_hops=self.exit_hops,
            allow_uturn=self.allow_uturn,
        )
        self._maybe_dump_json(
            output_path.parent / "patterns.json",
            [self._stream_to_dict(s) for s in streams],
        )

        dropped_directions = self._warn_missing_directions(matched)

        # Stage 4 — scale raw counts to vehs/hour and build descriptors.
        scale = self._observation_to_hourly_scale() * self.flow_scale
        descriptors = self._streams_to_descriptors(streams, sim_end=sim_end, scale=scale)

        # Stage 5 — write rou.xml (sanitisation + writes happen inside).
        flow_count, emitted_vehph, start_edge_counts = write_routes_xml(
            descriptors,
            output_path,
            net=net,
            sim_end=sim_end,
            flow_scale=1.0,  # already baked into descriptors
            min_route_edges=self.min_route_edges,
            allow_uturn=self.allow_uturn,
            spawn_upstream_hops=self.spawn_upstream_hops,
        )
        self._maybe_dump_json(
            output_path.parent / "generation_summary.json",
            {
                "flow_count": flow_count,
                "emitted_vehs_per_hour": round(emitted_vehph, 4),
                "spawn_start_edge_counts": start_edge_counts,
                "observation_to_hourly_scale": scale / self.flow_scale,
                "flow_scale": self.flow_scale,
                "dropped_directions": dropped_directions,
                "source": self.source_name,
            },
        )
        if flow_count == 0 and self._total_input_count() > 0:
            raise ValueError(
                f"{self.tmc_json}: the input carries "
                f"{self._total_input_count():g} vehicles of counts but ZERO "
                "flows were generated — the demand was silently lost "
                "(matching/boundary/routing dropped everything). Refusing to "
                "hand an empty scenario to the optimizer; inspect "
                "matched.json/boundary.json next to the routes output"
            )
        return output_path.resolve()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _total_input_count(self) -> float:
        """Sum of every turning count in the input payload."""
        return float(sum(
            float(v)
            for inter in self.tmc.intersections
            for turns in inter.counts.values()
            for v in turns.values()
        ))

    def _require_positive_counts_matched(self, matched: list[dict]) -> None:
        """Fail loud when an intersection CARRYING DEMAND failed to match.

        An unmatched zero-count intersection is harmless metadata; an
        unmatched intersection with positive counts means its whole demand
        would silently vanish and the optimizer would score plans against
        traffic that is not there.
        """
        counts_by_iid = {i.intersection_id: i.counts for i in self.tmc.intersections}
        problems: list[str] = []
        for record in matched:
            if record.get("status") == "matched":
                continue
            iid = record["intersection_id"]
            volume = sum(
                float(v)
                for turns in counts_by_iid.get(iid, {}).values()
                for v in turns.values()
            )
            if volume <= 0:
                continue
            candidates = ", ".join(
                f"{c['node_id']} ({c['distance_m']}m)"
                for c in record.get("candidate_nodes", [])[:5]
            ) or "(none)"
            reason = record.get("reason")
            why = (
                reason
                if reason and reason.startswith("ambiguous_match")
                else (
                    "no junction matched within "
                    f"{self.max_match_distance_m}m"
                )
            )
            problems.append(
                f"{iid} (lat={record.get('lat')}, lon={record.get('lon')}, "
                f"{volume:g} veh of counts): {why}; "
                f"nearest candidates: {candidates}"
            )
        if problems:
            raise ValueError(
                f"{self.tmc_json}: {len(problems)} intersection(s) carrying "
                "demand could not be matched to the map — their traffic would "
                "be silently dropped:\n  - " + "\n  - ".join(problems)
            )

    def _warn_missing_directions(self, matched: list[dict]) -> list[dict]:
        """Warn (not raise) when a positive-count direction has no approach.

        On skewed/diagonal streets the canonical TMC direction (say NB) and the geometric
        bucketing (say EB) can legitimately disagree, so a missing direction
        is a data-quality WARNING recorded in generation_summary.json, not a
        hard error — the zero-flow guard still catches total loss.
        """
        counts_by_iid = {i.intersection_id: i.counts for i in self.tmc.intersections}
        dropped: list[dict] = []
        for record in matched:
            if record.get("status") != "matched":
                continue
            iid = record["intersection_id"]
            approaches = record.get("approaches", {})
            for direction, turns in counts_by_iid.get(iid, {}).items():
                volume = sum(float(v) for v in turns.values())
                if volume > 0 and direction not in approaches:
                    dropped.append(
                        {"intersection_id": iid, "direction": direction,
                         "volume": volume}
                    )
                    logger.warning(
                        "%s: direction %s carries %g veh of counts but the "
                        "matched junction has no such approach — that demand "
                        "is dropped (see generation_summary.json)",
                        iid, direction, volume,
                    )
        return dropped

    def _streams_to_descriptors(
        self, streams: list[_Stream], *, sim_end: float, scale: float
    ) -> list[FlowDescriptor]:
        """Dedup identical edge sequences (summing rates) and scale to vehs/hour.

        ``depart_lane`` is ``"best"``: SUMO places the vehicle on the lane
        needing the fewest lane changes for its WHOLE route, so a turning
        vehicle is born in the lane it will actually turn from. ``"allowed"``
        (random among lanes valid for the NEXT edge only) was measured to
        strand vehicles needing a turn 2-3 edges ahead on short/congested
        approaches (a downtown corridor at 0.1 s steps: 4 teleports incl. 1
        wrong-lane vs 0 with best); ``"free"`` is route-blind and worse. On a
        busy eight-signal arterial, best == allowed on throughput with 0
        teleports for both.
        """
        merged: dict[tuple[str, ...], _Stream] = {}
        for s in streams:
            key = tuple(s.edges)
            if key in merged:
                merged[key].rate += s.rate
            else:
                merged[key] = _Stream(list(s.edges), s.rate, list(s.decisions))

        descriptors: list[FlowDescriptor] = []
        for i, key in enumerate(sorted(merged.keys())):
            s = merged[key]
            descriptors.append(
                FlowDescriptor(
                    flow_id=f"flow_{i}",
                    route_id=f"route_{i}",
                    route_edges=tuple(s.edges),
                    vehs_per_hour=s.rate * scale,
                    begin=0.0,
                    end=sim_end,
                    depart_lane="best",
                    source_metadata={"decisions": [list(d) for d in s.decisions]},
                )
            )
        return descriptors

    def _observation_to_hourly_scale(self) -> float:
        """Multiplier converting TMC window counts into vehicles-per-hour."""
        obs_seconds = self.tmc.observation_seconds()
        if obs_seconds <= 0:
            raise ValueError(
                "TMC observation_seconds() returned a non-positive value; "
                "cannot convert counts to vehicles-per-hour."
            )
        return 3600.0 / obs_seconds

    def _maybe_dump_json(self, path: Path, obj) -> None:
        if not self.dump_intermediate:
            return
        path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")

    @staticmethod
    def _stream_to_dict(stream: _Stream) -> dict:
        return {
            "edges": stream.edges,
            "vehs_per_hour": round(stream.rate, 4),
            "decisions": [list(d) for d in stream.decisions],
        }


# ---------------------------------------------------------------------------
# Corridor ordering
# ---------------------------------------------------------------------------

def _heading_vec(heading_deg: float) -> tuple[float, float]:
    """Unit vector for a compass heading (0=+y/north, clockwise)."""
    import math

    rad = math.radians(heading_deg)
    return (math.sin(rad), math.cos(rad))


def _node_coords(net, mapped_by_id: dict) -> dict[str, tuple[float, float]]:
    """Net (x, y) of every matched intersection's junction node."""
    return {
        iid: net.getNode(r["matched_node_id"]).getCoord()
        for iid, r in mapped_by_id.items()
    }


def _corridor_candidates(
    cur_iid: str,
    direction: str,
    coords: dict[str, tuple[float, float]],
    mapped_by_id: dict,
    visited: set[str],
) -> list[tuple[float, str]]:
    """Monitored intersections ahead in the travel direction, nearest first.

    Purely geometric: among the other matched intersections, keep those whose
    bearing from ``cur_iid`` deviates less than ``_CHAIN_BEARING_TOL_DEG`` from
    the travel heading of ``direction`` and that expose an approach in that
    direction (the vehicle arrives on it). No corridor orientation is assumed
    — E-W, N-S, diagonal and grid layouts all work. The CALLER walks the list
    (capped at ``_CHAIN_MAX_CANDIDATES``) and picks the first candidate that
    is actually drivable within the detour budget; an empty list means nothing
    monitored lies ahead (network exit).
    """
    import math

    heading = _DIRECTION_HEADINGS.get(direction)
    if heading is None:
        return []
    hx, hy = _heading_vec(heading)
    cx, cy = coords[cur_iid]
    min_cos = math.cos(math.radians(_CHAIN_BEARING_TOL_DEG))

    hits: list[tuple[float, str]] = []
    for cand, (x, y) in coords.items():
        if cand == cur_iid or cand in visited:
            continue
        if direction not in mapped_by_id[cand]["approaches"]:
            continue
        dx, dy = x - cx, y - cy
        dist = math.hypot(dx, dy)
        if dist <= 0.0:
            continue
        if (dx * hx + dy * hy) / dist <= min_cos:
            continue
        hits.append((dist, cand))
    hits.sort()
    return hits


# ---------------------------------------------------------------------------
# Peel-off propagation# ---------------------------------------------------------------------------
# Peel-off propagation
# ---------------------------------------------------------------------------

def _shortest_connector(net, from_edge_id: str, to_edge_id: str) -> list[str] | None:
    """Direct passenger shortest path (edge ids) from one edge to another.

    Returns the edge-id sequence including both endpoints, or ``None`` when no
    drivable path exists (``getShortestPath`` returns ``None`` edges) — the
    signal that two intersections are not connected and must not be chained.
    """
    edges, _cost = net.getShortestPath(
        net.getEdge(from_edge_id), net.getEdge(to_edge_id), vClass="passenger"
    )
    if edges is None:
        return None
    ids = [e.getID() for e in edges if not e.getID().startswith(":")]
    # Reject connectors that can only reach the target via a U-turn. That means
    # the target approach is a dead-end stub, not a real through path — chaining
    # into it would make the vehicle double back. Signal "not chainable" so the
    # through movement terminates as a normal network exit instead.
    for a, b in zip(ids, ids[1:]):
        conns = net.getEdge(a).getConnections(net.getEdge(b))
        if conns and all(c.getDirection() == "t" for c in conns):
            return None
    return ids


def _propagate(
    net,
    iid: str,
    direction: str,
    rate: float,
    trunk: list[str],
    decisions: list[tuple[str, str, str]],
    visited: set[str],
    *,
    mapped_by_id: dict,
    counts_by_iid: dict,
    coords: dict[str, tuple[float, float]],
    internal_of: dict[str, set[str]],
    exit_hops: int,
    allow_uturn: bool,
    out: list[_Stream],
) -> None:
    """Recursive peel-off. ``trunk`` ends at the approach edge of ``iid``.

    Splits ``rate`` by the TMC turn ratios; left/right movements terminate as
    exit routes, a through movement chains to the nearest monitored
    intersection in its travel direction when one is reachable (any compass
    direction). Total flow is preserved (children sum to the parent rate).
    """
    if rate <= _RATE_EPS:
        return
    app = mapped_by_id[iid]["approaches"].get(direction)
    if not app:
        out.append(_Stream(list(trunk), rate, list(decisions)))
        return
    move_map = app["movements"]
    available = tuple(t for t in CANONICAL_TURNS if t in move_map)
    if not available:
        out.append(_Stream(list(trunk), rate, list(decisions)))
        return

    counts_for_dir = counts_by_iid.get(iid, {}).get(direction, {})
    weights = _turn_weights(counts_for_dir, available)

    for turn, w in weights.items():
        child = rate * w
        if child <= _RATE_EPS:
            continue
        out_edge = move_map[turn]
        chain = decisions + [(iid, direction, turn)]

        if turn == "through":
            # Walk the nearest ahead candidates: the closest may be
            # unreachable (one-way / divided carriageway) while a slightly
            # farther one is fine. Reachability alone is not enough — a
            # connector whose driving length blows past the straight-line
            # distance (detour factor) would fabricate absurd routes, so it
            # counts as unreachable too. All gates fail -> network exit.
            import math

            nxt = None
            connector = None
            candidates = _corridor_candidates(
                iid, direction, coords, mapped_by_id, visited
            )
            for dist, cand in candidates[:_CHAIN_MAX_CANDIDATES]:
                # The boundary module is the single authority on whether an
                # approach's demand is corridor-internal. Chain into ``cand``
                # ONLY when the boundary marked its approach internal (its
                # spawn was suppressed — this chain is what delivers those
                # vehicles). If the boundary kept the approach external,
                # ``cand`` injects its own counted inflow there, and chaining
                # would inject the same vehicles a second time.
                if direction not in internal_of.get(cand, set()):
                    continue
                target = mapped_by_id[cand]["approaches"][direction]["edge_id"]
                cand_connector = _shortest_connector(net, out_edge, target)
                if not cand_connector:
                    continue
                driving_len = sum(
                    net.getEdge(e).getLength() for e in cand_connector
                )
                if driving_len > _CHAIN_DETOUR_FACTOR * max(dist, 1.0):
                    continue
                nxt, connector = cand, cand_connector
                break
            if nxt is not None and connector:
                # connector[0] == out_edge; extend the trunk and recurse.
                new_trunk = trunk + connector
                _propagate(
                    net,
                    nxt,
                    direction,
                    child,
                    new_trunk,
                    chain,
                    visited | {nxt},
                    mapped_by_id=mapped_by_id,
                    counts_by_iid=counts_by_iid,
                    coords=coords,
                    internal_of=internal_of,
                    exit_hops=exit_hops,
                    allow_uturn=allow_uturn,
                    out=out,
                )
                continue
            # End of corridor / disconnected: terminate as a network exit.
        # Terminating movement: trunk + out_edge + straight exit tail.
        tail = _walk_straightest(out_edge, net, hops=exit_hops, allow_uturn=allow_uturn)
        out.append(_Stream(trunk + tail, child, chain))


def build_streams(
    net,
    boundary_json: dict,
    mapped_results: list[dict],
    tmc,  # TMCData; kept untyped to avoid a circular import
    *,
    exit_hops: int,
    allow_uturn: bool,
) -> list[_Stream]:
    """Turn each external boundary inflow into a set of peel-off streams.

    The boundary JSON decides WHERE vehicles enter (external approaches, with
    corridor-internal duplicates already removed); the normalized TMC counts
    supply the turn ratios at every intersection the trunk visits.
    """
    mapped_by_id = {
        r["intersection_id"]: r
        for r in mapped_results
        if r["status"] == "matched"
    }
    counts_by_iid = {i.intersection_id: i.counts for i in tmc.intersections}
    coords = _node_coords(net, mapped_by_id)
    # The boundary's verdict on which approaches are corridor-internal (their
    # spawns were suppressed because an upstream camera covers them). The
    # through-chain must follow the SAME verdict — see the gate in
    # ``_propagate`` — so every unit of demand is injected exactly once.
    internal_of = {
        iid: set(dirs)
        for iid, dirs in boundary_json.get("injection_strategy", {})
        .get("removed_internal_directions", {})
        .items()
    }

    streams: list[_Stream] = []
    for inter in boundary_json.get("intersections", []):
        iid = inter["intersection_id"]
        if iid not in mapped_by_id:
            continue
        for d, turn_counts in inter.get("counts", {}).items():
            if d not in mapped_by_id[iid]["approaches"]:
                continue
            inflow = float(
                sum(float(turn_counts.get(t, 0.0)) for t in CANONICAL_TURNS)
            )
            if inflow <= _RATE_EPS:
                continue
            approach_edge = mapped_by_id[iid]["approaches"][d]["edge_id"]
            _propagate(
                net,
                iid,
                d,
                inflow,
                [approach_edge],
                [],
                {iid},
                mapped_by_id=mapped_by_id,
                counts_by_iid=counts_by_iid,
                coords=coords,
                internal_of=internal_of,
                exit_hops=exit_hops,
                allow_uturn=allow_uturn,
                out=streams,
            )
    return streams


__all__ = ["CorridorGraphFlowSource", "build_streams"]
