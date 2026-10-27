"""Match TMC intersections (lat/lon) to SUMO road network nodes.

This module is the first stage of the TMC pipeline. Given a SUMO network and a
:class:`TMCData` payload, it figures out which junction in the network
corresponds to each TMC intersection, and identifies the incoming approaches
(EB/WB/NB/SB) plus their legal turn movements.

The matching uses a "junction-first" strategy: among the K nearest candidate
nodes, pick the one whose approaches best cover the directions reported in the
TMC counts, breaking ties by distance. This is more robust than nearest-neighbor
alone because OSM-derived networks often have multiple closely spaced nodes near
a real-world intersection (e.g. cluster nodes for actuated signals).
"""

from __future__ import annotations

import math
from typing import Iterable

from terasim_city.binding import is_ambiguous, node_to_tls_map
from terasim_city.flow.descriptors import (
    CANONICAL_DIRECTIONS,
    CANONICAL_TURNS,
    TMCData,
)


def heading_deg(dx: float, dy: float) -> float:
    """Convert a 2D direction vector into a compass-style heading in degrees.

    The result is in ``[0, 360)`` where 0 points along +y (north in SUMO's
    coordinate system) and increases clockwise.
    """
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def nearest_cardinal(heading: float) -> str:
    """Snap a heading (degrees) to the closest cardinal label ``EB/WB/NB/SB``.

    Wraps modulo 360. Used to bucket each incoming edge into the same cardinal
    framework the TMC schema uses internally.
    """
    axes = [(0, "NB"), (90, "EB"), (180, "SB"), (270, "WB")]
    return min(
        axes, key=lambda t: min((heading - t[0]) % 360, (t[0] - heading) % 360)
    )[1]


def signed_turn(in_h: float, out_h: float) -> float:
    """Compute the signed turn angle (degrees) from an incoming heading to an outgoing one.

    Positive values indicate a left turn, negative values a right turn. Values
    near zero correspond to "through" movements. The result is normalised to
    ``(-180, 180]``.

    Note on sign convention: SUMO's coordinate frame is right-handed with +x
    pointing east and +y pointing north, but :func:`heading_deg` returns
    compass-style headings (0 = north, increasing clockwise). Under that
    convention a *left* turn decreases heading (e.g. east 90 → north 0), so
    we negate the naive ``(out - in)`` difference to keep positive = left.
    The original boundary prototype had this sign reversed,
    which silently swapped left/right in the fallback angle classifier.
    """
    return ((in_h - out_h + 540.0) % 360.0) - 180.0


def classify_turn_by_angle(angle: float) -> str:
    """Fallback turn classification from a signed angle when no SUMO direction hint exists.

    Uses a 35-degree threshold to separate left/right/through. Only invoked when
    the SUMO connection metadata does not provide an explicit direction code.
    """
    if angle > 35:
        return "left"
    if angle < -35:
        return "right"
    return "through"


def edge_heading_into_node(edge) -> float:
    """Return the heading (deg) of an edge as it arrives at its downstream node.

    Uses the last two shape points if available, otherwise falls back to the
    from/to node coordinates of the edge.
    """
    shape = edge.getShape()
    if len(shape) >= 2:
        x1, y1 = shape[-2]
        x2, y2 = shape[-1]
    else:
        x1, y1 = edge.getFromNode().getCoord()
        x2, y2 = edge.getToNode().getCoord()
    return heading_deg(x2 - x1, y2 - y1)


def edge_heading_out_of_node(edge) -> float:
    """Return the heading (deg) of an edge as it leaves its upstream node.

    Uses the first two shape points if available, otherwise the from/to node
    coordinates. This is the counterpart of :func:`edge_heading_into_node` and
    is used when classifying turn movements at the *downstream* end.
    """
    shape = edge.getShape()
    if len(shape) >= 2:
        x1, y1 = shape[0]
        x2, y2 = shape[1]
    else:
        x1, y1 = edge.getFromNode().getCoord()
        x2, y2 = edge.getToNode().getCoord()
    return heading_deg(x2 - x1, y2 - y1)


