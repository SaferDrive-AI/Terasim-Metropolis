"""Serialize a list of :class:`FlowDescriptor` into a SUMO ``rou.xml`` file.

This module is the only place in the package that writes SUMO route XML.
Keeping the writer separate from the source-specific logic means future flow
sources (OD matrix, loop detectors, ...) can reuse the same serialization
behaviour by producing the same descriptor type.

Note: this writer optionally sanitises the route edge sequences and pads them
with upstream/downstream hops, using the helpers in
:mod:`terasim_city.flow.network.topology`. We do this here, just before
serialisation, so the flow-source stage stays focused on graph
exploration and we keep all "make the route SUMO-friendly" logic in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from lxml import etree as ET

from terasim_city.flow.descriptors import FlowDescriptor
from terasim_city.flow.network.topology import (
    _ensure_min_route_length,
    _extend_route_upstream,
    _sanitize_connected_route,
)


#: The default vehicle type definition emitted at the top of every rou.xml.
#: Mirrors the ``car_normal`` definition used by the original demo so that
#: downstream tooling expecting that vType continues to work.
DEFAULT_VTYPE: dict[str, str] = {
    "id": "car_normal",
    "vClass": "passenger",
    "accel": "2.6",
    "decel": "4.5",
    "maxSpeed": "33.3",
    "sigma": "0.5",
    "tau": "1.2",
    "length": "4.5",
    "minGap": "2.5",
}


def write_routes_xml(
    descriptors: Iterable[FlowDescriptor],
    output_path: Path,
    *,
    net=None,
    sim_end: float | None = None,
    flow_scale: float = 1.0,
    min_route_edges: int = 5,
    allow_uturn: bool = True,
    spawn_upstream_hops: int = 6,
) -> tuple[int, float, dict[str, int]]:
    """Write a list of :class:`FlowDescriptor` to ``output_path`` as SUMO XML.

    Args:
        descriptors: The flow descriptors produced by a :class:`FlowSource`.
        output_path: Destination file path. Parent directories are created
            automatically.
        net: Optional ``sumolib.net.Net``. When provided, each route is
            sanitised, padded upstream, and grown to at least
            ``min_route_edges`` edges. When omitted, descriptors are written
            verbatim — useful for unit tests that pre-construct routes.
        sim_end: If supplied, overrides every descriptor's ``end`` field
            (typical for one-shot pipeline runs). When omitted, each
            descriptor's own ``end`` is used (or no ``end`` attribute is
            emitted if the descriptor's ``end`` is ``None``).
        flow_scale: Multiplier applied to every ``vehs_per_hour`` value.
            Useful for scenario sensitivity sweeps.
        min_route_edges: Minimum route length when ``net`` is provided.
        allow_uturn: Forwarded to the topology helpers.
        spawn_upstream_hops: Hop cap for the upstream spawn runway. The walk
            stops early once it reaches a natural-length approach edge, so this
            is an upper bound, not an exact count.

    Returns:
        ``(flow_count, total_emitted_vehs_per_hour, start_edge_counts)``.
        ``start_edge_counts`` maps each spawn edge ID to how many flows start
        there — handy for sanity-checking spawn distribution.
    """
    # lxml manages XML namespaces via the nsmap parameter rather than a
    # bare ``xmlns:xsi`` attribute, so we declare the schema namespace at
    # construction time and emit the noNamespaceSchemaLocation hint through
    # the namespaced attribute API.
    nsmap = {"xsi": "http://www.w3.org/2001/XMLSchema-instance"}
    root = ET.Element(
        "routes",
        nsmap=nsmap,
        attrib={
            "{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation":
                "http://sumo.dlr.de/xsd/routes_file.xsd",
        },
    )

    # Emit the shared default vType once at the top of the file.
    ET.SubElement(root, "vType", DEFAULT_VTYPE)

    flow_count = 0
    emitted_vehph = 0.0
    start_edge_counts: dict[str, int] = {}

    for descriptor in descriptors:
        scaled = descriptor.vehs_per_hour * flow_scale
        if scaled <= 0.01:
            # Drop sub-noise flows so SUMO does not emit ghost vehicles.
            continue

        edges = list(descriptor.route_edges)
        if net is not None:
            edges = _sanitize_connected_route(edges, net)
            edges = _extend_route_upstream(edges, net, upstream_hops=spawn_upstream_hops)
            edges = _sanitize_connected_route(edges, net)
            edges = _ensure_min_route_length(
                edges, net, min_edges=min_route_edges, allow_uturn=allow_uturn
            )
            edges = _sanitize_connected_route(edges, net)

        if len(edges) < 2:
            # SUMO refuses single-edge routes.
            continue

        # Determine the effective end time for the flow.
        effective_end = sim_end if sim_end is not None else descriptor.end

        ET.SubElement(
            root,
            "route",
            {"id": descriptor.route_id, "edges": " ".join(edges)},
        )
        start_edge_counts[edges[0]] = start_edge_counts.get(edges[0], 0) + 1

        flow_attrs = {
            "id": descriptor.flow_id,
            "type": descriptor.vehicle_type,
            "begin": f"{descriptor.begin:g}",
            "vehsPerHour": f"{scaled:.2f}",
            "route": descriptor.route_id,
            "departLane": descriptor.depart_lane,
            "departSpeed": descriptor.depart_speed,
        }
        if effective_end is not None:
            flow_attrs["end"] = f"{effective_end:g}"

        ET.SubElement(root, "flow", flow_attrs)
        flow_count += 1
        emitted_vehph += scaled

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    tree.write(
        str(output_path),
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=True,
    )

    return flow_count, emitted_vehph, start_edge_counts


__all__ = ["DEFAULT_VTYPE", "write_routes_xml"]
