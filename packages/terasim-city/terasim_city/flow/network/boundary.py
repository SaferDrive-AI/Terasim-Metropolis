"""Identify boundary injection points for TMC-driven traffic flows.

When two TMC-monitored intersections sit on the same corridor, naively turning
both into vehicle spawn points double-counts the corridor's through traffic:
vehicles entering intersection A and continuing east to intersection B would be
counted at *both* approaches. This module discards those internal duplicates,
leaving only the "outer rim" intersections where vehicles genuinely enter the
network from outside the observed corridor.

The algorithm walks upstream from each TMC approach for a bounded number of
edges and stops at the first node that satisfies a three-way stop rule:

1. The node is itself covered by a TMC observation. Observations are the authoritative
   corridor anchors — if the walk reaches another camera, the approach's
   traffic was already counted there, full stop.
2. The node is a netconvert junction cluster (``cluster_...`` ID), e.g. an
   actuated signal group joined during network import.
3. The node is signalized (junction type ``traffic_light*``), regardless of
   how the node happens to be named. Node naming is an artifact of the map
   source (plain OSM IDs, hand-built demo IDs, ...), so any signalized
   junction counts as a major junction.

If the stop node is camera-covered, the approach is marked *internal* and
dropped from the boundary; otherwise it is kept as a genuine inflow.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable

from terasim_city.flow.descriptors import TMCData
from terasim_city.flow.network.matcher import (
    edge_heading_into_node,
    redistributed_turn_counts,
)


def _angle_diff(a: float, b: float) -> float:
    """Smallest unsigned angular distance between two compass headings.

    Both arguments are in degrees and the result lies in ``[0, 180]``.
    """
    return min((a - b) % 360.0, (b - a) % 360.0)


def _is_major_junction(node) -> bool:
    """Whether a SUMO node should terminate camera-to-camera upstream expansion.

    Two structural signals qualify a node as "major":

    * SUMO's netconvert emits cluster IDs (``cluster_...``) for joined
      junctions such as actuated signal groups; and
    * any signalized junction (type ``traffic_light``, including variants
      like ``traffic_light_right_on_red``), regardless of node naming —
      naming conventions vary by map source and cannot be relied upon.
    """
    return node.getID().startswith("cluster_") or (node.getType() or "").startswith(
        "traffic_light"
    )


def _first_upstream_major_junction(
    net,
    edge_id: str,
    origin_node_id: str,
    camera_node_ids: set[str],
    max_hops: int,
    direction_thresh_deg: float = 35.0,
) -> str | None:
    """BFS upstream from ``edge_id`` looking for the first major junction.

    The search is directional: at each step we only follow incoming edges whose
    heading is close to the original edge's heading (within
    ``direction_thresh_deg``). Without this gate the reverse roadway would
    look like a valid upstream continuation and the BFS would teleport across
    the intersection.

    Args:
        net: Loaded ``sumolib.net.Net``.
        edge_id: SUMO edge ID where the search starts (typically a TMC
            approach edge).
        origin_node_id: The intersection node this edge belongs to. Used to
            prevent the BFS from "finding" the start node as the answer.
        camera_node_ids: Node IDs covered by TMC observations. Reaching any of
            these terminates the walk immediately — cameras are authoritative
            corridor anchors even when the node is otherwise unremarkable.
        max_hops: Maximum number of upstream hops to explore before giving up.
        direction_thresh_deg: Maximum heading deviation allowed when extending
            the search upstream.

    Returns:
        The node ID of the first major junction reached, or ``None`` if none
        is found within ``max_hops``.
    """
    start = net.getEdge(edge_id)
    base_heading = edge_heading_into_node(start)
    q = deque([(edge_id, 0)])
    visited = {edge_id}
    while q:
        eid, hops = q.popleft()
        edge = net.getEdge(eid)
        from_node = edge.getFromNode()
        if from_node.getID() != origin_node_id and (
            from_node.getID() in camera_node_ids or _is_major_junction(from_node)
        ):
            return from_node.getID()
        if hops >= max_hops:
            continue
        for prev_edge, conns in edge.getIncoming().items():
            pid = prev_edge.getID()
            if pid.startswith(":") or not conns or not prev_edge.allows("passenger"):
                continue
            if pid in visited:
                continue
            # Skip the immediate reverse edge (positive vs. negative ID).
            if pid.lstrip("-") == eid.lstrip("-") and pid != eid:
                continue
            # Keep the search directed — see docstring rationale.
            if _angle_diff(edge_heading_into_node(prev_edge), base_heading) > direction_thresh_deg:
                continue
            visited.add(pid)
            q.append((pid, hops + 1))
    return None


def identify_internal_inflows(
    net,
    mapped_results: Iterable[dict],
    max_hops: int = 6,
    axis_thresh_deg: float = 45.0,
) -> dict[str, set[str]]:
    """Mark which TMC approaches are corridor-internal (and thus should not spawn vehicles).

    Args:
        net: Loaded ``sumolib.net.Net``.
        mapped_results: The output of :func:`map_intersections`.
        max_hops: Upper bound on the upstream BFS depth.
        axis_thresh_deg: Heading tolerance for upstream traversal. The actual
            threshold passed to :func:`_first_upstream_major_junction` is
            ``min(axis_thresh_deg, 35.0)`` for a slightly stricter alignment.

    Returns:
        ``{intersection_id: {direction, ...}}`` mapping each matched TMC
        intersection to the set of cardinal directions whose approach is
        internal to the corridor (already covered by another TMC observation).
    """
    matched = [r for r in mapped_results if r["status"] == "matched"]
    internal: dict[str, set[str]] = {r["intersection_id"]: set() for r in matched}
    camera_by_node = {r["matched_node_id"]: r["intersection_id"] for r in matched}

    for r in matched:
        iid = r["intersection_id"]
        for d, info in r["approaches"].items():
            upstream_node_id = _first_upstream_major_junction(
                net,
                info["edge_id"],
                r["matched_node_id"],
                set(camera_by_node),
                max_hops=max_hops,
                direction_thresh_deg=min(axis_thresh_deg, 35.0),
            )
            if upstream_node_id in camera_by_node:
                internal[iid].add(d)

    return internal


def build_boundary_json(
    net,
    tmc: TMCData,
    mapped_results: list[dict],
    internal_max_hops: int,
    internal_axis_thresh_deg: float,
) -> dict:
    """Produce the boundary JSON consumed by the OD flow source.

    The boundary JSON keeps the same shape as the normalized TMC payload but with
    two filters applied: (a) directions whose first upstream major junction is
    another TMC observation are removed (internal duplicates), and (b) turn counts
    are reallocated to the legal SUMO turns at each approach.

    Args:
        net: Loaded ``sumolib.net.Net``.
        tmc: Parsed :class:`TMCData` payload (used to preserve top-level
            metadata like ``corridor_id`` in the output).
        mapped_results: Output of :func:`map_intersections`.
        internal_max_hops: Forwarded to :func:`identify_internal_inflows`.
        internal_axis_thresh_deg: Forwarded to :func:`identify_internal_inflows`.

    Returns:
        A serialisable dict capturing the boundary state, including diagnostic
        fields under ``injection_strategy`` that record exactly which
        directions were dropped and why.
    """
    mapped_by_id = {r["intersection_id"]: r for r in mapped_results}
    internal_dirs = identify_internal_inflows(
        net,
        mapped_results,
        max_hops=internal_max_hops,
        axis_thresh_deg=internal_axis_thresh_deg,
    )

    raw = tmc.raw
    out = {
        "schema_version": raw.get("schema_version", "1.0"),
        "corridor_id": raw.get("corridor_id"),
        "observation_interval": raw.get("observation_interval"),
        "bucket_seconds": raw.get("bucket_seconds"),
        "window": raw.get("window"),
        "intersections": [],
        "injection_strategy": {
            "type": "directed_upstream_first_major_junction",
            "notes": (
                "A direction is internal only when its first upstream major "
                "junction is also covered by a camera."
            ),
            "removed_internal_directions": {},
            "dropped_unmapped_or_unusable_directions": {},
        },
    }

    for inter in tmc.intersections:
        iid = inter.intersection_id
        counts = inter.counts
        mapped = mapped_by_id.get(iid)
        kept: dict[str, dict[str, float]] = {}
        removed_internal: list[str] = []
        dropped_unusable: list[str] = []

        if not mapped or mapped["status"] != "matched":
            out["injection_strategy"]["dropped_unmapped_or_unusable_directions"][iid] = list(counts.keys())
            out["intersections"].append({
                "intersection_id": iid,
                "lat": inter.lat,
                "lon": inter.lon,
                "counts": {},
            })
            continue

        approach_dirs = set(mapped["approaches"].keys())
        for d, turn_counts in counts.items():
            if d not in approach_dirs:
                dropped_unusable.append(d)
                continue
            if d in internal_dirs.get(iid, set()):
                removed_internal.append(d)
                continue
            moves = mapped["approaches"][d]["movements"]
            cleaned = redistributed_turn_counts(turn_counts, set(moves))
            if cleaned:
                kept[d] = cleaned

        if removed_internal:
            out["injection_strategy"]["removed_internal_directions"][iid] = removed_internal
        if dropped_unusable:
            out["injection_strategy"]["dropped_unmapped_or_unusable_directions"][iid] = dropped_unusable

        out["intersections"].append({
            "intersection_id": iid,
            "lat": inter.lat,
            "lon": inter.lon,
            "counts": kept,
        })

    return out