def nodes_within(net, lon: float, lat: float, max_dist_m: float) -> "list[dict]":
    """All non-internal junctions within ``max_dist_m``, distance-sorted.

    Same record shape as :func:`top_nodes`. Unlike a fixed top-N, this cannot
    hide a qualifying junction behind N closer non-qualifying ones.
    """
    x, y = net.convertLonLat2XY(lon, lat)
    hits = []
    for node in net.getNodes():
        node_id = node.getID()
        if node_id.startswith(":"):
            continue
        nx, ny = node.getCoord()
        dist = math.hypot(nx - x, ny - y)
        if dist <= max_dist_m:
            hits.append({"node_id": node_id, "distance_m": round(dist, 2)})
    hits.sort(key=lambda item: (item["distance_m"], item["node_id"]))
    return hits


def top_nodes(net, lon: float, lat: float, n: int = 5) -> list[dict]:
    """Return the N nearest non-internal SUMO nodes to a (lon, lat) point.

    Args:
        net: A loaded ``sumolib.net.Net`` instance.
        lon: Longitude in WGS84 degrees.
        lat: Latitude in WGS84 degrees.
        n: How many candidates to return.

    Returns:
        A list of ``{"node_id", "distance_m"}`` dicts, sorted by distance.
        Internal nodes (IDs starting with ``:``) are excluded because they
        represent intra-junction geometry and never serve as match targets.
    """
    x, y = net.convertLonLat2XY(lon, lat)
    rows = []
    for node in net.getNodes():
        if node.getID().startswith(":"):
            continue
        nx, ny = node.getCoord()
        d2 = (nx - x) ** 2 + (ny - y) ** 2
        rows.append((d2, node))
    rows.sort(key=lambda r: r[0])
    out = []
    for d2, node in rows[:n]:
        out.append({"node_id": node.getID(), "distance_m": round(math.sqrt(d2), 2)})
    return out


def build_approaches(net, node) -> dict:
    """Group a junction's incoming edges into cardinal approaches with turn movements.

    The result has the shape::

        {
            "EB": {
                "edge_id": "...",
                "heading": 87.3,
                "movements": {"left": "out_edge_a", "through": "out_edge_b", ...},
            },
            "WB": {...},
            ...
        }

    Only edges that allow the ``passenger`` vClass are considered, since the
    TMC counts come from passenger-vehicle observations. For each direction we
    keep the incoming edge with the most legal downstream connections, breaking
    ties by SUMO's enumeration order.

    For each ``(direction, turn)`` movement we pick the single outgoing edge
    that best represents that turn: the straightest one for "through", the most
    leftward one for "left", and the most rightward one for "right". Explicit
    U-turn connectors (``direction == "t"``) are excluded because the TMC
    schema does not model U-turns separately.
    """
    incoming = [
        e for e in node.getIncoming()
        if not e.getID().startswith(":") and e.allows("passenger")
    ]
    by_card: dict[str, list[tuple]] = {k: [] for k in CANONICAL_DIRECTIONS}
    for edge in incoming:
        h = edge_heading_into_node(edge)
        by_card[nearest_cardinal(h)].append((edge, h))

    approaches: dict[str, dict] = {}
    for card, cands in by_card.items():
        if not cands:
            continue

        def score(item):
            # Pick the incoming edge whose downstream connections cover the
            # most outgoing edges — that is, the one that actually behaves like
            # a real intersection approach (not a service road stub).
            e, _ = item
            cnt = 0
            for out_e, conns in e.getOutgoing().items():
                if out_e.getID().startswith(":"):
                    continue
                if conns:
                    cnt += 1
            return cnt

        edge, h = max(cands, key=score)
        approaches[card] = {"edge_id": edge.getID(), "heading": h, "movements": {}}

    for card, info in approaches.items():
        in_edge = net.getEdge(info["edge_id"])
        in_h = info["heading"]
        grouped: dict[str, list[dict]] = {t: [] for t in CANONICAL_TURNS}
        for out_edge, conns in in_edge.getOutgoing().items():
            if not conns or out_edge.getID().startswith(":"):
                continue
            if out_edge.getToNode().getID() == in_edge.getFromNode().getID():
                # Skip explicit U-turn back to the upstream node.
                continue

            out_h = edge_heading_out_of_node(out_edge)
            angle = signed_turn(in_h, out_h)
            # For input turn schema (left/through/right), exclude explicit
            # U-turn connectors (direction code "t").
            valid_conns = [c for c in conns if c.getDirection() != "t"]
            if not valid_conns:
                continue
            dirs = {c.getDirection() for c in valid_conns}
            if "r" in dirs:
                turn = "right"
            elif "l" in dirs:
                turn = "left"
            elif "s" in dirs:
                turn = "through"
            else:
                turn = classify_turn_by_angle(angle)
            grouped[turn].append({
                "to_edge": out_edge.getID(),
                "angle": angle,
                "abs_angle": abs(angle),
            })

        for t in CANONICAL_TURNS:
            cands = grouped[t]
            if not cands:
                continue
            if t == "through":
                best = min(cands, key=lambda c: c["abs_angle"])
            elif t == "left":
                best = max(cands, key=lambda c: c["angle"])
            else:  # right
                best = min(cands, key=lambda c: c["angle"])
            info["movements"][t] = best["to_edge"]

    return approaches


