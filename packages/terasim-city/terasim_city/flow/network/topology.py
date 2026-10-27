"""Road-network graph helpers shared by flow generation.

Small, single-purpose utilities over a sumolib net used by the OD flow source
and the routes writer: legal next-edge enumeration, straightest-exit walking
(route tails), spawn-runway extension (route heads), route sanitisation, and
turn-count weighting.
"""

from __future__ import annotations

from typing import Iterable

from terasim_city.flow.descriptors import CANONICAL_TURNS
from terasim_city.flow.network.matcher import (
    edge_heading_out_of_node,
    signed_turn,
)


def _next_edges(net, edge_id: str, allow_uturn: bool) -> list[str]:
    """Return the downstream edges reachable from ``edge_id`` via passenger-legal connections.

    Filters out internal junction edges, edges that disallow passenger
    vehicles, immediate reverse bounces, and (unless ``allow_uturn`` is true)
    explicit U-turn connectors.
    """
    edge = net.getEdge(edge_id)
    out: list[str] = []
    for oe, conns in edge.getOutgoing().items():
        oid = oe.getID()
        if oid.startswith(":") or not conns:
            continue
        if not oe.allows("passenger"):
            continue
        # Restrict to connections where both lanes allow passenger vehicles.
        valid_conns = [
            c for c in conns
            if c.getFromLane().allows("passenger") and c.getToLane().allows("passenger")
        ]
        if not valid_conns:
            continue
        dirs = {c.getDirection() for c in valid_conns}
        if (not allow_uturn) and ("t" in dirs):
            continue
        # Avoid immediate reverse-edge bounce (eid -> -eid).
        if edge_id.lstrip("-") == oid.lstrip("-") and edge_id != oid:
            continue
        out.append(oid)
    return out


def _pick_straightest_outgoing(edge_id: str, net, allow_uturn: bool) -> str | None:
    """Return the downstream edge that makes the smallest turn relative to ``edge_id``.

    Used by the straight-walk helpers for route tails and padding: we
    keep walking "straight ahead" for a few hops so the vehicle has a plausible
    exit corridor.
    """
    curr = net.getEdge(edge_id)
    curr_h = edge_heading_out_of_node(curr)
    best = None
    best_abs = float("inf")
    for ne in _next_edges(net, edge_id, allow_uturn=allow_uturn):
        # A "straightest" walk must never reverse direction: skip a U-turn step
        # even when ``allow_uturn`` let ``_next_edges`` include it. A turnaround
        # is a ~180 deg move -- the least-straight option, never a plausible
        # exit corridor. This also stops the walk cleanly when the only exit
        # from ``edge_id`` is a U-turn (a dead-end spur reached as ``out_edge``).
        step_conns = curr.getConnections(net.getEdge(ne))
        if step_conns and all(c.getDirection() == "t" for c in step_conns):
            continue
        # Skip dead-end spurs. If ``ne`` has no onward move except a U-turn
        # (``_next_edges`` with no U-turns is empty), walking onto it forces the
        # vehicle to double back. Never pick one as a through/tail continuation;
        # if every candidate is a dead-end ``best`` stays ``None`` and the walk
        # stops here instead.
        if not _next_edges(net, ne, allow_uturn=False):
            continue
        e2 = net.getEdge(ne)
        out_h = edge_heading_out_of_node(e2)
        d = abs(signed_turn(curr_h, out_h))
        if d < best_abs:
            best_abs = d
            best = ne
    return best


def _walk_straightest(start_edge: str, net, hops: int, allow_uturn: bool) -> list[str]:
    """Greedily walk forward for up to ``hops`` edges, always taking the straightest exit.

    Used for the straight exit "tail" appended after a terminating peel-off
    movement (see ``corridor_graph._propagate``). The walk stops early on dead-ends or revisits.
    """
    route = [start_edge]
    seen = {start_edge}
    cur = start_edge
    for _ in range(hops):
        ne = _pick_straightest_outgoing(cur, net, allow_uturn=allow_uturn)
        if not ne or ne in seen:
            break
        route.append(ne)
        seen.add(ne)
        cur = ne
    return route


def _ensure_min_route_length(
    route_edges: list[str], net, min_edges: int, allow_uturn: bool, extra_hops: int = 10
) -> list[str]:
    """Pad a route forward until it has at least ``min_edges`` edges.

    SUMO will reject extremely short routes (and the spawn point is often
    visually ugly). This function extends the route by greedily picking the
    straightest outgoing edge for at most ``extra_hops`` iterations.
    """
    if len(route_edges) >= min_edges:
        return route_edges
    out = list(route_edges)
    seen = set(out)
    cur = out[-1]
    for _ in range(extra_hops):
        if len(out) >= min_edges:
            break
        ne = _pick_straightest_outgoing(cur, net, allow_uturn=allow_uturn)
        if not ne or ne in seen:
            break
        out.append(ne)
        seen.add(ne)
        cur = ne
    return out


def _sanitize_connected_route(route_edges: list[str], net) -> list[str]:
    """Truncate a route at the first illegal hop.

    A route edge ``B`` is legal after ``A`` only if at least one passenger
    connection from ``A`` to ``B`` exists. This protects against rare cases
    where a source produces an edge sequence whose connections were partially
    invalidated (e.g. by ``net.xml`` rewrites).
    """
    if not route_edges:
        return route_edges
    out = [route_edges[0]]
    for nxt in route_edges[1:]:
        cur = out[-1]
        ok = False
        edge = net.getEdge(cur)
        for oe, conns in edge.getOutgoing().items():
            if oe.getID() == nxt and conns:
                valid_conns = [
                    c for c in conns
                    if c.getFromLane().allows("passenger") and c.getToLane().allows("passenger")
                ]
                if not valid_conns:
                    continue
                ok = True
                break
        if not ok:
            break
        out.append(nxt)
    return out


