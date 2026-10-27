"""Flow generation from a single traffic-count file, map found automatically.

This is the core flow-generation entry point, with the business boundary
drawn where it belongs: the caller hands over ONE flow file (aggregated TMC turning
counts — how it was produced, buffered or windowed is the business side's
concern) and this module does the rest:

1. **Validate** the flow file (:class:`~terasim_city.flow.descriptors.TMCData`
   strict parsing).
2. **Find the map**: query the :class:`~terasim_city.maps.MapStore` for the
   stored map whose WGS84 bounding box covers EVERY intersection in the flow
   file (a corridor never spans two maps). When several qualify the store's
   source preference applies (prebuilt > OSM). An explicit ``map_id`` skips
   the lookup.
3. **Generate**: run the existing pipeline (match → boundary → OD peel-off →
   ``routes.rou.xml``) against the chosen map's base sumocfg.

No knowledge of snapshots, queues or aggregation windows exists here — the
one flow file is the entire input contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from terasim_city.flow.descriptors import GeneratedScenario, TMCData
from terasim_city.maps import MapPackage, MapStore


@dataclass(frozen=True)
class FlowGenerationResult:
    """What :func:`generate_flow` produced and which stored map it used."""

    map_id: str
    package: MapPackage
    scenario: GeneratedScenario


def generate_flow(
    flow_json: Path | str,
    output_dir: Path | str,
    *,
    map_id: str | None = None,
    data_root: Path | str | None = None,
    store: MapStore | None = None,
    **tuning,
) -> FlowGenerationResult:
    """Turn one traffic-count file into a runnable SUMO flow scenario.

    Args:
        flow_json: The flow input file (TMC per-intersection turning
            counts with lat/lon anchors). The only business input.
        output_dir: Where the generated ``routes.rou.xml`` / scenario land.
        map_id: Use this stored map explicitly instead of searching by
            coverage.
        data_root: Map-store location; defaults to ``$TERASIM_CITY_DATA_ROOT``.
        store: An already-constructed :class:`MapStore` (overrides
            ``data_root``); mainly for callers that hold one anyway.
        **tuning: Forwarded verbatim to
            :func:`terasim_city.api.run_tmc_pipeline` (``sim_end``, ``seed``,
            ``flow_scale``, ``flow_source``, hop/tolerance parameters, ...).

    Raises:
        ValueError: Flow file invalid, or no single stored map covers every
            intersection in it (the error lists uncovered intersections and
            the stored maps' bounding boxes).
        KeyError: ``map_id`` given but not in the store.
    """
    # Local import: api.py imports flow submodules, so importing it at module
    # scope would be circular.
    from terasim_city.api import run_tmc_pipeline

    flow_json = Path(flow_json).resolve()
    tmc = TMCData.from_json(flow_json)  # fail loud before touching the store

    if store is None:
        store = MapStore(data_root)
    if map_id is not None:
        package = store.get(map_id)
    else:
        map_id, package = select_map_for(store, tmc)

    scenario = run_tmc_pipeline(
        flow_json, package.base_sumocfg, output_dir, **tuning
    )
    return FlowGenerationResult(map_id=map_id, package=package, scenario=scenario)


def select_map_for(
    store: MapStore,
    tmc: TMCData,
    *,
    extra_points: Iterable[tuple[float, float]] = (),
) -> tuple[str, MapPackage]:
    """The stored map covering EVERY intersection, best source first.

    A corridor must sit entirely inside one map: partial coverage is rejected
    with a diagnostic rather than silently stitching maps together.

    ``extra_points`` are additional ``(lat, lon)`` positions the map must also
    cover — the optimization target's coordinates, so a target outside the TMC
    camera footprint cannot be handed a map that does not contain it.
    """
    per_point: list[tuple[str, list[tuple[str, MapPackage]]]] = []
    for it in tmc.intersections:
        hits = store.find_covering(lat=it.lat, lon=it.lon)
        per_point.append((it.intersection_id, hits))
    for lat, lon in extra_points:
        hits = store.find_covering(lat=float(lat), lon=float(lon))
        per_point.append((f"target({lat},{lon})", hits))

    covering_ids = [
        {hit_id for hit_id, _ in hits} for _iid, hits in per_point
    ]
    common = set.intersection(*covering_ids) if covering_ids else set()
    if not common:
        uncovered = [iid for (iid, hits), ids in zip(per_point, covering_ids) if not ids]
        stored = [
            f"{mid} (source={pkg.source}, bbox={pkg.metadata.get('orig_boundary')})"
            for mid, pkg in store.list_packages()
        ]
        detail: list[str] = []
        if uncovered:
            detail.append(
                f"intersections not covered by any stored map: {uncovered}"
            )
        else:
            detail.append(
                "every intersection is covered by some map, but no single "
                "map covers all of them (a corridor must sit inside one map)"
            )
        detail.append(
            "stored maps: " + ("; ".join(stored) if stored else "(store is empty)")
        )
        raise ValueError(
            "no stored map covers the flow file's corridor — " + " | ".join(detail)
        )

    # find_covering already orders each hit list by source preference; take
    # the first hit (of the first intersection) that covers everyone.
    for hit_id, pkg in per_point[0][1]:
        if hit_id in common:
            return hit_id, pkg
    raise AssertionError("unreachable: common id missing from first hit list")


__all__ = ["FlowGenerationResult", "generate_flow"]