def map_intersections(net, tmc: TMCData, max_dist_m: float) -> list[dict]:
    """Match every TMC intersection to a SUMO junction and inspect its approaches.

    Args:
        net: Loaded ``sumolib.net.Net``.
        tmc: Parsed :class:`TMCData` payload.
        max_dist_m: If the best-matching candidate is farther than this many
            metres, the intersection is recorded as unmatched (still returned,
            but with ``status="unmatched"``).

    Returns:
        A list of dicts with these keys::

            intersection_id, lat, lon, counts,
            candidate_nodes,         # top-5 nearest nodes, for debugging
            matched_node_id,         # best match, or None
            distance_to_node_m,
            approaches,              # output of build_approaches()
            status,                  # "matched" | "unmatched"
            reason                   # human-readable explanation

    The ranking algorithm prefers junctions whose mapped approach directions
    overlap with the directions reported in the TMC counts; only after that
    does it fall back to distance. This avoids snapping to a stub node a few
    metres closer than the real signalised intersection.

    Candidates are ALL junctions within ``max_dist_m`` (the shared binding
    radius), not a fixed top-N: OSM nets sprinkle driveway/kerb micro-nodes
    around real intersections, and a top-5 window can hide the actual
    signalised junction behind five closer stubs. Only when nothing lies
    within the radius does the entry fall back to a nearest-5 listing for
    the diagnostic message.
    """
    results: list[dict] = []
    tls_of = node_to_tls_map(net)
    for inter in tmc.intersections:
        iid = inter.intersection_id
        lat = inter.lat
        lon = inter.lon
        counts = inter.counts

        cands = nodes_within(net, lon, lat, max_dist_m=max_dist_m)
        entry = {
            "intersection_id": iid,
            "lat": lat,
            "lon": lon,
            "counts": counts,
            "candidate_nodes": cands,
            "status": "unmatched",
            "reason": None,
            "matched_node_id": None,
            "distance_to_node_m": None,
            "approaches": {},
        }
        if not cands:
            # Nothing within the radius: report the nearest few junctions so
            # the error is actionable, then record the legacy best-effort
            # match metadata (status stays unmatched).
            nearest = top_nodes(net, lon, lat, n=5)
            entry["candidate_nodes"] = nearest
            if not nearest:
                entry["reason"] = "no_candidate_node"
            else:
                entry["matched_node_id"] = nearest[0]["node_id"]
                entry["distance_to_node_m"] = nearest[0]["distance_m"]
                entry["reason"] = f"distance_too_far>{max_dist_m}m"
            results.append(entry)
            continue

        # The set of directions that have at least one observed vehicle.
        expected_dirs = {
            d for d, tc in counts.items()
            if float(sum(float(tc.get(t, 0.0)) for t in CANONICAL_TURNS)) > 0
        }

        ranked = []
        for cand in cands:
            node = net.getNode(cand["node_id"])
            approaches = build_approaches(net, node)
            mapped_dirs = set(approaches.keys())
            overlap = len(expected_dirs & mapped_dirs)
            movement_cnt = sum(len(v.get("movements", {})) for v in approaches.values())
            incoming_cnt = len([
                e for e in node.getIncoming()
                if not e.getID().startswith(":") and e.allows("passenger")
            ])
            dist = float(cand["distance_m"])
            # Tuple sort key: prefer high overlap, then more movements, then
            # more incoming edges, then closer distance.
            ranked.append((
                overlap,
                movement_cnt,
                incoming_cnt,
                -dist,
                cand["node_id"],
                dist,
                approaches,
            ))

        ranked.sort(reverse=True)
        _ov, _mv, _inc, _negd, best_node_id, dist, approaches = ranked[0]
        node = net.getNode(best_node_id)

        # Fail-loud guard: when a junction JUST beyond the radius covers more
        # of the observed directions than the in-radius winner, the in-radius
        # pick is probably a kerb/driveway node standing in for the real
        # intersection (whose coordinates are simply a bit off). Refuse to
        # match rather than silently pour the demand into the wrong node.
        best_overlap = ranked[0][0]
        if best_overlap < len(expected_dirs):
            beyond = [
                c for c in nodes_within(net, lon, lat, max_dist_m=2 * max_dist_m)
                if float(c["distance_m"]) > max_dist_m
            ]
            outranker = None
            for cand in beyond:
                cand_dirs = set(build_approaches(net, net.getNode(cand["node_id"])))
                if len(expected_dirs & cand_dirs) > best_overlap:
                    outranker = (cand, len(expected_dirs & cand_dirs))
                    break
            if outranker is not None:
                cand, cand_overlap = outranker
                entry["reason"] = (
                    f"best_structural_match_beyond_radius: {cand['node_id']} "
                    f"({cand['distance_m']}m) covers {cand_overlap} observed "
                    f"direction(s) vs {best_overlap} for anything within "
                    f"{max_dist_m}m — check the intersection's lat/lon"
                )
                results.append(entry)
                continue

        # Ambiguity: when the runner-up looks structurally identical (same
        # direction overlap, movements and approach count) and sits comparably
        # close, the ranking would be decided by distance noise alone — report
        # that instead of silently picking, per the shared binding standard.
        # (Every candidate is within max_dist_m by construction here.) Two
        # nodes controlled by the SAME traffic light — the halves of a divided
        # junction under one joined TLS — are one intersection, not a choice:
        # never report them as ambiguous.
        if len(ranked) > 1:
            runner = ranked[1]
            same_signal = (
                tls_of.get(best_node_id) is not None
                and tls_of.get(best_node_id) == tls_of.get(runner[4])
            )
            if (
                not same_signal
                and ranked[0][:3] == runner[:3]
                and is_ambiguous(dist, runner[5])
            ):
                entry["reason"] = (
                    f"ambiguous_match: {best_node_id} ({dist}m) and "
                    f"{runner[4]} ({runner[5]}m) rank equally "
                    "— provide a more precise lat/lon"
                )
                results.append(entry)
                continue

        entry["matched_node_id"] = node.getID()
        entry["distance_to_node_m"] = dist
        entry["approaches"] = approaches
        entry["status"] = "matched"
        entry["reason"] = "ok"
        results.append(entry)
    return results


