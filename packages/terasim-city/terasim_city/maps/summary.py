"""Conversion summary of a map package: what the converted SUMO net contains.

Every provider records a ``metadata["conversion"]`` block (edges, junctions,
traffic lights, connectivity, geo-reference, warnings) so the upload surface
can tell the caller what it just registered without re-reading the net. The
net is streamed, so multi-hundred-MB nets are summarized in bounded memory.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

from terasim_city.maps.base import MapPackage
from terasim_city.maps.geo import read_location
from terasim_city.maps.validate import map_package_warnings, net_is_geo_referenced

#: A junction with at least this many distinct neighbouring junctions is an
#: intersection (a mid-block node joining two road pieces has two).
INTERSECTION_MIN_NEIGHBOURS = 3
#: Below this share of edges in the largest connected component the summary
#: warns that the net is fragmented; below ``UNUSABLE_SHARE`` it says so loudly.
FRAGMENTED_SHARE = 0.8
UNUSABLE_SHARE = 0.5
#: Boundary edges (entering/leaving the map) are never strongly connected, so
#: a city extract typically scores 0.5-0.8; below this the map barely routes.
POORLY_ROUTABLE_SHARE = 0.2
#: Below this many drivable edges the strong-connectivity check is skipped
#: (in a tiny extract most edges are boundary edges).
STRONG_CHECK_MIN_EDGES = 50


def net_summary(net_file: Path | str) -> dict:
    """Counts and connectivity of a SUMO net, internal elements excluded.

    * ``edges`` / ``lanes`` — normal (non-internal) edges and their lanes.
    * ``junctions`` — non-internal junctions; ``intersections`` — those with
      ``INTERSECTION_MIN_NEIGHBOURS`` or more distinct neighbouring junctions;
      ``signalized_junctions`` — ``traffic_light*`` typed junctions.
    * ``traffic_lights`` — distinct ``<tlLogic>`` ids (signal programs).
    * ``drivable_edges`` — edges with at least one lane passenger cars may use;
      the connectivity measures below are computed over these.
    * ``largest_component_share`` — share of drivable edges in the largest
      weakly connected component of the connection graph (1.0 = fully
      connected; low values flag an unconnected, unusable conversion).
    * ``largest_strong_component_share`` — the same for the largest strongly
      connected component (edges that can all reach each other by driving).
    """
    edges: dict[str, tuple[str, str]] = {}
    drivable: set[str] = set()
    lanes = 0
    junction_types: Counter = Counter()
    tls_ids: set[str] = set()
    links: list[tuple[str, str]] = []
    for _event, elem in ET.iterparse(str(net_file), events=("end",)):
        tag = elem.tag
        if tag == "edge":
            if elem.get("function") != "internal":
                edge_id = elem.get("id", "")
                edges[edge_id] = (elem.get("from", ""), elem.get("to", ""))
                edge_lanes = [child for child in elem if child.tag == "lane"]
                lanes += len(edge_lanes)
                if any(_allows_cars(lane) for lane in edge_lanes):
                    drivable.add(edge_id)
            elem.clear()
        elif tag == "junction":
            if elem.get("type") != "internal":
                junction_types[elem.get("type", "")] += 1
            elem.clear()
        elif tag == "tlLogic":
            tls_ids.add(elem.get("id", ""))
            elem.clear()
        elif tag == "connection":
            src, dst = elem.get("from", ""), elem.get("to", "")
            if not src.startswith(":") and not dst.startswith(":"):
                links.append((src, dst))
            elem.clear()

    neighbours: dict[str, set[str]] = defaultdict(set)
    for src, dst in edges.values():
        if src and dst and src != dst:
            neighbours[src].add(dst)
            neighbours[dst].add(src)
    intersections = sum(
        1 for nbrs in neighbours.values() if len(nbrs) >= INTERSECTION_MIN_NEIGHBOURS
    )

    # Connectivity is measured on the car network: footpaths, bike lanes and
    # "none"-typed placeholder lanes neither help nor hurt routing.
    parent = {edge_id: edge_id for edge_id in drivable}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for src, dst in links:
        if src in parent and dst in parent:
            root_a, root_b = find(src), find(dst)
            if root_a != root_b:
                parent[root_a] = root_b
    sizes = Counter(find(edge_id) for edge_id in drivable)
    largest = max(sizes.values()) if sizes else 0
    strong = _largest_strong_component(drivable, links)

    return {
        "edges": len(edges),
        "lanes": lanes,
        "junctions": sum(junction_types.values()),
        "intersections": intersections,
        "signalized_junctions": sum(
            n for kind, n in junction_types.items() if kind.startswith("traffic_light")
        ),
        "traffic_lights": len(tls_ids),
        "dead_ends": junction_types.get("dead_end", 0),
        "drivable_edges": len(drivable),
        "components": len(sizes),
        "largest_component_share": round(largest / len(drivable), 4) if drivable else 0.0,
        "largest_strong_component_share": round(strong / len(drivable), 4) if drivable else 0.0,
    }


def _allows_cars(lane) -> bool:
    """Whether a ``<lane>`` of a net admits passenger cars (SUMO allow/disallow lists)."""
    allow = lane.get("allow")
    if allow is not None:
        return bool({"passenger", "all"} & set(allow.split()))
    disallow = set((lane.get("disallow") or "").split())
    return not ({"passenger", "all"} & disallow)


def _largest_strong_component(edges, links: list[tuple[str, str]]) -> int:
    """Edges in the largest strongly connected component (every edge reaches every other).

    Weak connectivity can look fine while most routes are impossible (one-way
    dead ends, missing turn links); this is the directed measure. Iterative
    Kosaraju, so deep road chains do not hit Python's recursion limit.
    """
    succ: dict[str, list[str]] = defaultdict(list)
    pred: dict[str, list[str]] = defaultdict(list)
    for src, dst in set(links):
        if src in edges and dst in edges:
            succ[src].append(dst)
            pred[dst].append(src)
    order: list[str] = []
    visited: set[str] = set()
    for root in edges:
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(succ[root]))]
        while stack:
            node, children = stack[-1]
            for child in children:
                if child not in visited:
                    visited.add(child)
                    stack.append((child, iter(succ[child])))
                    break
            else:
                order.append(node)
                stack.pop()
    assigned: set[str] = set()
    best = 0
    for root in reversed(order):
        if root in assigned:
            continue
        assigned.add(root)
        size, stack = 0, [root]
        while stack:
            node = stack.pop()
            size += 1
            for parent in pred[node]:
                if parent not in assigned:
                    assigned.add(parent)
                    stack.append(parent)
        best = max(best, size)
    return best


_NUMBERS = re.compile(r"-?\d+(?:\.\d+)?")
_QUOTED = re.compile(r"'[^']*'")


def netconvert_warning_summary(stderr: str, *, top: int = 3) -> dict:
    """Count netconvert's ``Warning:`` lines and name the most frequent kinds.

    Ids and numbers are blanked so ``Warning: Edge '12' ...`` and ``Warning:
    Edge '97' ...`` count as one kind. netconvert's own trailing "N total
    messages of type" lines are skipped (they would double count).
    """
    kinds: Counter = Counter()
    for line in (stderr or "").splitlines():
        line = line.strip()
        if not line.startswith("Warning:") or "total messages of type" in line:
            continue
        kind = _NUMBERS.sub("#", _QUOTED.sub("'…'", line[len("Warning:"):].strip()))
        kinds[kind] += 1
    return {
        "count": sum(kinds.values()),
        "top": [f"{n}x {kind}" for kind, n in kinds.most_common(top)],
    }


def conversion_summary(
    net_file: Path | str,
    *,
    source_format: str,
    warnings: list[str] | tuple[str, ...] = (),
    **extra,
) -> dict:
    """The ``metadata["conversion"]`` block a provider stores in its manifest.

    Combines :func:`net_summary` with the geo-reference state (projection,
    WGS84 boundary) and caller-supplied ``warnings`` / ``extra`` fields
    (conversion time, signal source, driving side, ...).
    """
    location = read_location(net_file) or {}
    stats = net_summary(net_file)
    warnings = list(warnings)
    share = stats["largest_component_share"]
    if stats["drivable_edges"] and share < FRAGMENTED_SHARE:
        severity = "severely fragmented" if share < UNUSABLE_SHARE else "fragmented"
        warnings.append(
            f"the network is {severity}: only {share:.0%} of the edges form one connected "
            f"network ({stats['components']} pieces), so most routes cannot be built — "
            "check the source map's lane/road links"
        )
    strong = stats["largest_strong_component_share"]
    if stats["drivable_edges"] >= STRONG_CHECK_MIN_EDGES and strong < POORLY_ROUTABLE_SHARE:
        warnings.append(
            f"only {strong:.0%} of the edges can all reach each other by driving (largest "
            "strongly connected component): most origin-destination pairs have no route — "
            "check one-way links and missing turn connections in the source map"
        )
    if stats["edges"] and stats["intersections"] == 0:
        warnings.append(
            "no intersection was built (no junction joins three or more roads): the source map's "
            "junction topology did not survive conversion"
        )
    return {
        "format": source_format,
        **stats,
        "geo_referenced": net_is_geo_referenced(net_file),
        "proj": location.get("projParameter"),
        **extra,
        "warnings": warnings,
    }


def ensure_net_loads(net_file: Path | str, *, source: Path | str) -> None:
    """Fail loud when SUMO cannot load the converted net (see :func:`sumo_load_problem`).

    Raises:
        MapConversionError: SUMO rejected the net; the message carries SUMO's
            errors and the net's statistics so the caller can judge the source.
    """
    from terasim_city.maps.osm import MapConversionError, sumo_load_problem

    problem = sumo_load_problem(Path(net_file))
    if problem is None:
        return
    stats = net_summary(net_file)
    raise MapConversionError(
        f"{source}: netconvert wrote a network SUMO cannot load ({problem}); the source "
        f"map's geometry/topology is too broken to convert as-is (converted net: "
        f"{stats['edges']} edges, {stats['intersections']} intersections, "
        f"{stats['largest_component_share']:.0%} connected) — fix the source map"
    )


def map_summary(pkg: MapPackage) -> dict:
    """The conversion summary of a stored package.

    Converted packages carry it in ``metadata["conversion"]``; for prebuilt
    scenarios and manifests written before summaries existed it is computed
    from the net on the fly (not persisted).
    """
    stored = pkg.metadata.get("conversion")
    if isinstance(stored, dict):
        return stored
    return conversion_summary(
        pkg.net_file, source_format=pkg.source, warnings=map_package_warnings(pkg)
    )


__all__ = [
    "FRAGMENTED_SHARE",
    "INTERSECTION_MIN_NEIGHBOURS",
    "POORLY_ROUTABLE_SHARE",
    "STRONG_CHECK_MIN_EDGES",
    "UNUSABLE_SHARE",
    "conversion_summary",
    "ensure_net_loads",
    "map_summary",
    "net_summary",
    "netconvert_warning_summary",
]