def _extend_route_upstream(
    route_edges: list[str], net, upstream_hops: int, min_spawn_edge_len: float = 30.0
) -> list[str]:
    """Prepend upstream edges so vehicles spawn farther from the intersection.

    SUMO struggles when a vehicle is spawned exactly at the start of a junction
    approach (it often emergency-brakes or fails to depart). Adding upstream
    edges gives the vehicle room to accelerate and lane-sort before reaching the
    intersection. We require each upstream edge to feed *every* passenger lane of
    the downstream edge so the spawn point is reachable from any lane choice.

    ``upstream_hops`` is a hop *cap*, not an exact count: the walk stops early as
    soon as it lands on an edge at least ``min_spawn_edge_len`` metres long, so
    the spawn edge is a natural-length approach rather than whatever short stub
    happens to sit exactly ``upstream_hops`` edges back. Real approaches are
    often a chain of very short segments (turn-lane splits, pockets); a fixed
    hop count lands the spawn point on one of those slivers, which looks wrong
    and gives no runway. Walking until a long-enough edge fixes that.
    """
    if upstream_hops <= 0 or not route_edges:
        return route_edges
    first = route_edges[0]
    first_base = first.lstrip("-")
    out = list(route_edges)
    seen = set(out)
    cur = net.getEdge(first)
    prefix: list[str] = []

    def can_feed_all_passenger_lanes(up_edge, down_edge_id: str) -> bool:
        """Verify that every passenger lane of ``down_edge_id`` is reachable from ``up_edge``.

        A usable spawn runway must let the vehicle reach *any* lane of the
        downstream approach, so that whatever turn its route needs it can merge
        into the right lane over the length of the runway. We therefore require
        every passenger lane of the DOWNSTREAM edge to have an incoming
        passenger connection from ``up_edge``. (A stricter, route-blind reading
        -- that every lane of ``up_edge`` continue onto the downstream edge --
        rejects any approach whose upstream edge has a turn-off/pocket lane,
        i.e. almost all of them, leaving vehicles to spawn on the ~12 m stub
        right at the stop line.)
        """
        down_edge = net.getEdge(down_edge_id)
        passenger_to = set()
        for li in range(down_edge.getLaneNumber()):
            ln = down_edge.getLane(li)
            if ln.allows("passenger"):
                passenger_to.add(ln.getID())
        if not passenger_to:
            return False

        fed = set()
        for oe, conns in up_edge.getOutgoing().items():
            if oe.getID() != down_edge_id:
                continue
            for c in conns:
                fl = c.getFromLane()
                tl = c.getToLane()
                if fl.allows("passenger") and tl.allows("passenger"):
                    fed.add(tl.getID())
        # Require every downstream passenger lane to be reachable from up_edge.
        return passenger_to.issubset(fed)

    for _ in range(upstream_hops):
        incomings = []
        for ie in cur.getIncoming():
            iid = ie.getID()
            if iid.startswith(":"):
                continue
            if not ie.allows("passenger"):
                continue
            if iid in seen:
                continue
            # Do not prepend the immediate reverse of the route start edge.
            if iid.lstrip("-") == first_base and iid != first:
                continue
            # Never use a turnaround as a spawn runway. If the only way onto
            # ``cur`` from this upstream edge is a U-turn (SUMO direction 't'),
            # the vehicle would spawn facing the wrong way and loop back. Skip
            # it; if that leaves no non-U-turn upstream option the loop below
            # stops (``if not incomings: break``) and the vehicle spawns from
            # the current edge instead of being pushed further upstream.
            conns = ie.getConnections(cur)
            if conns and all(c.getDirection() == "t" for c in conns):
                continue
            if not can_feed_all_passenger_lanes(ie, cur.getID()):
                continue
            incomings.append(ie)
        if not incomings:
            break
        # Prefer the longest upstream edge to push spawn point farther.
        best = max(incomings, key=lambda e: e.getLength())
        prefix.append(best.getID())
        seen.add(best.getID())
        cur = best
        # Stop once the spawn edge is a natural-length approach; keep walking
        # through chains of short stubs otherwise (up to the hop cap).
        if best.getLength() >= min_spawn_edge_len:
            break
    prefix.reverse()
    return prefix + out


def _turn_weights(
    counts_for_dir: dict, available_turns: Iterable[str]
) -> dict[str, float]:
    """Normalize turn counts into a probability distribution over the legal turns.

    When the raw counts sum to zero (e.g. an unused approach we still want to
    route through), the function returns a uniform distribution to avoid
    losing volume.

    The returned dict iterates in :data:`CANONICAL_TURNS` order
    (``left``, ``through``, ``right``). This is important: callers iterate
    over ``.items()`` and any non-deterministic order
    here would propagate into the final ``rou.xml`` and break
    reproducibility across Python hash seeds.
    """
    # Preserve canonical ordering — do NOT use ``set`` for iteration.
    available_ordered = [t for t in CANONICAL_TURNS if t in available_turns]
    if not available_ordered:
        return {}
    raw = {t: float(counts_for_dir.get(t, 0.0)) for t in available_ordered}
    s = sum(raw.values())
    if s <= 0:
        w = 1.0 / len(available_ordered)
        return {t: w for t in available_ordered}
    return {t: v / s for t, v in raw.items()}