def redistributed_turn_counts(
    turn_counts: dict, available_turns: Iterable[str]
) -> dict[str, float]:
    """Reallocate volume from impossible turns to the legal ones.

    If the TMC counts report e.g. left/through/right but the SUMO network only
    has through+right at that approach, the left-turn volume is redistributed
    across the available turns proportionally to their observed counts (or
    evenly if all available turns observed zero). The total inflow volume is
    preserved so downstream flow rates stay calibrated to reality.

    Args:
        turn_counts: Canonical ``{turn: count}`` parsed from TMC.
        available_turns: The subset of ``CANONICAL_TURNS`` that are legal in SUMO at
            this approach (derived from :func:`build_approaches`).

    Returns:
        A new ``{turn: float}`` dict containing only available turns.
    """
    ordered_turns = [t for t in CANONICAL_TURNS if t in available_turns]
    total = sum(float(turn_counts.get(t, 0.0)) for t in CANONICAL_TURNS)
    if total <= 0.0 or not ordered_turns:
        return {}

    available_total = sum(float(turn_counts.get(t, 0.0)) for t in ordered_turns)
    if available_total <= 0.0:
        even_share = total / len(ordered_turns)
        return {t: even_share for t in ordered_turns}

    return {
        t: total * float(turn_counts.get(t, 0.0)) / available_total
        for t in ordered_turns
        if float(turn_counts.get(t, 0.0)) > 0.0
    }
