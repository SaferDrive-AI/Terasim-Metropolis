"""The built-in ``signal_timing`` plugin: the core corridor optimizer behind the Plugin Interface.

It maps an :class:`~terasim_plugin_sdk.model.OperationRequest` onto
``terasim_city.orchestration.corridor_optimization.CorridorOptimizationConfig``,
runs ``run_corridor_optimization`` and maps its result to an
:class:`~terasim_plugin_sdk.model.OperationResult`: the exported ``nema_plan.json``
and the KPI comparison projected exactly as the service's ``POST /v1/city/optimize``
projects it. It is the same optimizer the HTTP service and the MCP tools run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from terasim_plugin_sdk.constants import (
    API_VERSION,
    BUILTIN_SIGNAL_TIMING,
    NEMA_FILENAME,
    SDK_VERSION,
)
from terasim_plugin_sdk.errors import PluginInputError, PluginRunError
from terasim_plugin_sdk.model import OperationRequest, OperationResult, TrafficSnapshot
from terasim_plugin_sdk.protocol import ProgressCallback, ProgressRelay
from terasim_plugin_sdk.toolkit import (
    PLAN_STORE_DIRNAME,
    _latlon_points,
    check_vehicle_behavior,
    ingest_nema_plan,
    kpis_from_corridor_result,
    resolve_map,
)

__all__ = ["SIGNAL_TIMING_PARAMS", "SignalTimingPlugin"]

#: ``params`` accepted by ``signal_timing`` (all optional), with what each one sets.
SIGNAL_TIMING_PARAMS: dict[str, str] = {
    "optimizer": (
        "search after the seed plans: 'auto' (default; offsets for >= 2 signals, green "
        "splits for one), 'exhaustive' (seeds only), 'bayes_offsets', 'bayes_splits', "
        "'bayes_corridor' (splits, offsets and optionally the cycle)"
    ),
    "max_trials": "search budget in simulated plans (default 20)",
    "patience": "stop after this many plans without improvement (default 8; <= 0 disables)",
    "stage_mode": "Webster seed stages: 'two_stage' (default) or 'per_approach'",
    "search_cycle": "let the search vary the cycle length (default false)",
    "cycle_min_s": "lower cycle bound, seconds (default 45)",
    "cycle_max_s": "upper cycle bound, seconds (default 150)",
    "flow_source": "demand: 'corridor_graph' (default, from the counts) or 'scenario_routes'",
    "counts_source": "turning counts for the seed: 'tmc' (default) or 'scenario_routes'",
}

_INT_PARAMS = ("max_trials", "patience")
_NUMBER_PARAMS = ("cycle_min_s", "cycle_max_s")
_STR_PARAMS = ("optimizer", "stage_mode", "flow_source", "counts_source")
_BOOL_PARAMS = ("search_cycle",)

_RESULT_FILE = "corridor_optimization_result.json"
_LEDGER_FILE = "trials.jsonl"


def _options(params: Mapping[str, Any]) -> dict:
    """Validate the ``signal_timing`` params (keys and value types)."""
    unknown = sorted(set(params) - set(SIGNAL_TIMING_PARAMS))
    if unknown:
        raise PluginInputError(
            f"signal_timing does not accept params {unknown}; "
            f"accepted: {sorted(SIGNAL_TIMING_PARAMS)}"
        )
    for key, value in params.items():
        if key in _INT_PARAMS and (isinstance(value, bool) or not isinstance(value, int)):
            raise PluginInputError(f"param {key!r} must be an integer, got {value!r}")
        if key in _NUMBER_PARAMS and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise PluginInputError(f"param {key!r} must be a number, got {value!r}")
        if key in _STR_PARAMS and not isinstance(value, str):
            raise PluginInputError(f"param {key!r} must be a string, got {value!r}")
        if key in _BOOL_PARAMS and not isinstance(value, bool):
            raise PluginInputError(f"param {key!r} must be true or false, got {value!r}")
    return dict(params)


class SignalTimingPlugin:
    """Corridor signal-timing optimization with the core optimizer.

    The current plan (or, without one, a Webster plan from the TMC counts) is the
    baseline; a Bayesian search over offsets / green splits (/ cycle) follows, and
    the best simulated plan is exported. ``params``: see :data:`SIGNAL_TIMING_PARAMS`.
    """

    name = BUILTIN_SIGNAL_TIMING
    version = SDK_VERSION
    api_version = API_VERSION
    description = (
        "Corridor signal-timing optimization (offsets, green splits, cycle) with the core optimizer"
    )
    params = SIGNAL_TIMING_PARAMS

    def build_config(self, request: OperationRequest):
        """The core ``CorridorOptimizationConfig`` this plugin runs for ``request``.

        Validates the request and materializes its inputs under ``<output_dir>/inputs``
        (an in-memory TMC payload, a NEMA plan document's plan store); runs nothing.
        """
        return self._prepare(request)[0]

    def _prepare(self, request: OperationRequest):
        """``(CorridorOptimizationConfig, ResolvedMap)`` for ``request``."""
        from terasim_city.api import FLOW_SOURCES
        from terasim_city.optimize.base import Budget
        from terasim_city.orchestration.corridor_optimization import CorridorOptimizationConfig

        options = _options(request.params)
        request.check_inputs()
        check_vehicle_behavior(request.vehicle_behavior)
        flow_source = options.pop("flow_source", "corridor_graph")
        if flow_source not in FLOW_SOURCES:
            raise PluginInputError(
                f"unknown flow_source {flow_source!r}; known: {sorted(FLOW_SOURCES)}"
            )
        request.output_dir.mkdir(parents=True, exist_ok=True)
        tmc_json = request.traffic.write_to(request.inputs_dir)
        tmc = TrafficSnapshot.from_file(tmc_json).load()
        selection = request.corridor
        resolved = resolve_map(
            request.map, tmc, extra_points=_latlon_points(selection.from_ref, selection.to_ref)
        )
        try:
            return CorridorOptimizationConfig(
                tmc_json=Path(tmc_json),
                base_sumocfg=resolved.base_sumocfg,
                output_dir=request.output_dir,
                from_ref=selection.from_ref,
                to_ref=selection.to_ref,
                tls_ids=selection.tls_ids,
                optimizer=options.pop("optimizer", "auto"),
                budget=Budget(
                    max_trials=options.pop("max_trials", 20), patience=options.pop("patience", 8)
                ),
                kpi=request.weights.to_kpi_config(),
                sim_end=request.sim_end,
                seed=request.seed,
                n_workers=request.n_workers,
                vehicle_behavior=request.vehicle_behavior,
                flow_source=flow_source,
                **self._current_plan_fields(request),
                **options,
            ), resolved
        except ValueError as exc:
            raise PluginInputError(f"invalid signal_timing request: {exc}") from exc

    @staticmethod
    def _current_plan_fields(request: OperationRequest) -> dict:
        current = request.current_plan
        if current is None:
            return {}
        if current.kind == "timing_sheet":
            return {"baseline_sheet": current.path}
        if current.kind == "scenario_programs":
            return {"seed_from_scenario_programs": True}
        root = current.path
        if current.kind == "nema_plan":
            root = ingest_nema_plan(current.path, request.inputs_dir / PLAN_STORE_DIRNAME)
        return {"plan_data_root": root, "plan_time": current.plan_time}

    def run(
        self, request: OperationRequest, progress: "ProgressCallback | None" = None
    ) -> OperationResult:
        from terasim_city.orchestration.corridor_optimization import run_corridor_optimization

        config, resolved = self._prepare(request)
        relay = ProgressRelay(progress)
        try:
            result = run_corridor_optimization(
                config, progress_callback=relay if progress is not None else None
            )
        except Exception as exc:
            if relay.is_cancellation(exc):
                raise
            if isinstance(exc, ValueError):  # configuration, corridor, map, sheet coverage
                raise PluginInputError(str(exc)) from exc
            if isinstance(exc, RuntimeError):  # no valid seed evaluation
                raise PluginRunError(str(exc)) from exc
            raise

        export = result.get("nema_export") or {}
        nema_file = request.output_dir / NEMA_FILENAME
        if not export.get("exported") or not nema_file.is_file():
            raise PluginRunError(export.get("reason") or "the optimizer exported no NEMA plan")
        nema_plan = json.loads(nema_file.read_text(encoding="utf-8"))
        return OperationResult(
            plugin=self.name,
            plugin_version=self.version,
            corridor_id=str(nema_plan.get("corridor_id", "")),
            tls_ids=tuple(item["tls_id"] for item in result["resolved_corridor"]),
            nema_plan=nema_plan,
            kpis=kpis_from_corridor_result(result, request.weights),
            output_dir=request.output_dir,
            artifacts=self._artifacts(request.output_dir),
            notes=self._notes(result),
            unserved_movements=result.get("unserved_movements") or {},
            details=self._details(result, config, resolved),
        )

    @staticmethod
    def _artifacts(output_dir: Path) -> dict:
        candidates = {
            "nema_plan": NEMA_FILENAME,
            "corridor_optimization_result": _RESULT_FILE,
            "trials_ledger": _LEDGER_FILE,
            "scenario": "scenario",
            "trials": "trials",
            "derived_tmc": "derived_tmc.json",
            "inputs": "inputs",
        }
        return {name: rel for name, rel in candidates.items() if (output_dir / rel).exists()}

    @staticmethod
    def _notes(result: Mapping) -> tuple:
        notes = []
        seed_note = (result.get("seed") or {}).get("note")
        if seed_note:
            notes.append(str(seed_note))
        search = result.get("search") or {}
        if search.get("ran") is False and search.get("reason"):
            notes.append(f"no search after the seed plans: {search['reason']}")
        if result.get("export_source") == "coarse":
            notes.append(
                "the refine stage produced no usable winner; the coarse winner was exported"
            )
        return tuple(notes)

    @staticmethod
    def _details(result: Mapping, config, resolved) -> dict:
        seed = result.get("seed") or {}
        if result.get("export_source") == "refine":
            recommended = (result.get("refine") or {}).get("winner_plan_id")
        else:
            recommended = (result.get("best") or {}).get("plan_id")
        return {
            "optimizer": config.optimizer,
            "seed_source": seed.get("source"),
            "stored_plans_used": bool(seed.get("stored_plans_used")),
            "baseline_plan_id": (result.get("baseline") or {}).get("plan_id"),
            "recommended_plan_id": recommended,
            "n_trials": result.get("n_trials"),
            "search": result.get("search"),
            "resolved_corridor": result.get("resolved_corridor"),
            "map_id": resolved.map_id,
            "result_schema": result.get("schema"),
        }
