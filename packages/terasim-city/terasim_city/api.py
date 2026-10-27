"""High-level Python API for the TMC pipeline.

This module exists purely to give callers a single function they can import
and invoke. It does not introduce any new logic; it simply chains together the
single-responsibility components (flow source + ``ScenarioAssembler``)
and writes a ``metadata.json`` for audit purposes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from lxml import etree as ET

from terasim_city.binding import BINDING_RADIUS_M
from terasim_city.flow.descriptors import GeneratedScenario
from terasim_city.flow.sources.base import FlowSource
from terasim_city.flow.sources.corridor_graph import CorridorGraphFlowSource
from terasim_city.flow.sources.scenario_routes import ScenarioRoutesFlowSource
from terasim_city.scenario.assembler import ScenarioAssembler
from terasim_city.scenario.sumocfg_io import parse_sumocfg

logger = logging.getLogger(__name__)

#: Registry mapping a flow-source name to its constructor. ``corridor_graph``
#: (OD peel-off from the TMC counts) is the default; ``scenario_routes`` reuses
#: the base scenario's own route files. Further empirical sources (loop
#: detectors, OD matrices) plug in without touching the pipeline. Every
#: constructor is called as ``constructor(tmc_json, base_sumocfg=..., **tuning)``.
FLOW_SOURCES: dict[str, type[FlowSource]] = {
    "corridor_graph": CorridorGraphFlowSource,
    "scenario_routes": ScenarioRoutesFlowSource,
}

#: The default flow source used when a caller does not pick one explicitly.
DEFAULT_FLOW_SOURCE = "corridor_graph"


def build_flow_source(
    name: str,
    tmc_json: Path | str,
    *,
    base_sumocfg: Path | str | None = None,
    **kwargs,
) -> FlowSource:
    """Instantiate the flow source registered under ``name``.

    ``base_sumocfg`` is handed to every source so one can read the base
    scenario; sources that do not need it accept and ignore it.

    Raises:
        ValueError: If ``name`` is not a registered flow source.
    """
    try:
        constructor = FLOW_SOURCES[name]
    except KeyError:
        raise ValueError(
            f"unknown flow_source {name!r}; known sources: "
            f"{sorted(FLOW_SOURCES)}"
        ) from None
    return constructor(tmc_json, base_sumocfg=base_sumocfg, **kwargs)


def run_tmc_pipeline(
    tmc_json: Path | str,
    base_sumocfg: Path | str,
    output_dir: Path | str,
    *,
    sim_end: float = 1800.0,
    seed: int = 42,
    exit_hops: int = 2,
    spawn_upstream_hops: int = 6,
    max_corridor_hops: int = 8,
    allow_uturn: bool = True,
    max_match_distance_m: float = BINDING_RADIUS_M,
    flow_scale: float = 1.0,
    min_route_edges: int = 5,
    internal_axis_thresh_deg: float = 45.0,
    flow_source: str = DEFAULT_FLOW_SOURCE,
) -> GeneratedScenario:
    """End-to-end driver: TMC JSON + base sumocfg → runnable scenario directory.

    Steps:
      1. Parse the base sumocfg to discover the ``net.xml`` path.
      2. Run the flow source to write ``routes.rou.xml`` (plus
         debugging artifacts). ``corridor_graph`` synthesises it from the TMC
         counts; ``scenario_routes`` copies the base sumocfg's own route files.
      3. Run :class:`ScenarioAssembler` to write ``simulation.sumocfg``.
      4. Write ``metadata.json`` capturing the run context.

    Args:
        tmc_json: Path to the TMC payload JSON.
        base_sumocfg: Path to the SUMO project sumocfg whose net/tls/view we
            want to reuse.
        output_dir: Where to drop the generated files. Created if missing.
        sim_end: Simulation horizon (seconds).
        seed: SUMO random seed pinned in the new sumocfg.
        exit_hops, spawn_upstream_hops, max_corridor_hops, allow_uturn,
            max_match_distance_m, flow_scale, min_route_edges,
            internal_axis_thresh_deg: Forwarded verbatim to the flow source.
            See that class for the per-parameter documentation;
            ``scenario_routes`` ignores them.
        flow_source: Which registered flow source to use;
            ``"corridor_graph"`` (the OD peel-off source) is the default,
            ``"scenario_routes"`` uses the base sumocfg's route files as the
            demand, as-is.

    Returns:
        A :class:`GeneratedScenario` describing the freshly written files.
    """
    tmc_json = Path(tmc_json).resolve()
    base_sumocfg = Path(base_sumocfg).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    base_info = parse_sumocfg(base_sumocfg)

    # ---- Stage 1: generate routes -------------------------------------
    source = build_flow_source(
        flow_source,
        tmc_json,
        base_sumocfg=base_sumocfg,
        exit_hops=exit_hops,
        spawn_upstream_hops=spawn_upstream_hops,
        max_corridor_hops=max_corridor_hops,
        allow_uturn=allow_uturn,
        max_match_distance_m=max_match_distance_m,
        flow_scale=flow_scale,
        min_route_edges=min_route_edges,
        internal_axis_thresh_deg=internal_axis_thresh_deg,
    )
    routes_file = output_dir / "routes.rou.xml"
    source.generate(
        net_path=base_info.net_file,
        output_path=routes_file,
        sim_end=sim_end,
    )

    # ---- Stage 2: assemble scenario -----------------------------------
    assembler = ScenarioAssembler(sim_end=sim_end, seed=seed)
    sumocfg_file = assembler.assemble(
        routes_file=routes_file,
        base_sumocfg=base_sumocfg,
        output_dir=output_dir,
    )

    # ---- Stage 3: metadata --------------------------------------------
    n_flows, total_vph = _summarise_routes(routes_file)
    metadata_file = output_dir / "metadata.json"
    metadata: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "type": source.source_name,
            "flow_source": source.source_name,
            "tmc_json": str(tmc_json),
            "corridor_id": source.tmc.corridor_id,
            "observation_interval": source.tmc.observation_interval,
            "timestamp_start": source.tmc.timestamp_start,
            "n_intersections": len(source.tmc.intersections),
        },
        "base_sumocfg": str(base_sumocfg),
        "net_file": str(base_info.net_file),
        "params": {
            "sim_end": sim_end,
            "seed": seed,
            "exit_hops": exit_hops,
            "spawn_upstream_hops": spawn_upstream_hops,
            "max_corridor_hops": max_corridor_hops,
            "allow_uturn": allow_uturn,
            "max_match_distance_m": max_match_distance_m,
            "flow_scale": flow_scale,
            "min_route_edges": min_route_edges,
            "internal_axis_thresh_deg": internal_axis_thresh_deg,
            "flow_source": flow_source,
        },
        "output": {
            "n_flows": n_flows,
            "total_vehs_per_hour": round(total_vph, 4),
        },
    }
    metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return GeneratedScenario(
        output_dir=output_dir,
        routes_file=routes_file.resolve(),
        sumocfg_file=sumocfg_file,
        metadata_file=metadata_file.resolve(),
        net_file=base_info.net_file,
        n_flows=n_flows,
        total_vehs_per_hour=total_vph,
    )


def _summarise_routes(routes_file: Path) -> tuple[int, float]:
    """Compute ``(n_flows, sum_vehsPerHour)`` from the generated rou.xml."""
    if not routes_file.exists():
        return 0, 0.0
    tree = ET.parse(str(routes_file))
    root = tree.getroot()
    flows = root.findall(".//flow")
    total = 0.0
    for flow in flows:
        raw = flow.get("vehsPerHour", "0")
        try:
            total += float(raw)
        except ValueError:
            # A malformed vehsPerHour zeroes the row but should not silently
            # corrupt the summary; surface it so misgenerated routes are
            # noticed at audit time.
            logger.warning(
                "ignoring flow %r in %s: vehsPerHour=%r is not a float",
                flow.get("id"),
                routes_file,
                raw,
            )
            continue
    return len(flows), total
