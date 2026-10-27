"""One matching standard for every coordinate → map-signal-junction binding.

The city contract requires TMC counts, uploaded signal plans and optimization
targets to be associated through the MAP, never through each other: each
coordinate is independently bound to a map signal junction (its *anchor*),
and two records describe the same physical intersection iff they bind to the
same anchor. Radii and tie-break rules must therefore be identical everywhere
a coordinate meets the map — this module is their single home:

* :data:`BINDING_RADIUS_M` — the one radius used by TMC matching, target
  snapping and plan lookup alike.
* :func:`is_ambiguous` — the shared "too close to call" rule: a match is only
  trusted when the winner is clearly nearer than the runner-up.
* :func:`signal_candidates` — signalized junctions within the radius of a
  WGS84 point, one candidate per traffic light (a joined TLS spanning several
  junctions is a single candidate), distance-sorted.
* :func:`junction_latlon` — a junction's own WGS84 position: the anchor
  coordinate that stored plans are matched against.

Anything that would silently pick between comparable candidates raises
:class:`AmbiguousMatchError` instead — reporting ambiguity beats guessing,
because a wrong guess optimizes the neighbouring intersection and nothing in
the result reveals it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

#: Max distance (metres) between a submitted coordinate and the map signal
#: junction it binds to. Shared by TMC matching, target snapping and plan
#: lookup — one standard, not three.
BINDING_RADIUS_M = 60.0

#: A match is ambiguous when the runner-up is closer than this factor times
#: the winner's distance: 20 m vs 25 m is too close to call, 20 m vs 55 m is a
#: clear winner.
AMBIGUITY_CLEARANCE = 1.5


@dataclass(frozen=True)
class SignalCandidate:
    """One traffic light reachable from a queried coordinate."""

    tls_id: str
    node_id: str
    distance_m: float


class AmbiguousMatchError(ValueError):
    """Several candidates are comparably close to the queried coordinate.

    ``candidates`` keeps the pre-formatted candidate descriptions so callers
    (API layers, logs) can surface the choice to whoever can actually make it.
    """

    def __init__(self, subject: str, candidates: Iterable[str], hint: str) -> None:
        self.subject = subject
        self.candidates = list(candidates)
        super().__init__(
            f"{subject}: ambiguous match — {len(self.candidates)} candidates "
            f"are comparably close: {'; '.join(self.candidates)}. {hint}"
        )


def is_ambiguous(best_distance_m: float, runner_up_distance_m: float) -> bool:
    """True when a runner-up is too close to the winner to trust the pick."""
    return runner_up_distance_m <= best_distance_m * AMBIGUITY_CLEARANCE


def junction_latlon(net, node) -> tuple[float, float]:
    """A junction's own WGS84 position — the canonical anchor coordinate."""
    x, y = node.getCoord()
    lon, lat = net.convertXY2LonLat(x, y)
    return lat, lon


def node_to_tls_map(net) -> dict[str, str]:
    """Map every controlled junction id to its TLS id.

    Covers both the common case (junction id == tls id) and joined TLS whose
    controlled junctions keep their own ids: the junctions a TLS controls are
    derived from its connections' incoming edges. Shared by corridor
    resolution (anchoring) and TMC matching (so ambiguity is judged between
    *signals*, never between sibling nodes of one signal).
    """
    mapping: dict[str, str] = {}
    for tls in net.getTrafficLights():
        tls_id = tls.getID()
        if net.hasNode(tls_id):
            mapping.setdefault(tls_id, tls_id)
        for in_lane, _out_lane, _link_no in tls.getConnections():
            mapping.setdefault(in_lane.getEdge().getToNode().getID(), tls_id)
    return mapping


def signal_candidates(
    net,
    node_to_tls: Mapping[str, str],
    lat: float,
    lon: float,
    *,
    radius_m: float = BINDING_RADIUS_M,
) -> list[SignalCandidate]:
    """Traffic lights within ``radius_m`` of a WGS84 point, nearest first.

    ``node_to_tls`` maps every controlled junction to its TLS id (see
    :func:`terasim_city.corridor.resolve._node_to_tls_map`). Several junctions
    of one joined TLS collapse into a single candidate at the distance of the
    nearest of them — the corridor walk already treats a cluster signal as one
    intersection, so ambiguity must be judged between *signals*, not between
    sibling nodes of the same signal.
    """
    x, y = net.convertLonLat2XY(lon, lat)
    best: dict[str, SignalCandidate] = {}
    for node_id, tls_id in node_to_tls.items():
        nx, ny = net.getNode(node_id).getCoord()
        dist = math.hypot(nx - x, ny - y)
        if dist > radius_m:
            continue
        prev = best.get(tls_id)
        if prev is None or dist < prev.distance_m:
            best[tls_id] = SignalCandidate(
                tls_id=tls_id, node_id=node_id, distance_m=round(dist, 2)
            )
    return sorted(best.values(), key=lambda c: (c.distance_m, c.tls_id))


__all__ = [
    "AMBIGUITY_CLEARANCE",
    "BINDING_RADIUS_M",
    "AmbiguousMatchError",
    "SignalCandidate",
    "is_ambiguous",
    "junction_latlon",
    "node_to_tls_map",
    "signal_candidates",
]
