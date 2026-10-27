"""Building blocks for Traffic Operation plug-ins, on top of the core pipeline.

Each helper calls the same terasim-city code the built-in optimizer runs, so a
third-party module simulates, scores and exports plans exactly like
``run_corridor_optimization`` does. A typical ``run`` (see
``examples/plugins/webster_fixed_time``)::

    scenario = prepare_scenario(request)                     # TMC + map -> one shared SUMO scenario
    current, notes = baseline_plan(scenario, request.current_plan)
    candidate = ...                                          # the module's own logic
    scores = evaluate_plans(scenario, [current, candidate], progress=progress)
    winner = best_plan(scores)
    nema = export_nema(scenario, winner.plan)                # <output_dir>/nema_plan.json
    return build_result(self, scenario, nema_plan=nema, baseline=scores[0],
                        optimized=winner, notes=notes, evaluated=scores)

Plans are the core's plan objects: ``terasim_city.plans.CorridorPlan`` (static
per-signal programs) or ``terasim_city.plans.nema_plan.NemaCorridorPlan``
(dual-ring NEMA timing).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from terasim_plugin_sdk import _core
from terasim_plugin_sdk.constants import NEMA_FILENAME
from terasim_plugin_sdk.errors import PluginInputError, PluginRunError
from terasim_plugin_sdk.model import (
    CurrentPlan,
    KpiWeights,
    MapInput,
    OperationRequest,
    OperationResult,
    TrafficSnapshot,
)
from terasim_plugin_sdk.protocol import ProgressCallback

logger = logging.getLogger(__name__)

__all__ = [
    "PlanScore",
    "ResolvedMap",
    "Scenario",
    "baseline_plan",
    "best_plan",
    "build_kpis",
    "build_result",
    "check_vehicle_behavior",
    "evaluate_plans",
    "export_nema",
    "ingest_map",
    "ingest_nema_plan",
    "kpi_metrics",
    "kpis_from_corridor_result",
    "plan_timing",
    "prepare_scenario",
    "resolve_map",
]

#: Subdirectories of the output directory the helpers write.
SCENARIO_DIRNAME = "scenario"
TRIALS_DIRNAME = "trials"
PLAN_STORE_DIRNAME = "plan_store"


# ---------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedMap:
    """A :class:`~terasim_plugin_sdk.model.MapInput` resolved to files on disk."""

    #: The map's base SUMO config (net + additional files).
    base_sumocfg: Path
    #: The map store id, when the map came from the store.
    map_id: "str | None" = None
    #: The map package directory, when known.
    map_dir: "Path | None" = None


def ingest_map(source_file, *, data_root=None, proj: "str | None" = None) -> str:
    """Store a map file with the core map adapter and return its ``map_id``.

    Accepts OpenStreetMap / Lanelet2 (``.osm``), OpenDRIVE (``.xodr``) and prebuilt
    SUMO (``.sumocfg``) maps; HD maps are converted to a SUMO network. ``proj`` is the
    proj.4 definition of an HD map's planar coordinates when the file has no usable
    geo-reference. Identical content resolves to the same id. Use the id with
    ``MapInput.from_store(map_id, data_root=...)``.
    """
    from terasim_city.maps import MapConversionError, MapStore

    try:
        return MapStore(data_root).ingest(Path(source_file), proj=proj).map_id
    except (ValueError, MapConversionError) as exc:
        raise PluginInputError(f"cannot store map {source_file}: {exc}") from exc


def resolve_map(
    map_input: MapInput, tmc=None, *, extra_points: Sequence[tuple[float, float]] = ()
) -> ResolvedMap:
    """The base sumocfg (and store id / package directory) a map input refers to.

    ``tmc`` (the core's ``TMCData``) is needed for ``MapInput.from_store()`` without a
    ``map_id``: the stored map covering every TMC intersection (and ``extra_points``,
    ``(lat, lon)`` pairs) is selected with the core's rule.
    """
    from terasim_city.maps.base import MANIFEST_NAME

    if map_input.kind == "sumocfg":
        sumocfg = Path(map_input.sumocfg)
        if not sumocfg.is_file():
            raise PluginInputError(f"map file not found: {sumocfg}")
        map_dir = sumocfg.parent if (sumocfg.parent / MANIFEST_NAME).exists() else None
        return ResolvedMap(base_sumocfg=sumocfg, map_dir=map_dir)

    from terasim_city.maps.store import MapStore

    try:
        store = MapStore(map_input.data_root)
    except ValueError as exc:
        raise PluginInputError(str(exc)) from exc
    if map_input.kind == "store":
        try:
            package = store.get(map_input.map_id)
        except (KeyError, ValueError, FileNotFoundError) as exc:
            reason = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
            raise PluginInputError(f"map {map_input.map_id!r}: {reason}") from exc
        return ResolvedMap(
            base_sumocfg=package.base_sumocfg, map_id=map_input.map_id, map_dir=package.root
        )
    if tmc is None:
        raise PluginInputError("selecting the covering stored map needs the TMC data")
    from terasim_city.flow.generate import select_map_for

    try:
        map_id, package = select_map_for(store, tmc, extra_points=tuple(extra_points))
    except ValueError as exc:
        raise PluginInputError(str(exc)) from exc
    return ResolvedMap(base_sumocfg=package.base_sumocfg, map_id=map_id, map_dir=package.root)


def _latlon_points(*refs) -> list[tuple[float, float]]:
    """``"lat,lon"`` corridor endpoints, which the selected map must cover too (as in the core)."""
    points = []
    for ref in refs:
        parts = str(ref).split(",") if ref else ()
        if len(parts) == 2:
            try:
                points.append((float(parts[0]), float(parts[1])))
            except ValueError:
                pass
    return points


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """The one simulation scenario every candidate plan of a run is evaluated on.

    Demand and network never change between trials; only the signal plan does.
    """

    request: OperationRequest
    #: The TMC file the run uses (the request's file, or its payload written to ``inputs/``).
    tmc_json: Path
    #: The parsed counts (``terasim_city.flow.descriptors.TMCData``).
    tmc: Any
    map: ResolvedMap
    #: The ordered corridor (``terasim_city.corridor.ResolvedCorridor``).
    corridor: Any
    #: The generated scenario config (routes from the TMC, teleporting disabled).
    sumocfg: Path
    net_file: Path

    @property
    def corridor_id(self) -> str:
        return self.tmc.corridor_id

    @property
    def tls_ids(self) -> tuple[str, ...]:
        return tuple(self.corridor.tls_ids)

    @property
    def output_dir(self) -> Path:
        return self.request.output_dir


def check_vehicle_behavior(name: str) -> None:
    """Raise PluginInputError unless ``name`` is in the core's vehicle-behavior registry."""
    from terasim_city.factories import VEHICLE_BEHAVIORS

    if name not in VEHICLE_BEHAVIORS:
        raise PluginInputError(
            f"unknown vehicle_behavior {name!r}; registered behaviors: {sorted(VEHICLE_BEHAVIORS)}"
        )


def prepare_scenario(request: OperationRequest, *, flow_source: str = "corridor_graph") -> Scenario:
    """Build the shared scenario with the core TMC pipeline.

    Resolves the map, resolves the corridor (``terasim_city.corridor.resolve_corridor``),
    generates the demand from the counts (``terasim_city.api.run_tmc_pipeline`` with the
    registered ``flow_source``) into ``<output_dir>/scenario/``, and disables
    teleporting so congestion stays in the measurement, exactly as the core
    optimizer prepares its scenario. The request's ``seed`` and ``sim_end`` are
    pinned in the scenario config.
    """
    from terasim_city.api import FLOW_SOURCES, run_tmc_pipeline
    from terasim_city.corridor import resolve_corridor
    from terasim_city.evaluation.diagnostics import disable_teleport

    request.check_inputs()
    check_vehicle_behavior(request.vehicle_behavior)
    if flow_source not in FLOW_SOURCES:
        raise PluginInputError(
            f"unknown flow_source {flow_source!r}; known: {sorted(FLOW_SOURCES)}"
        )
    request.output_dir.mkdir(parents=True, exist_ok=True)
    tmc_json = request.traffic.write_to(request.inputs_dir)
    tmc = TrafficSnapshot.from_file(tmc_json).load()
    corridor_sel = request.corridor
    resolved = resolve_map(
        request.map, tmc, extra_points=_latlon_points(corridor_sel.from_ref, corridor_sel.to_ref)
    )
    try:
        corridor = resolve_corridor(
            resolved.base_sumocfg,
            tmc_json,
            from_ref=corridor_sel.from_ref,
            to_ref=corridor_sel.to_ref,
            tls_ids=corridor_sel.tls_ids or None,
        )
        generated = run_tmc_pipeline(
            tmc_json,
            resolved.base_sumocfg,
            request.output_dir / SCENARIO_DIRNAME,
            sim_end=request.sim_end,
            seed=request.seed,
            flow_source=flow_source,
        )
    except ValueError as exc:
        raise PluginInputError(str(exc)) from exc
    sumocfg = Path(generated.sumocfg_file)
    disable_teleport(sumocfg, sim_end=request.sim_end, seed=request.seed)
    return Scenario(
        request=request,
        tmc_json=Path(tmc_json),
        tmc=tmc,
        map=resolved,
        corridor=corridor,
        sumocfg=sumocfg,
        net_file=Path(generated.net_file),
    )


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _webster_reference(
    scenario: Scenario, *, stage_mode: str, cycle_min_s: float, cycle_max_s: float
):
    from terasim_city.plans import build_webster_plan

    try:
        return build_webster_plan(
            scenario.tmc_json,
            scenario.corridor,
            scenario.net_file,
            stage_mode=stage_mode,
            cycle_min_s=cycle_min_s,
            cycle_max_s=cycle_max_s,
        )
    except ValueError as exc:
        raise PluginInputError(f"cannot build the Webster reference plan: {exc}") from exc


def baseline_plan(
    scenario: Scenario,
    current_plan: "CurrentPlan | None" = None,
    *,
    stage_mode: str = "two_stage",
    cycle_min_s: float = 45.0,
    cycle_max_s: float = 150.0,
) -> "tuple[Any, tuple[str, ...]]":
    """The plan an operation is measured against, and notes about how it was chosen.

    * ``None``: a Webster plan from the TMC counts (``build_webster_plan``; the core's
      reference when no current timing is known). ``stage_mode`` / ``cycle_min_s`` /
      ``cycle_max_s`` shape it.
    * ``"timing_sheet"``: the sheet's plan for the corridor's signals.
    * ``"scenario_programs"``: the map's own ``<tlLogic>`` programs.
    * ``"nema_plan"``: the document is stored in ``<output_dir>/inputs/plan_store`` and
      read like ``"plan_store"``.
    * ``"plan_store"``: the stored NEMA plans active at ``plan_time`` (default: the TMC
      ``timestamp_start``). When they do not cover every corridor signal, the Webster
      reference is used instead and a note says why, as the core optimizer does.
    """
    if current_plan is None:
        return _webster_reference(
            scenario, stage_mode=stage_mode, cycle_min_s=cycle_min_s, cycle_max_s=cycle_max_s
        ), ()
    request = scenario.request
    common = dict(
        tmc_json=scenario.tmc_json,
        base_sumocfg=scenario.map.base_sumocfg,
        output_dir=request.output_dir,
    )
    if current_plan.kind == "timing_sheet":
        try:
            return _core.timing_sheet_plan(
                current_plan.path, corridor=scenario.corridor, net_file=scenario.net_file, **common
            ), ()
        except (OSError, ValueError) as exc:
            raise PluginInputError(f"timing sheet {current_plan.path}: {exc}") from exc
    if current_plan.kind == "scenario_programs":
        from terasim_city.plans import load_scenario_programs

        try:
            return load_scenario_programs(scenario.map.base_sumocfg, scenario.tls_ids), ()
        except ValueError as exc:
            raise PluginInputError(f"the map's signal programs: {exc}") from exc

    store_root = Path(current_plan.path)
    if current_plan.kind == "nema_plan":
        store_root = ingest_nema_plan(current_plan.path, request.inputs_dir / PLAN_STORE_DIRNAME)
    try:
        plan, reason = _core.stored_nema_plan(
            plan_data_root=store_root,
            plan_time=current_plan.plan_time,
            corridor=scenario.corridor,
            tmc=scenario.tmc,
            **common,
        )
    except ValueError as exc:
        raise PluginInputError(f"stored NEMA plans in {store_root}: {exc}") from exc
    if plan is not None:
        return plan, ()
    note = (
        f"the current NEMA plan was not used ({reason}); the operation is measured against "
        "a Webster plan from the TMC counts"
    )
    logger.warning("%s", note)
    return _webster_reference(
        scenario, stage_mode=stage_mode, cycle_min_s=cycle_min_s, cycle_max_s=cycle_max_s
    ), (note,)


def ingest_nema_plan(document, data_root) -> Path:
    """Store a ``terasim.nema_plan.v1`` document in the plan store at ``data_root``."""
    from terasim_city.plans.store import PlanFormatError, PlanStore

    data_root = Path(data_root)
    try:
        PlanStore(data_root).ingest(Path(document))
    except (OSError, ValueError, PlanFormatError) as exc:
        raise PluginInputError(f"NEMA plan {document}: {exc}") from exc
    return data_root


def plan_timing(plan) -> dict:
    """Per-signal ``cycle_s`` / ``offsets_s`` (and ``greens_s`` for static plans) of a plan."""
    return _core.plan_timing(plan)


def kpi_metrics() -> "dict[str, str]":
    """KPI weight key -> the measure it weights (``"stops"`` -> ``"stops_per_veh"``)."""
    return _core.kpi_metrics()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanScore:
    """One plan simulated on the scenario and scored against the baseline."""

    plan: Any
    valid: bool
    #: Weighted baseline-ratio cost (``score_moe``); lower is better; None when invalid.
    cost: "float | None"
    #: The measures of effectiveness (``extract_moe`` output plus per-junction delay).
    metrics: Mapping[str, Any] = field(hash=False)
    trial_dir: str = ""
    error: "str | None" = None

    @property
    def plan_id(self) -> str:
        return self.plan.plan_id


def evaluate_plans(
    scenario: Scenario,
    plans: Sequence[Any],
    *,
    baseline_index: int = 0,
    weights: "KpiWeights | None" = None,
    n_workers: "int | None" = None,
    vehicle_behavior: "str | None" = None,
    progress: "ProgressCallback | None" = None,
    trials_dir=None,
) -> "list[PlanScore]":
    """Simulate every plan with the core ``BatchEvaluator`` and score it.

    Each plan runs in its own trial directory (``trial_0000``, ``trial_0001``, ...
    under ``trials_dir``, default ``<output_dir>/trials``; give a second call its own
    ``trials_dir``) in a fresh SUMO process, ``n_workers`` at a time. Costs are
    ``score_moe(plan, baseline)`` with ``plans[baseline_index]`` as the baseline and
    the request's weights unless ``weights`` is given. ``progress`` receives
    ``(0, len(plans), None)`` first and ``(done, len(plans), last_plan_id)`` after
    each batch; its exceptions propagate.

    Raises:
        PluginRunError: The baseline plan has no valid evaluation.
    """
    from terasim_city.evaluation.moe import score_moe
    from terasim_city.optimize.batch import BatchEvaluator

    request = scenario.request
    plans = list(plans)
    if not plans:
        raise PluginInputError("evaluate_plans needs at least one plan")
    if not 0 <= baseline_index < len(plans):
        raise PluginInputError(
            f"baseline_index {baseline_index} is outside the {len(plans)} plan(s)"
        )
    weights = weights or request.weights
    n_workers = int(n_workers or request.n_workers)
    vehicle_behavior = vehicle_behavior or request.vehicle_behavior
    check_vehicle_behavior(vehicle_behavior)
    trials_root = (
        Path(trials_dir) if trials_dir is not None else request.output_dir / TRIALS_DIRNAME
    )

    total = len(plans)
    if progress is not None:
        progress(0, total, None)
    results: list[dict] = []
    with BatchEvaluator(
        scenario_sumocfg=scenario.sumocfg,
        net_file=scenario.net_file,
        trials_root=trials_root,
        sim_end=request.sim_end,
        stalls=[],
        quality_thresholds=_core.quality_thresholds_off(),
        n_workers=n_workers,
        vehicle_behavior=vehicle_behavior,
        map_dir=scenario.map.map_dir,
    ) as evaluator:
        for start in range(0, total, n_workers):
            batch = plans[start : start + n_workers]
            results.extend(evaluator.evaluate(batch))
            if progress is not None:
                progress(len(results), total, batch[-1].plan_id)

    baseline = results[baseline_index]
    if not baseline.get("valid"):
        raise PluginRunError(
            f"the baseline plan {plans[baseline_index].plan_id!r} produced no valid evaluation: "
            f"{_failure(baseline)}"
        )
    kpi = weights.to_kpi_config()
    scores = []
    for plan, result in zip(plans, results):
        valid = bool(result.get("valid"))
        moe = dict(result.get("moe") or {})
        scores.append(
            PlanScore(
                plan=plan,
                valid=valid,
                cost=score_moe(moe, baseline["moe"], kpi) if valid else None,
                metrics=moe,
                trial_dir=str(result.get("trial_dir", "")),
                error=None if valid else _failure(result),
            )
        )
    return scores


def _failure(result: Mapping) -> str:
    return (
        result.get("error") or "; ".join(result.get("quality_errors") or ()) or "invalid evaluation"
    )


def best_plan(scores: Sequence[PlanScore]) -> PlanScore:
    """The valid score with the lowest cost (the earliest one on ties)."""
    valid = [
        score
        for score in scores
        if score.valid and score.cost is not None and math.isfinite(score.cost)
    ]
    if not valid:
        raise PluginRunError(f"none of the {len(scores)} evaluated plan(s) is valid")
    return min(valid, key=lambda score: score.cost)


# ---------------------------------------------------------------------------
# KPIs and NEMA output
# ---------------------------------------------------------------------------


def _side(cost, metrics, timing) -> dict:
    side = {"cost": cost, "metrics": dict(metrics)}
    if timing is not None:
        side["timing"] = timing
    return side


def build_kpis(baseline: PlanScore, optimized: PlanScore, weights: KpiWeights) -> dict:
    """The KPI comparison in the shape of the service's ``kpis`` block.

    ``{"cost_weights": {...}, "baseline": {"cost", "metrics", "timing"},
    "optimized": {"cost", "metrics", "timing"}}``.
    """
    for label, score in (("baseline", baseline), ("optimized", optimized)):
        if not score.valid or score.cost is None:
            raise PluginRunError(f"the {label} plan {score.plan_id!r} has no valid evaluation")
    return {
        "cost_weights": weights.to_dict(),
        "baseline": _side(baseline.cost, baseline.metrics, plan_timing(baseline.plan)),
        "optimized": _side(optimized.cost, optimized.metrics, plan_timing(optimized.plan)),
    }


def kpis_from_corridor_result(result: Mapping, weights: KpiWeights) -> dict:
    """Project a ``terasim.corridor_optimization.v1`` result to the service's ``kpis`` block.

    The same projection as ``POST /v1/city/optimize``: ``optimized`` is the plan that
    was exported to ``nema_plan.json``. When the optional refine stage exported its
    winner (``export_source == "refine"``), both sides come from the refine block
    (measured under the refine vehicle behavior); otherwise from ``baseline`` /
    ``best``. A side's ``timing`` is left out when the block has none.
    """
    if result.get("export_source") == "refine":
        refine = result["refine"]
        baseline = refine["baseline"]
        winner_plan_id = refine.get("winner_plan_id")
        optimized = next(
            (
                entry
                for entry in (refine["baseline"], *refine["evaluations"])
                if entry.get("plan_id") == winner_plan_id
            ),
            None,
        )
        if optimized is None:
            raise PluginRunError(
                f"refine.winner_plan_id={winner_plan_id!r} not found among the refine "
                "block's baseline/evaluations entries"
            )
    else:
        baseline = result["baseline"]
        optimized = result["best"]
    return {
        "cost_weights": weights.to_dict(),
        "baseline": _side(baseline["score"], baseline["moe"], baseline.get("timing")),
        "optimized": _side(optimized["score"], optimized["moe"], optimized.get("timing")),
    }


def export_nema(scenario: Scenario, plan) -> dict:
    """Write ``plan`` to ``<output_dir>/nema_plan.json`` with the core NEMA exporter.

    Static plans become stage rows against the common cycle (``handoff_ready`` when
    every green carries NEMA phase numbers); NEMA plans keep their patterns.

    Raises:
        PluginRunError: The plan cannot be written as one NEMA timing (e.g. its
            signals run different cycles); the message gives the core's reason.
    """
    payload, reason = _core.export_nema_or_reason(
        plan, corridor_id=scenario.corridor_id, out_dir=scenario.output_dir
    )
    if payload is None:
        raise PluginRunError(reason or f"plan {plan.plan_id!r} cannot be exported as NEMA timing")
    return payload


def build_result(
    plugin,
    scenario: Scenario,
    *,
    nema_plan: Mapping[str, Any],
    baseline: PlanScore,
    optimized: PlanScore,
    notes: Sequence[str] = (),
    evaluated: Sequence[PlanScore] = (),
    details: "Mapping[str, Any] | None" = None,
    artifacts: "Mapping[str, str] | None" = None,
) -> OperationResult:
    """Assemble the :class:`OperationResult` of a run built with these helpers.

    Lists the standard artifacts that exist (``nema_plan.json``, ``scenario/``,
    ``trials/``, ``inputs/``) plus ``artifacts``; records which plans were compared in
    ``details``; reports the movements the recommended plan never serves.
    """
    output_dir = scenario.output_dir
    listed = {
        name: relative
        for name, relative in (
            ("nema_plan", NEMA_FILENAME),
            ("scenario", SCENARIO_DIRNAME),
            ("trials", TRIALS_DIRNAME),
            ("inputs", scenario.request.inputs_dir.name),
        )
        if (output_dir / relative).exists()
    }
    listed.update(artifacts or {})
    info: dict[str, Any] = {
        "baseline_plan_id": baseline.plan_id,
        "recommended_plan_id": optimized.plan_id,
    }
    if evaluated:
        info["evaluations"] = [
            {
                "plan_id": score.plan_id,
                "valid": score.valid,
                "cost": score.cost,
                "trial_dir": _relative(score.trial_dir, output_dir),
                **({"error": score.error} if score.error else {}),
            }
            for score in evaluated
        ]
    info.update(details or {})
    return OperationResult(
        plugin=plugin.name,
        plugin_version=plugin.version,
        corridor_id=scenario.corridor_id,
        tls_ids=scenario.tls_ids,
        nema_plan=nema_plan,
        kpis=build_kpis(baseline, optimized, scenario.request.weights),
        output_dir=output_dir,
        artifacts=listed,
        notes=tuple(notes),
        unserved_movements=_core.unserved_movements(optimized.plan, scenario.net_file),
        details=info,
    )


def _relative(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)
