"""End-to-end corridor signal-plan optimization.

``run_corridor_optimization`` chains the corridor pipeline: resolve the
corridor, generate ONE shared scenario via the TMC pipeline, build the seed
plan (uploaded plans via the plan store; else the baseline timing sheet if
given; else a Webster initialiser from the TMC counts — with
``seed_from_scenario_programs`` the scenario's own signal programs come first
and are the baseline; see ``_build_seed_plans``), evaluate it through
:class:`~terasim_city.optimize.batch.BatchEvaluator`,
optionally run a Bayesian refinement, and write the trials ledger
(``trials.jsonl``), the NEMA export of the winner, and
``corridor_optimization_result.json`` (schema
``terasim.corridor_optimization.v1``).

``optimizer`` selects the refinement phase run after the seeds:

* ``"auto"``: corridors of >= 2 TLS search offsets
  (:class:`~terasim_city.optimize.bayes_offsets.BayesianOffsetOptimizer`);
  a single TLS searches green splits
  (:class:`~terasim_city.optimize.bayes_splits.BayesianSplitOptimizer`),
  falling back to seeds-only with a logged warning when the seed plan has
  fewer than 2 green stages.
* ``"exhaustive"``: seeds only, any corridor size.
* ``"bayes_offsets"``: offset search; requires >= 2 TLS (ValueError).
* ``"bayes_splits"``: green-split search; requires exactly 1 TLS (ValueError).
* ``"bayes_corridor"``: joint search over every TLS's green split and offset, plus
  a common cycle when ``search_cycle`` is on
  (:class:`~terasim_city.optimize.bayes_corridor.BayesianCorridorOptimizer`);
  requires >= 2 TLS (ValueError).

With ``search_cycle`` the single-TLS split search also varies the cycle, within
``[cycle_min_s, cycle_max_s]``; the offset search never does.
A Bayesian phase that cannot start from the best seed (e.g. per-TLS cycles without a
common cycle, with the cycle not searched) is skipped with a logged warning and the run
reports its seeds only, under ``auto`` and under every explicit optimizer alike. The
result's ``search`` says which happened: ``{"ran": true, "reason": null}``, or
``{"ran": false, "reason": ...}`` carrying that warning's sentence (``exhaustive``, which
evaluates the seeds only by request, gives that as its reason). Its ``warm_starts`` lists
the seed plans the Bayesian phase started from (``used``: the best seed, plus under
``bayes_corridor`` every other valid seed it can represent) and those it could not
(``skipped``: ``{"plan_id", "reason"}``); both lists are empty when the phase did not run.
``result["baseline"]["timing"]`` and ``result["best"]["timing"]`` give the
per-TLS cycle, offsets and (static plans only) green durations of those two plans.

The result's ``nema_export`` says whether ``nema_plan.json`` was written:
``{"exported": true}``, or ``{"exported": false, "reason": ...}`` when the plan picked
for export has no common cycle (a customer's own programs, see
``seed_from_scenario_programs``, often run a different cycle at every signal). The run
then still completes and writes its result and ledger; ``nema.handoff_ready`` is false
and no ``nema_plan.json`` is written.

The result's ``unserved_movements`` lists, per TLS, the vehicle movements the exported
plan never lets move, red or yellow in every phase (``{}`` when it serves every
movement). Generated plans serve every approach that has controlled links; a
customer's own programs or a timing sheet may not, and such a plan must not be
implemented as is.

``progress_callback`` exceptions propagate: the service worker cancels a run
softly by raising ``LeaseLost`` through it between evaluation batches.

Optional refine stage (``refine_top_k`` / ``refine_vehicle_behavior`` on
:class:`CorridorOptimizationConfig`, OFF by default — see
``_run_refine_stage``): after the coarse BO search above finishes, re-run the
baseline plus the K best distinct coarse-score plans under a DIFFERENT
vehicle behavior (e.g. "cnde"). ``refine_n_workers`` (default 1) picks
sequential in-process evaluation or a spawn pool of that many concurrent
refine processes; every trial seeds its RNGs and iterates in a
deterministic order (see ``_run_refine_stage``), so a plan evaluates to
byte-identical results either way and the knob is purely a wall-clock vs.
GPU-memory trade-off. The refine winner (argmin over baseline + top-K, minus
anything failing the optional ``refine_max_collisions`` gate) becomes the
plan exported to ``nema_plan.json``; the coarse winner in the result JSON's
``best`` key is untouched. Disabled (the default), every output of this
module is byte-identical to a build without this feature.

When the refine stage ran, the result JSON also carries top-level
``export_source`` — ``"refine"`` when a refine winner was actually exported,
``"coarse"`` when refine ran but produced no usable winner (baseline refine
failure, or every candidate gated/invalid — the export then falls back to
the coarse winner). The key is absent entirely when refine never ran.
**Any consumer that reports "optimized" KPIs for the EXPORTED plan (e.g. the
terasim-service wiring — see ``optimize_api.py``'s ``_direct_kpis``) MUST
branch on ``export_source``**: read
``result["refine"]["baseline"]``/``["evaluations"]`` (which carry a full
``moe`` dict per entry, not just the ratio score) when it is ``"refine"``,
and ``result["best"]`` otherwise — reading ``result["best"]`` unconditionally
silently reports the COARSE plan's KPIs for a NEMA export that is actually
the refine winner's plan.

With ``vehicle_behavior="cnde"`` every candidate -- the seeds and every
Bayesian trial -- runs under the CNDE learned driver behavior, at most
``cnde_max_workers`` of them at once on the GPU, and the result JSON carries
top-level ``traffic_model: "cnde"`` (absent under the default "sumo_passive").
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

from terasim_city.corridor.resolve import ResolvedCorridor, resolve_corridor
from terasim_city.evaluation.moe import KpiConfig, score_moe
from terasim_city.flow.descriptors import TMCData
from terasim_city.nema.export import JSON_FILENAME as NEMA_JSON_FILENAME
from terasim_city.nema.export import export_nema, export_nema_plan
from terasim_city.optimize.base import Budget, PlanEvaluation
from terasim_city.optimize.batch import BatchEvaluator
from terasim_city.optimize.bayes_corridor import BayesianCorridorOptimizer
from terasim_city.optimize.bayes_nema_offsets import BayesianNemaOffsetOptimizer
from terasim_city.optimize.bayes_nema_splits import BayesianNemaSplitOptimizer
from terasim_city.optimize.bayes_offsets import BayesianOffsetOptimizer
from terasim_city.optimize.bayes_splits import BayesianSplitOptimizer
from terasim_city.evaluation.diagnostics import (
    disable_teleport,
    load_stalls,
    snap_stalls_to_vehicle_lanes,
)
from terasim_city.plans.assemble import plan_selections
from terasim_city.plans.baseline_sheet import TimingSheetCoverageError, load_timing_sheet
from terasim_city.plans.init_splits import STAGE_MODES, build_webster_plan
from terasim_city.plans.model import MOVEMENTS, CorridorPlan, PlanProvenance
from terasim_city.plans.nema_plan import NemaCorridorPlan, NemaSignalEntry
from terasim_city.plans.nema_webster import (
    FREE_PATTERN_ID,
    recommended_cycle_s,
    synthesize_free_pattern,
)
from terasim_city.plans.store import PlanStore
from terasim_city.plans.sumo_programs import load_scenario_programs

__all__ = [
    "CorridorOptimizationConfig",
    "ProgressCallback",
    "run_corridor_optimization",
]

logger = logging.getLogger(__name__)

RESULT_SCHEMA = "terasim.corridor_optimization.v1"
RESULT_FILENAME = "corridor_optimization_result.json"
TRIALS_FILENAME = "trials.jsonl"
#: The TMC derived from the base scenario's route files (``counts_source="scenario_routes"``).
DERIVED_TMC_FILENAME = "derived_tmc.json"

OPTIMIZER_CHOICES = ("auto", "exhaustive", "bayes_offsets", "bayes_splits", "bayes_corridor")
COUNTS_SOURCES = ("tmc", "scenario_routes")

#: Called as ``(done, total_budget, last_plan_id)``: once with ``(0, total,
#: None)`` before any trial runs, then after every evaluated plan. Exceptions
#: raised by the callback PROPAGATE (soft-cancellation contract).
ProgressCallback = Callable[[int, int, "str | None"], None]


@dataclass(frozen=True)
class CorridorOptimizationConfig:
    """File-based configuration for one corridor optimization run."""

    tmc_json: Path
    #: The corridor's map (a base sumocfg). ``None`` = auto-select from the
    #: map store by the TMC intersections' coordinates (prebuilt/HD preferred
    #: over OSM, exactly the flow pipeline's rule).
    base_sumocfg: Path | None
    output_dir: Path
    from_ref: str | None = None
    to_ref: str | None = None
    tls_ids: tuple[str, ...] = ()
    baseline_sheet: Path | None = None
    stall_data: Path | None = None
    optimizer: str = "auto"
    budget: Budget = Budget()
    kpi: KpiConfig = KpiConfig()
    sim_end: float = 1800.0
    seed: int = 42
    n_workers: int = 4
    #: Vehicle-behavior registry name (see terasim_city.factories). Lane
    #: assist only applies with "sumo_passive": behavior models own their
    #: own lane behavior, so any other value disables it for the run.
    vehicle_behavior: str = "sumo_passive"
    #: Most concurrent trial processes a "cnde" search may run (``n_workers``
    #: above it is rejected, see __post_init__). Each holds the 972 MB
    #: checkpoint plus a CUDA context, ~1.5 GB growing to ~6 GB over a long
    #: run. The default 1 keeps every "cnde" search single-process; the service
    #: raises it to TERASIM_CITY_CNDE_MAX_WORKERS for a CNDE request.
    cnde_max_workers: int = 1
    signal_groups_overrides: Path | None = None
    #: Flow source for the shared scenario; must be registered in
    #: terasim_city.api.FLOW_SOURCES: "corridor_graph" (the default) synthesises
    #: the demand from the TMC counts, "scenario_routes" reuses the base
    #: scenario's own route files as they are.
    flow_source: str = "corridor_graph"
    #: Root of the uploaded-plan store ($TERASIM_CITY_DATA_ROOT). When given
    #: and the store covers EVERY corridor intersection at ``plan_time``, the
    #: assembled real NEMA plans become the seed (ladder rung -1) and the
    #: Bayesian phase searches NEMA offsets.
    plan_data_root: Path | None = None
    #: ISO timestamp selecting each intersection's time-of-day pattern.
    #: Defaults to the TMC file's ``timestamp_start`` (flow and plan then
    #: describe the same time window). Signal schedules are LOCAL wall-clock
    #: tables: a timezone-aware value (e.g. "...T16:16:17Z") is converted
    #: into the corridor's local time, derived from the corridor's own
    #: coordinates (see :mod:`terasim_city.geo_timezone`); naive values are
    #: taken as local wall-clock time as-is. There is deliberately no
    #: timezone knob anywhere.
    plan_time: str | None = None
    #: 0 (default) disables the refine stage entirely: BO-stage behavior and
    #: outputs are then byte-identical to a build without this feature.
    #: > 0 re-evaluates the baseline plus this many best DISTINCT coarse-score
    #: plans (deduped; bounded by how many valid coarse evaluations exist)
    #: under ``refine_vehicle_behavior`` — see ``_run_refine_stage``.
    refine_top_k: int = 0
    #: Vehicle-behavior registry name for the refine stage (e.g. "cnde").
    #: ``None`` (default) leaves the refine stage disabled even if
    #: ``refine_top_k`` is set. An unregistered name fails loud HERE, at
    #: config-build time, never mid-run (see terasim_city.factories).
    refine_vehicle_behavior: str | None = None
    #: Per-behavior refine validity gate on the collision count already
    #: written to each trial's kpi.json (quality_gate.counts.collisions).
    #: ``None`` (default) = no gate. Deliberately independent of the
    #: BatchEvaluator quality_thresholds used everywhere else in this module
    #: (which stay None/no-gate: different
    #: vehicle behaviors are not equally collision-prone, so a single
    #: uniform gate would distort the refine ranking); a refine evaluation
    #: exceeding this is excluded from winner selection, not from the report.
    refine_max_collisions: int | None = None
    #: Refine-stage process count. 1 (default) evaluates the baseline + top-K
    #: one after another in the calling process (``BatchEvaluator(inline=
    #: True)``, sharing one loaded behavior runtime); > 1 runs them through a
    #: spawn pool of this many concurrent processes (``inline=False``), each
    #: loading its own copy of the refine behavior's runtime -- for "cnde"
    #: the 972 MB checkpoint plus a CUDA context, ~1.5 GB GPU per process --
    #: so size it to the GPU (e.g. ``refine_top_k + 1`` on a 96 GB card).
    #: Never capped to the candidate count: surplus workers simply idle.
    #: Results are identical in both modes -- see ``_run_refine_stage``.
    refine_n_workers: int = 1
    #: Webster seed decomposition: "two_stage" (E-W / N-S) or "per_approach"
    #: (one protected stage per approach, long-cycle style; an approach with
    #: controlled lanes but no demand gets a minimum-green stage).
    stage_mode: str = "two_stage"
    #: Let the Bayesian phase vary the cycle length within [cycle_min_s, cycle_max_s].
    #: Honoured by the single-TLS split search and the joint ``bayes_corridor``
    #: search; the offset-only searches keep the seed's cycle. Must be a real bool.
    search_cycle: bool = False
    #: Cycle bounds (s): they clamp the Webster seed's cycle and, with
    #: ``search_cycle``, the range the Bayesian phase may vary it within.
    cycle_min_s: float = 45.0
    cycle_max_s: float = 150.0
    #: Where the seed's turning counts come from: "tmc" (the tmc_json) or
    #: "scenario_routes" (derived from the base scenario's own route files, from
    #: the demand inside the simulated window [0, sim_end); tmc_json is then only
    #: a placeholder and derived_tmc.json replaces it).
    counts_source: str = "tmc"
    #: Use the scenario's own <tlLogic> programs (net + additional files) as the
    #: first seed, so the customer's current timing is the baseline. Has no effect
    #: when uploaded NEMA plans seed the run (``plan_data_root``). Must be a real bool.
    seed_from_scenario_programs: bool = False

    def __post_init__(self) -> None:
        for name in ("tmc_json", "output_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)))
        if self.base_sumocfg is not None:
            object.__setattr__(self, "base_sumocfg", Path(self.base_sumocfg))
        for name in ("baseline_sheet", "stall_data", "signal_groups_overrides",
                     "plan_data_root"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value))
        object.__setattr__(self, "tls_ids", tuple(str(t) for t in self.tls_ids))
        if self.optimizer not in OPTIMIZER_CHOICES:
            raise ValueError(
                f"optimizer must be one of {OPTIMIZER_CHOICES}, got {self.optimizer!r}"
            )
        if int(self.n_workers) < 1:
            raise ValueError(f"n_workers must be >= 1, got {self.n_workers!r}")
        object.__setattr__(self, "n_workers", int(self.n_workers))
        object.__setattr__(self, "sim_end", float(self.sim_end))
        object.__setattr__(self, "seed", int(self.seed))
        object.__setattr__(self, "vehicle_behavior", str(self.vehicle_behavior))
        object.__setattr__(self, "cnde_max_workers", int(self.cnde_max_workers))
        if self.cnde_max_workers < 1:
            raise ValueError(
                f"cnde_max_workers must be >= 1, got {self.cnde_max_workers!r}"
            )
        if self.vehicle_behavior == "cnde" and self.n_workers > self.cnde_max_workers:
            # Only the coarse BatchEvaluator construction in
            # run_corridor_optimization is affected: it passes inline=None,
            # which defers to the TERASIM_CITY_INLINE_EVAL env var (see
            # terasim_city.optimize.batch._inline_mode_enabled) exactly like
            # this check does -- so the guard rejects precisely the
            # combination that would actually spawn a pool of more concurrent
            # CNDE workers than ``cnde_max_workers``, each loading the 972MB
            # checkpoint onto the GPU per trial. It is deliberately narrower
            # than "n_workers <= cnde_max_workers for cnde": when
            # the env var forces inline mode, BatchEvaluator.evaluate never
            # builds the pool at all, so n_workers is unused rather than
            # unsafe in that case. The refine stage is untouched by this
            # check (it validates ``vehicle_behavior``, not
            # ``refine_vehicle_behavior``): _run_refine_stage constructs its
            # own BatchEvaluator sized by ``refine_n_workers`` (inline when
            # 1), never by cfg.n_workers -- concurrent CNDE refine workers
            # are an explicit, separately validated opt-in, not a leak.
            from terasim_city.optimize.batch import _inline_mode_enabled

            if not _inline_mode_enabled():
                raise ValueError(
                    f"vehicle_behavior='cnde' with n_workers={self.n_workers} "
                    "would run more concurrent CNDE workers than "
                    f"cnde_max_workers={self.cnde_max_workers}, each loading the "
                    "972MB checkpoint onto the GPU per trial; set n_workers to "
                    f"at most {self.cnde_max_workers}, or set "
                    "TERASIM_CITY_INLINE_EVAL=1 to force single-process "
                    "in-process evaluation"
                )
        object.__setattr__(self, "flow_source", str(self.flow_source))
        object.__setattr__(self, "counts_source", str(self.counts_source))
        if self.counts_source not in COUNTS_SOURCES:
            raise ValueError(
                f"counts_source must be one of {COUNTS_SOURCES}, got {self.counts_source!r}"
            )
        if self.counts_source == "scenario_routes":
            missing = [
                name
                for name, given in (
                    ("base_sumocfg", self.base_sumocfg is not None),
                    ("tls_ids", bool(self.tls_ids)),
                )
                if not given
            ]
            if missing:
                raise ValueError(
                    "counts_source='scenario_routes' derives the turning counts from the "
                    f"base scenario's route files and needs {' and '.join(missing)}"
                )
        object.__setattr__(self, "refine_top_k", int(self.refine_top_k))
        if self.refine_top_k < 0:
            raise ValueError(
                f"refine_top_k must be >= 0, got {self.refine_top_k!r}"
            )
        if self.refine_vehicle_behavior is not None:
            object.__setattr__(
                self, "refine_vehicle_behavior", str(self.refine_vehicle_behavior)
            )
            # Deferred import: keeps this module's own top-level imports free
            # of the factories registry when the refine stage is unused (the
            # module docstring in terasim_city.factories.cnde_behavior
            # promises importing it is cheap either way, but there is no
            # reason to pay even that unconditionally).
            from terasim_city.factories import VEHICLE_BEHAVIORS

            if self.refine_vehicle_behavior not in VEHICLE_BEHAVIORS:
                raise ValueError(
                    f"unknown refine_vehicle_behavior "
                    f"{self.refine_vehicle_behavior!r}; registered behaviors: "
                    f"{sorted(VEHICLE_BEHAVIORS)}"
                )
        if self.refine_max_collisions is not None:
            object.__setattr__(
                self, "refine_max_collisions", int(self.refine_max_collisions)
            )
            if self.refine_max_collisions < 0:
                raise ValueError(
                    "refine_max_collisions must be >= 0 or None, got "
                    f"{self.refine_max_collisions!r}"
                )
        object.__setattr__(self, "refine_n_workers", int(self.refine_n_workers))
        if self.refine_n_workers < 1:
            raise ValueError(
                f"refine_n_workers must be >= 1, got {self.refine_n_workers!r}"
            )
        if self.stage_mode not in STAGE_MODES:
            raise ValueError(
                f"stage_mode must be one of {STAGE_MODES}, got {self.stage_mode!r}"
            )
        if not isinstance(self.search_cycle, bool):
            raise ValueError(f"search_cycle must be a bool, got {self.search_cycle!r}")
        if not isinstance(self.seed_from_scenario_programs, bool):
            raise ValueError(
                "seed_from_scenario_programs must be a bool, "
                f"got {self.seed_from_scenario_programs!r}"
            )
        object.__setattr__(self, "cycle_min_s", float(self.cycle_min_s))
        object.__setattr__(self, "cycle_max_s", float(self.cycle_max_s))
        if not 0.0 < self.cycle_min_s < self.cycle_max_s:
            raise ValueError(
                f"need 0 < cycle_min_s < cycle_max_s, got "
                f"{self.cycle_min_s!r}/{self.cycle_max_s!r}"
            )


#: Tells the trials' Simulators that this run already warned that sumolib cannot
#: read the net's TLS programs (the same name as ``terasim.simulator``'s constant).
QUIET_PROGRAM_FALLBACK_ENV = "TERASIM_QUIET_PROGRAM_FALLBACK"


def run_corridor_optimization(
    cfg: CorridorOptimizationConfig,
    *,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """Run the full corridor optimization and return the result payload.

    Returns the dict also written to ``output_dir/corridor_optimization_result.json``.

    Usage rules for standalone scripts:

    * Call this behind ``if __name__ == "__main__":``. Trials run in a
      spawn-based process pool, and spawn re-imports the calling script —
      an unguarded top-level call makes every worker relaunch the whole
      optimization (the run hangs with no error and no progress).
    * On a map with more than one traffic light, identify the corridor:
      either explicit ``tls_ids`` or a ``from_ref``/``to_ref`` pair
      (TMC intersection ids or ``"lat,lon"`` strings). With neither, a
      multi-TLS map raises ValueError instead of guessing.

    Raises:
        ValueError: Bad configuration (unknown optimizer, ``bayes_offsets`` or
            ``bayes_corridor`` on a single-TLS corridor, ``bayes_splits`` on a
            multi-TLS corridor, ...). A resolved signal the baseline sheet has no
            entry for is a ``TimingSheetCoverageError`` (a ``ValueError``).
        RuntimeError: No valid seed evaluation exists (per-seed errors listed).
    """
    # The run may set QUIET_PROGRAM_FALLBACK_ENV for its trials (see
    # _warn_once_if_sumolib_cannot_read_programs); the caller's value comes back after.
    saved = os.environ.get(QUIET_PROGRAM_FALLBACK_ENV)
    try:
        return _run_corridor_optimization(cfg, progress_callback=progress_callback)
    finally:
        if saved is None:
            os.environ.pop(QUIET_PROGRAM_FALLBACK_ENV, None)
        else:
            os.environ[QUIET_PROGRAM_FALLBACK_ENV] = saved


def _warn_once_if_sumolib_cannot_read_programs(net_file: Path) -> bool:
    """Warn, once for the run, when sumolib cannot read the net's TLS programs.

    Every trial builds a Simulator, in a fresh process, which then falls back to
    loading the net without its programs (sumolib < 1.27 rejects fractional phase
    durations that SUMO itself runs) and would warn once per trial. Returns True
    when the fallback applies, so the caller can quiet the trials.
    """
    import sumolib

    try:
        sumolib.net.readNet(str(net_file), withInternal=True, withPrograms=True)
    except ValueError as exc:
        logger.warning(
            "sumolib could not parse the TLS programs of %s (%s); every trial loads "
            "the net without programs (SUMO itself runs them)",
            net_file,
            exc,
        )
        return True
    return False


def _run_corridor_optimization(
    cfg: CorridorOptimizationConfig,
    *,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """The body of :func:`run_corridor_optimization`."""
    # The TMC every later step works from: the given file, or the counts
    # derived from the base scenario's own route files.
    tmc_json = (
        _write_derived_tmc(cfg) if cfg.counts_source == "scenario_routes" else cfg.tmc_json
    )
    tmc = TMCData.from_json(tmc_json)
    corridor_id = tmc.corridor_id

    # Map resolution: an explicit base sumocfg wins; otherwise auto-select the
    # stored map covering every TMC intersection (prebuilt/HD before OSM —
    # the same rule the flow pipeline applies).
    map_id_used = None
    # MapStore package directory backing base_sumocfg, when knowable. Feeds
    # the "cnde" vehicle_behavior's artifact cache
    # (terasim_city.maps.cnde_artifacts.ensure_cnde_artifacts) via
    # BatchEvaluator -> the trial task dict's "map_dir"; ignored for every
    # other behavior. See evaluator._resolve_cnde_map_dir for the (weaker)
    # fallback this passthrough exists to avoid needing.
    map_dir_used: Path | None = None
    base_sumocfg = cfg.base_sumocfg
    if base_sumocfg is None:
        from terasim_city.flow.generate import select_map_for
        from terasim_city.maps.store import MapStore

        store = MapStore(cfg.plan_data_root) if cfg.plan_data_root else MapStore()
        # Raw "lat,lon" endpoint refs are optimization targets the map must
        # also cover — a target outside the TMC camera footprint must not be
        # handed a map that does not contain it. Named TMC refs need nothing
        # extra: their coordinates are the TMC's own.
        extra_points = []
        for ref in (cfg.from_ref, cfg.to_ref):
            parts = str(ref).split(",") if ref else ()
            if len(parts) == 2:
                try:
                    extra_points.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    pass
        map_id_used, package = select_map_for(
            store, tmc, extra_points=tuple(extra_points)
        )
        base_sumocfg = package.base_sumocfg
        map_dir_used = package.root
        logger.info("auto-selected map %s -> %s", map_id_used, base_sumocfg)
    else:
        # An explicit base_sumocfg may itself point straight at a stored
        # package's own base sumocfg (e.g. MapStore(...).get(map_id).base_sumocfg
        # passed through by the caller); if its directory carries the
        # package manifest, it IS the map_dir — no auto-selection needed to
        # know that.
        from terasim_city.maps.base import MANIFEST_NAME as _MAP_MANIFEST_NAME

        candidate_dir = base_sumocfg.parent
        if (candidate_dir / _MAP_MANIFEST_NAME).exists():
            map_dir_used = candidate_dir

    corridor = resolve_corridor(
        base_sumocfg,
        tmc_json,
        from_ref=cfg.from_ref,
        to_ref=cfg.to_ref,
        tls_ids=cfg.tls_ids or None,
    )

    # Ladder rung -1: uploaded real plans. When the plan store covers every
    # corridor intersection at the query time, the assembled NEMA baseline is
    # the seed and the Bayesian phase searches NEMA offsets.
    nema_seed, seed_fallback_reason = _load_stored_nema_seed(
        cfg, corridor, corridor_id, tmc
    )
    # Recorded into the result so the caller can SEE whether their uploaded
    # plans were actually used or the run silently fell back to a generated
    # seed (partial coverage / no coverage).
    stored_plans_considered = cfg.plan_data_root is not None
    if nema_seed is not None:
        if cfg.optimizer in ("bayes_offsets", "bayes_splits", "bayes_corridor"):
            raise ValueError(
                f"optimizer {cfg.optimizer!r} drives the static-plan search and "
                "cannot run on a stored NEMA seed; use 'auto' (NEMA offsets for "
                "corridors, NEMA splits for a single intersection) or "
                "'exhaustive' (baseline only)"
            )
        # auto: corridors coordinate via offsets; a single intersection has no
        # relative offset, so its knob is the green split.
        if cfg.optimizer != "auto":
            bo_mode = None
        elif len(nema_seed.entries) >= 2:
            bo_mode = "nema_offsets"
        else:
            bo_mode = "nema_splits"
    else:
        bo_mode = _bo_mode(cfg.optimizer, len(corridor.intersections))
    # Why the Bayesian phase does not run, when it does not: ``result["search"]`` reports
    # it. ``exhaustive`` is seeds-only by request; every other reason is a degrade, which
    # goes through ``_skip_search`` so the log and the result carry the same sentence.
    search_skipped: "str | None" = (
        f"optimizer {cfg.optimizer!r} evaluates the seed plans only" if bo_mode is None else None
    )
    # Which seed plans warm-started the Bayesian phase (``result["search"]["warm_starts"]``):
    # empty when it never ran.
    warm_starts: dict = {"used": [], "skipped": []}

    stalls = load_stalls(cfg.stall_data)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    # One shared scenario for every trial: the plan under test is layered on
    # top per-trial by materialize_plan, the demand/network never change.
    from terasim_city.api import run_tmc_pipeline

    scenario = run_tmc_pipeline(
        tmc_json,
        base_sumocfg,
        cfg.output_dir / "scenario",
        sim_end=cfg.sim_end,
        seed=cfg.seed,
        flow_source=cfg.flow_source,
    )
    # Every optimization run measures congestion honestly: gridlock stays in
    # the network (and in the score) instead of being teleported away, and
    # collisions warn instead of removing demand. See disable_teleport.
    disable_teleport(
        Path(scenario.sumocfg_file), sim_end=cfg.sim_end, seed=cfg.seed
    )

    net_file = Path(scenario.net_file)
    if _warn_once_if_sumolib_cannot_read_programs(net_file):
        # The trials (fresh processes) inherit the environment: they log the
        # fallback at DEBUG. run_corridor_optimization restores it afterwards.
        os.environ[QUIET_PROGRAM_FALLBACK_ENV] = "1"
    if stalls:
        # Resolve incident geometry ONCE, before any trial: junction-internal
        # snaps are corrected here; an unplaceable coordinate fails the whole
        # run now (better than silently evaluating a no-incident scenario).
        stalls = snap_stalls_to_vehicle_lanes(net_file, stalls)
    if nema_seed is not None:
        seeds = [nema_seed]
    else:
        seeds = _build_seed_plans(
            cfg, corridor, net_file, base_sumocfg=base_sumocfg, tmc_json=tmc_json
        )

    if (
        bo_mode == "splits"
        and cfg.optimizer == "auto"
        and not any(_n_green_stages(plan) >= 2 for plan in seeds)
    ):
        # Production maps may carry odd programs (e.g. a single green stage):
        # under auto there is nothing to split, so degrade gracefully.
        search_skipped = _skip_search(
            "auto optimizer: no seed plan for TLS %s has >= 2 green stages; "
            "skipping the Bayesian split search (seeds only)",
            corridor.tls_ids[0],
        )
        bo_mode = None

    total_budget = len(seeds) + (cfg.budget.max_trials if bo_mode else 0)
    ledger_path = cfg.output_dir / TRIALS_FILENAME
    ledger_path.write_text("", encoding="utf-8")

    if progress_callback is not None:
        progress_callback(0, total_budget, None)

    records: list[dict] = []
    evaluations: list[PlanEvaluation] = []
    done = 0

    def _record(evaluation: PlanEvaluation) -> None:
        nonlocal done
        evaluations.append(evaluation)
        _append_ledger(records, ledger_path, evaluation)
        done += 1
        if progress_callback is not None:
            progress_callback(done, total_budget, evaluation.plan.plan_id)

    with BatchEvaluator(
        scenario_sumocfg=Path(scenario.sumocfg_file),
        net_file=net_file,
        trials_root=cfg.output_dir / "trials",
        sim_end=cfg.sim_end,
        stalls=stalls,
        # No validity gates: with teleports disabled, congestion/incident
        # severity flows into the MOE (unfinished + undeparted demand is
        # scored), so a bad plan loses by score rather than by invalidation.
        quality_thresholds={
            "emergency_stops": None,
            "teleports": None,
            "collisions": None,
        },
        n_workers=cfg.n_workers,
        vehicle_behavior=cfg.vehicle_behavior,
        map_dir=map_dir_used,
    ) as evaluator:
        seed_results = evaluator.evaluate(list(seeds))
        baseline_index, baseline_moe = _pick_baseline(cfg, seeds, seed_results)
        seed_evaluations = [
            _evaluation(plan, result, baseline_moe, cfg.kpi)
            for plan, result in zip(seeds, seed_results)
        ]
        for evaluation in seed_evaluations:
            _record(evaluation)

        if bo_mode is not None:
            best_seed = min(
                (e for e in seed_evaluations if e.valid), key=lambda e: e.score
            )
            optimizer = None
            if bo_mode == "nema_offsets":
                try:
                    optimizer = BayesianNemaOffsetOptimizer(
                        best_seed.plan, budget=cfg.budget, rng_seed=cfg.seed
                    )
                except ValueError as exc:
                    # Cycles neither equal nor integer multiples: cannot hold a
                    # stable coordination — evaluate the real baseline only.
                    search_skipped = _skip_search(
                        "stored-plan seed %r cannot be offset-optimized (%s); "
                        "baseline evaluation only",
                        best_seed.plan.plan_id,
                        exc,
                    )
            elif bo_mode == "nema_splits":
                try:
                    optimizer = BayesianNemaSplitOptimizer(
                        best_seed.plan, budget=cfg.budget, rng_seed=cfg.seed
                    )
                except ValueError as exc:
                    # E.g. the cycle barely covers the minimum greens: nothing
                    # to trade — evaluate the real baseline only.
                    search_skipped = _skip_search(
                        "stored-plan seed %r cannot be split-optimized (%s); "
                        "baseline evaluation only",
                        best_seed.plan.plan_id,
                        exc,
                    )
            elif bo_mode == "offsets":
                try:
                    optimizer = BayesianOffsetOptimizer(
                        best_seed.plan, budget=cfg.budget, rng_seed=cfg.seed
                    )
                except ValueError as exc:
                    # The best valid seed may have per-TLS cycles without a common
                    # value (a customer's own programs, or a mixed seed pool) or too
                    # few TLS. The seeds are already evaluated: degrade to seeds-only
                    # instead of crashing, under auto and for an explicit
                    # bayes_offsets alike.
                    search_skipped = _skip_search(
                        "%soffset search not applicable to best valid seed %r (%s); "
                        "seeds only",
                        "auto optimizer: " if cfg.optimizer == "auto" else "",
                        best_seed.plan.plan_id,
                        exc,
                    )
            elif bo_mode == "corridor":
                try:
                    optimizer = BayesianCorridorOptimizer(
                        best_seed.plan,
                        budget=cfg.budget,
                        rng_seed=cfg.seed,
                        cycle_bounds=_cycle_bounds(cfg),
                    )
                except ValueError as exc:
                    # The best seed is a plan the joint search cannot start from: e.g. a
                    # customer's own programs (seed_from_scenario_programs) with a
                    # different cycle at every signal while the cycle is not searched.
                    # The seeds are already evaluated; report them rather than fail.
                    search_skipped = _skip_search(
                        "corridor search not applicable to best valid seed %r (%s); "
                        "seeds only",
                        best_seed.plan.plan_id,
                        exc,
                    )
            elif (
                cfg.optimizer == "auto" and _n_green_stages(best_seed.plan) < 2
            ):
                # Mixed seed pool: the best valid seed turned out to be a
                # single-green program even though other seeds have >= 2 green
                # stages. Under auto, degrade to seeds-only instead of crashing.
                search_skipped = _skip_search(
                    "auto optimizer: best valid seed %r has fewer than 2 green "
                    "stages; skipping the Bayesian split search (seeds only)",
                    best_seed.plan.plan_id,
                )
            else:
                try:
                    optimizer = BayesianSplitOptimizer(
                        best_seed.plan,
                        budget=cfg.budget,
                        rng_seed=cfg.seed,
                        cycle_bounds=_cycle_bounds(cfg),
                    )
                except ValueError as exc:
                    # E.g. a single green stage under an explicit bayes_splits (auto checks
                    # that above) or too little green time for the minimum green: there is
                    # nothing to split, so report the seeds instead of failing the run.
                    search_skipped = _skip_search(
                        "split search not applicable to best valid seed %r (%s); "
                        "seeds only",
                        best_seed.plan.plan_id,
                        exc,
                    )
            # Every mode warm-starts with the plan BO derives from. Other valid seeds
            # go only to the corridor optimizer: it raises ValueError, before touching
            # the study, for a plan it cannot represent (another TLS set, number of
            # green stages or non-green time at a TLS, or, in fixed-cycle mode,
            # another cycle), and such a seed is skipped; it stays in the ledger as
            # context. The offsets and splits optimizers encode only part of a plan,
            # so they would score an extra seed at a plan that was never evaluated.
            if optimizer is None:
                # Degraded to seeds-only: the BO share of the budget will
                # never run — shrink the reported total so progress can reach
                # done == total.
                total_budget = len(seeds)
                if progress_callback is not None:
                    progress_callback(
                        len(seed_evaluations), total_budget, None
                    )
            else:
                optimizer.warm_start([best_seed])
                warm_starts["used"].append(best_seed.plan.plan_id)
                if bo_mode == "corridor":
                    for evaluation in seed_evaluations:
                        if evaluation is best_seed:
                            continue
                        if not evaluation.valid:
                            warm_starts["skipped"].append(
                                {"plan_id": evaluation.plan.plan_id,
                                 "reason": "its trial was not valid"}
                            )
                            continue
                        try:
                            optimizer.warm_start([evaluation])
                        except ValueError as exc:
                            logger.info(
                                "seed %r not used as a warm start (%s)",
                                evaluation.plan.plan_id, exc,
                            )
                            warm_starts["skipped"].append(
                                {"plan_id": evaluation.plan.plan_id, "reason": str(exc)}
                            )
                        else:
                            warm_starts["used"].append(evaluation.plan.plan_id)
            while optimizer is not None and not optimizer.is_finished():
                batch = optimizer.ask(cfg.n_workers)
                if not batch:
                    break
                batch_results = evaluator.evaluate(batch)
                batch_evaluations = [
                    _evaluation(plan, result, baseline_moe, cfg.kpi)
                    for plan, result in zip(batch, batch_results)
                ]
                optimizer.tell(batch_evaluations)
                for evaluation in batch_evaluations:
                    _record(evaluation)

    winner = _pick_winner(evaluations)
    baseline_evaluation = evaluations[baseline_index]

    # Refine stage: OFF by default (refine_top_k == 0 or
    # refine_vehicle_behavior is None) -> refine_block stays None,
    # export_source stays None (key omitted from the result, see below), and
    # export_plan stays the coarse winner, so the result JSON and the NEMA
    # export are byte-identical to a build without this feature at all.
    export_plan = winner.plan
    refine_block: dict | None = None
    # "refine" | "coarse" | None. Only set (and only added to the result
    # dict) when the refine stage actually ran: "refine" when it produced a
    # usable winner, "coarse" when it ran but fell back (see the module
    # docstring: the service KPI wiring must source "optimized" KPIs from
    # result["refine"] when this is "refine", from result["best"]
    # otherwise).
    export_source: "str | None" = None
    if cfg.refine_top_k > 0 and cfg.refine_vehicle_behavior:
        refine_block, refine_winner_plan = _run_refine_stage(
            cfg,
            evaluations=evaluations,
            baseline_evaluation=baseline_evaluation,
            scenario_sumocfg=Path(scenario.sumocfg_file),
            net_file=net_file,
            stalls=stalls,
            map_dir=map_dir_used,
            trials_root=cfg.output_dir / "trials_refine",
            records=records,
            ledger_path=ledger_path,
        )
        if refine_winner_plan is not None:
            export_plan = refine_winner_plan
            export_source = "refine"
        else:
            export_source = "coarse"

    nema_payload, nema_skipped_reason = _export_nema(
        export_plan, corridor_id=corridor_id, out_dir=cfg.output_dir
    )
    nema_export = (
        {"exported": True}
        if nema_payload is not None
        else {"exported": False, "reason": nema_skipped_reason}
    )

    seed_source = baseline_evaluation.plan.provenance.source
    seed_info = {
        "source": seed_source,
        "stored_plans_used": seed_source == "stored_nema",
    }
    if nema_seed is not None:
        synthesized = [
            entry.tls_id
            for entry in nema_seed.entries
            if entry.pattern.get("pattern_id") == FREE_PATTERN_ID
        ]
        if synthesized:
            seed_info["synthesized_free"] = synthesized
            seed_info["note"] = (
                "the official schedule runs these signals uncoordinated (free) "
                "at the requested time; their seeds reuse the official phase "
                "structure with demand-computed timings"
            )
    if stored_plans_considered and seed_source != "stored_nema":
        reason = seed_fallback_reason or (
            "its plans did not cover every corridor intersection at the "
            "requested time"
        )
        if seed_source == "sumo_tls" and cfg.seed_from_scenario_programs:
            # The scenario's programs were asked for: using them is no fallback.
            outcome = (
                "the run used the scenario's own signal programs, as requested "
                "(seed_from_scenario_programs)"
            )
        else:
            outcome = f"the run fell back to the {seed_source!r} seed"
        seed_info["note"] = f"a plan store was configured but was not used ({reason}); {outcome}"

    result = {
        "schema": RESULT_SCHEMA,
        "seed": seed_info,
        "resolved_corridor": [asdict(item) for item in corridor.intersections],
        "baseline": {
            "plan_id": baseline_evaluation.plan.plan_id,
            "score": _json_score(baseline_evaluation.score),
            "moe": baseline_evaluation.moe,
            "timing": _timing(baseline_evaluation.plan),
        },
        "best": {
            "plan_id": winner.plan.plan_id,
            "score": _json_score(winner.score),
            "moe": winner.moe,
            "offsets": _offsets(winner.plan),
            "timing": _timing(winner.plan),
            "source": winner.plan.provenance.source,
            "trial_dir": winner.trial_dir,
        },
        "trials": records,
        "nema": {
            "path": str(cfg.output_dir / NEMA_JSON_FILENAME),
            "handoff_ready": (
                bool(nema_payload["handoff_ready"]) if nema_payload is not None else False
            ),
        },
        "nema_export": nema_export,
        "unserved_movements": _unserved_movements(
            export_plan, net_file, cfg.signal_groups_overrides
        ),
        "search": {
            "ran": search_skipped is None,
            "reason": search_skipped,
            "warm_starts": warm_starts,
        },
        "config": _config_echo(
            cfg, tmc_json=tmc_json, resolved_base_sumocfg=base_sumocfg, map_id=map_id_used
        ),
        "n_trials": len(records),
    }
    if refine_block is not None:
        # Added ONLY when the refine stage actually ran: this key's absence
        # (not a null/False placeholder) is what keeps a disabled run's
        # result byte-identical to a build without this feature.
        result["refine"] = refine_block
    if export_source is not None:
        # Same rule: absent (not "coarse") when refine never ran at all.
        result["export_source"] = export_source
    if cfg.vehicle_behavior == "cnde":
        # Named at the top, next to the plans it scored: every candidate (the
        # seeds and every Bayesian trial) ran under the CNDE learned driver
        # behavior, not SUMO's own. Absent under the default behavior, so such
        # a result stays as it was.
        result["traffic_model"] = "cnde"
    (cfg.output_dir / RESULT_FILENAME).write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


# ---------------------------------------------------------------------------
# Movements the exported plan never serves
# ---------------------------------------------------------------------------

#: SUMO link states that let no traffic through: red, red+yellow, yellow.
_NO_MOVEMENT_STATES = frozenset("ruyY")


def _unserved_movements(
    plan, net_file: Path, overrides_path: "Path | None"
) -> "dict[str, list[str]]":
    """Vehicle movements ``plan`` never lets move, per TLS (``{}`` when none).

    A movement is unserved when every phase shows each of its links as red or
    yellow (``r``, ``u``, ``y``, ``Y``). Green (``G``/``g``), a green arrow that
    requires stopping (``s``) and an uncontrolled link (``o``/``O``) all let traffic
    move. Generated plans give every approach with controlled links a stage;
    a customer's own programs or a timing sheet may leave a movement permanently red.
    NEMA plans report none: materialising one already refuses a plan that leaves a
    movement without green. A TLS whose links cannot be classified is left out (with
    a warning): this is a report and must not fail a finished run.
    """
    if isinstance(plan, NemaCorridorPlan):
        return {}
    from terasim_city.plans.signal_groups import derive_link_movements, load_overrides

    overrides = load_overrides(overrides_path) if overrides_path is not None else {}
    unserved: dict[str, list[str]] = {}
    for signal_plan in plan.plans:
        try:
            links = derive_link_movements(
                Path(net_file), signal_plan.tls_id, overrides=overrides.get(signal_plan.tls_id)
            )
        except ValueError as exc:
            logger.warning(
                "cannot check which movements plan %r serves at TLS %s: %s",
                plan.plan_id, signal_plan.tls_id, exc,
            )
            continue
        served = {
            link.movement
            for link in links
            if any(
                link.link_index < len(phase.state)
                and phase.state[link.link_index] not in _NO_MOVEMENT_STATES
                for phase in signal_plan.phases
            )
        }
        missing = {link.movement for link in links} - served
        if missing:
            unserved[signal_plan.tls_id] = sorted(missing, key=MOVEMENTS.index)
    if unserved:
        logger.warning(
            "plan %r never lets these vehicle movements move: %s",
            plan.plan_id,
            "; ".join(f"{tls} {', '.join(moves)}" for tls, moves in unserved.items()),
        )
    return unserved


# ---------------------------------------------------------------------------
# NEMA export
# ---------------------------------------------------------------------------

def _export_nema(
    plan, *, corridor_id: str, out_dir: Path
) -> "tuple[dict | None, str | None]":
    """Write ``nema_plan.json`` for ``plan``: ``(payload, None)``, or ``(None, reason)``.

    A static plan is exported as ONE coordinated timing sheet, which needs a common
    cycle. A customer's own programs (``seed_from_scenario_programs``) often run a
    different cycle at every signal, and such a plan can still win when nothing beats
    it. That must not fail the run after every trial has been paid for, so the export
    is skipped with a warning and the reason goes into the result. The same holds when
    the exporter refuses a program (no green phase at a signal, or stage splits that
    do not add up to its cycle). NEMA plans carry their own per-pattern timing and
    always export.
    """
    if isinstance(plan, NemaCorridorPlan):
        return export_nema_plan(plan, corridor_id=corridor_id, out_dir=out_dir), None
    try:
        plan.common_cycle_s()
    except ValueError:
        cycles = ", ".join(f"{p.tls_id}={p.cycle_s:g}s" for p in plan.plans)
        reason = (
            f"NEMA export skipped: plan {plan.plan_id!r} has no common cycle ({cycles}), "
            "so it cannot be written as one coordinated NEMA timing"
        )
        logger.warning("%s", reason)
        return None, reason
    try:
        return export_nema(plan, corridor_id=corridor_id, out_dir=out_dir), None
    except ValueError as exc:
        reason = f"NEMA export skipped: plan {plan.plan_id!r}: {exc}"
        logger.warning("%s", reason)
        return None, reason


# ---------------------------------------------------------------------------
# Turning counts
# ---------------------------------------------------------------------------

def _write_derived_tmc(cfg: CorridorOptimizationConfig) -> Path:
    """Derive the TMC from the base scenario's route files; return ``derived_tmc.json``.

    The counts cover the demand inside ``[0, sim_end)``, the window the trials
    simulate (their sumocfg is rewritten to begin at 0 and end at ``sim_end``),
    not whatever longer period the customer's own sumocfg spans.

    The file is written into ``output_dir`` and replaces ``cfg.tmc_json`` for
    the whole run. The given ``tmc_json`` is only a placeholder here: its
    ``corridor_id`` and ``timestamp_start`` are carried over when it has them
    (the time selects the plan-store patterns), otherwise the corridor is called
    "scenario" and the timestamp is the epoch.
    """
    from terasim_city.flow.counts_from_routes import derive_tmc_from_routes
    from terasim_city.plans.signal_groups import load_overrides

    corridor_id = "scenario"
    carried: dict[str, str] = {}
    try:
        placeholder = json.loads(Path(cfg.tmc_json).read_text(encoding="utf-8"))
        if isinstance(placeholder, dict):
            if placeholder.get("corridor_id"):
                corridor_id = str(placeholder["corridor_id"])
            if placeholder.get("timestamp_start"):
                carried["timestamp_start"] = str(placeholder["timestamp_start"])
    except (OSError, ValueError):
        pass  # the placeholder may be absent or empty; it is not read for anything else
    payload = derive_tmc_from_routes(
        cfg.base_sumocfg,
        cfg.tls_ids,
        corridor_id=corridor_id,
        overrides=(
            load_overrides(cfg.signal_groups_overrides) if cfg.signal_groups_overrides else None
        ),
        horizon_s=(0.0, float(cfg.sim_end)),
        **carried,
    )
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    derived = cfg.output_dir / DERIVED_TMC_FILENAME
    derived.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return derived


# ---------------------------------------------------------------------------
# Seed construction
# ---------------------------------------------------------------------------

def _load_stored_nema_seed(
    cfg: CorridorOptimizationConfig,
    corridor: ResolvedCorridor,
    corridor_id: str,
    tmc: TMCData,
) -> "tuple[NemaCorridorPlan | None, str | None]":
    """Ladder rung -1: the assembled real plans from the plan store.

    Returns ``(seed, None)`` when every corridor intersection has a stored
    document, else ``(None, reason)`` — the reason surfaces in the result's
    ``seed.note`` so a silent fallback to a generated seed is visible to the
    caller, not just in the logs.
    Intersections whose schedule is coordinated at the query time use their
    stored pattern; intersections running "free" at that hour keep their
    official skeleton (rings, movements, clearances) and get demand-computed
    timings (see plans.nema_webster). Only an intersection with NO stored
    document at all sends the corridor to the static ladder.
    """
    if cfg.plan_data_root is None:
        return None, None
    from datetime import datetime

    when_raw = cfg.plan_time or tmc.timestamp_start
    if not when_raw:
        logger.warning(
            "plan store given but neither plan_time nor the TMC timestamp_start "
            "is set; cannot pick a time-of-day pattern — static ladder"
        )
        return None, (
            "neither plan_time nor the TMC timestamp_start is set, so no "
            "time-of-day pattern could be selected"
        )
    when = datetime.fromisoformat(str(when_raw).replace("Z", "+00:00"))
    if when.tzinfo is not None:
        # Schedules are local wall-clock tables: an aware timestamp must be
        # converted into the corridor's LOCAL time, never just stripped (a
        # "16:16Z" summer reading on a US Pacific corridor is 09:16 local —
        # AM peak, not PM). The timezone is a function of where the corridor
        # is, so it is derived from the corridor's own coordinates —
        # deliberately not a config knob: a knob is a way to silently run
        # another city's clock.
        from terasim_city.geo_timezone import timezone_for

        anchor = tmc.intersections[0]
        tz_name = timezone_for(anchor.lat, anchor.lon)
        if tz_name is None:
            raise ValueError(
                f"plan time {when_raw!r} carries a timezone but the corridor's "
                f"local timezone could not be derived from its coordinates "
                f"({anchor.lat}, {anchor.lon}); time-of-day patterns cannot "
                "be selected without local wall-clock time (is tzfpy "
                "installed?)"
            )
        from zoneinfo import ZoneInfo

        logger.info(
            "derived corridor timezone %s from (%s, %s)",
            tz_name, anchor.lat, anchor.lon,
        )
        when = when.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)

    store = PlanStore(cfg.plan_data_root)
    rows, warnings = plan_selections(
        corridor_id, corridor.intersections, store, when
    )
    for message in warnings:
        logger.info("plan store: %s", message)
    no_plan = [sel.tls_id for sel, _, _ in rows if sel.source == "no_plan"]
    if no_plan:
        logger.info(
            "no stored plan for TLS %s; falling through to the static seed ladder",
            no_plan,
        )
        return None, f"no stored plan covers TLS {sorted(no_plan)}"

    # "free" rows: the schedule runs this hour uncoordinated, but the document
    # still provides the official skeleton (rings, movements, clearances) —
    # time it from the measured demand instead of discarding it.
    free_rows = [(sel, sp) for sel, sp, pattern in rows if sel.source == "free"]
    synth_patterns: dict[str, dict] = {}
    if free_rows:
        counts_by_iid = {i.intersection_id: i.counts for i in tmc.intersections}
        # The counts cover one observation window; the Webster math is hourly.
        count_scale = 3600.0 / tmc.observation_seconds()
        stored_cycles = [
            float(pattern["cycle_s"]) for sel, _, pattern in rows if sel.source == "nema"
        ]
        missing_counts = [
            sel.tls_id for sel, _ in free_rows
            if counts_by_iid.get(sel.intersection_id) is None
        ]
        if missing_counts:
            logger.info(
                "free-hour TLS %s have no TMC counts to time the official "
                "skeleton with; falling through to the static seed ladder",
                missing_counts,
            )
            return None, (
                f"free-hour TLS {sorted(missing_counts)} have no TMC counts "
                "to time the official skeleton with"
            )
        if stored_cycles:
            # Coordination compatibility: synthesized patterns adopt the
            # active stored cycle instead of inventing their own.
            corridor_cycle = max(stored_cycles)
        else:
            corridor_cycle = max(
                recommended_cycle_s(
                    sp.plan["intersections"][0],
                    counts_by_iid[sel.intersection_id],
                    scale=count_scale,
                )
                for sel, sp in free_rows
            )
        try:
            for sel, sp in free_rows:
                synth_patterns[sel.tls_id] = synthesize_free_pattern(
                    sp.plan["intersections"][0],
                    counts_by_iid[sel.intersection_id],
                    cycle_s=corridor_cycle,
                    scale=count_scale,
                )
        except ValueError as exc:
            # An uploaded official plan being discarded is a WARNING-level
            # event, and the reason must reach the result payload — a user
            # who uploaded a government plan should not need the logs to
            # learn the run silently swapped it for a synthesized seed.
            logger.warning(
                "could not time the official skeleton for %s (%s); falling "
                "through to the static seed ladder",
                sel.tls_id,
                exc,
            )
            return None, (
                f"could not time the official skeleton for {sel.tls_id}: {exc}"
            )
        logger.info(
            "free-hour TLS %s seeded from their official skeletons "
            "(demand-timed, cycle %.1fs)",
            sorted(synth_patterns),
            corridor_cycle,
        )

    entries = tuple(
        NemaSignalEntry(
            sel.tls_id,
            sp.plan["intersections"][0],
            pattern if pattern is not None else synth_patterns[sel.tls_id],
        )
        for sel, sp, pattern in rows
    )
    return NemaCorridorPlan(
        plan_id="stored_nema",
        entries=entries,
        provenance=PlanProvenance(source="stored_nema"),
    ), None


def _build_seed_plans(
    cfg: CorridorOptimizationConfig,
    corridor: ResolvedCorridor,
    net_file: Path,
    *,
    base_sumocfg: Path,
    tmc_json: Path,
) -> list[CorridorPlan]:
    """Build the seed plans for the static path (no stored NEMA plans).

    At most two seeds, in baseline-priority order (``_pick_baseline`` takes the
    first valid one) — the baseline is therefore deterministic by construction
    (never dependent on file ordering):

    0. ``seed_from_scenario_programs``: the scenario's own ``<tlLogic>`` programs
       (the net and additional files of ``base_sumocfg``, the map this run
       resolved), i.e. the customer's current timing. Opt-in.
    1. ``baseline_sheet`` given: the sheet's plan.
    2. Otherwise: a Webster plan generated from the counts in ``tmc_json``
       (the run's TMC: ``cfg.tmc_json``, or the file derived from the
       scenario's routes), decomposed per ``cfg.stage_mode`` and with its
       cycle clamped to ``[cfg.cycle_min_s, cfg.cycle_max_s]``.

    1 and 2 are alternatives, so by default there is a single seed. Programs
    bundled in the map's additional files are deliberately NOT seeds unless 0
    asks for them: the product's plan inputs are uploaded ``nema_plan.v1``
    documents (handled upstream via the plan store) or nothing — in which case
    Webster, computed from the observed demand, is the reproducible baseline.
    """
    seeds: list[CorridorPlan] = []
    if cfg.seed_from_scenario_programs:
        seeds.append(load_scenario_programs(
            base_sumocfg, corridor.tls_ids, overrides_path=cfg.signal_groups_overrides
        ))
    if cfg.baseline_sheet is not None:
        seeds.append(_load_baseline_plan(cfg, corridor, net_file))
    else:
        seeds.append(build_webster_plan(
            tmc_json,
            corridor,
            net_file,
            overrides_path=cfg.signal_groups_overrides,
            stage_mode=cfg.stage_mode,
            cycle_min_s=cfg.cycle_min_s,
            cycle_max_s=cfg.cycle_max_s,
        ))
    return seeds


def _load_baseline_plan(
    cfg: CorridorOptimizationConfig,
    corridor: ResolvedCorridor,
    net_file: Path,
) -> CorridorPlan:
    """The baseline sheet's plan for the resolved corridor's signals.

    The sheet may describe more signals than the run covers: a corridor's sheet
    seeds a request for one of its intersections, or for a from/to pair inside
    it, with those signals' entries only. A resolved signal the sheet has no entry
    for raises ``TimingSheetCoverageError``.
    """
    mapping = _sheet_tls_mapping(cfg.baseline_sheet, corridor)
    return load_timing_sheet(
        cfg.baseline_sheet,
        net_file=net_file,
        tls_by_intersection=mapping,
        overrides_path=cfg.signal_groups_overrides,
        tls_ids=corridor.tls_ids,
    )


def _sheet_tls_mapping(
    sheet_path: Path, corridor: ResolvedCorridor
) -> "dict[str, str] | None":
    """Map sheet intersections without a ``tls_id`` to the corridor's signals.

    By intersection id when every corridor intersection has a TMC intersection id
    (whatever the sheet's order and length: entries of intersections outside the
    corridor are not part of the run). Otherwise BY ORDER, which needs the sheet
    to list exactly the corridor's intersections.

    Returns ``None`` when every sheet entry carries its own ``tls_id`` (the
    sheet is self-sufficient) or when the sheet is malformed —
    ``load_timing_sheet`` then raises its own, more detailed error.
    """
    payload = json.loads(Path(sheet_path).read_text(encoding="utf-8"))
    entries = payload.get("intersections") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) for entry in entries
    ):
        return None
    if all(entry.get("tls_id") for entry in entries):
        return None
    if all(item.intersection_id is not None for item in corridor.intersections):
        return {item.intersection_id: item.tls_id for item in corridor.intersections}
    if len(entries) != len(corridor.intersections):
        raise TimingSheetCoverageError(
            f"baseline sheet {sheet_path} has {len(entries)} intersection(s) but "
            f"the resolved corridor has {len(corridor.intersections)} "
            f"({', '.join(corridor.tls_ids)}); cannot map sheet intersections to "
            "TLS by order"
        )
    mapping: dict[str, str] = {}
    for position, (entry, item) in enumerate(zip(entries, corridor.intersections)):
        sheet_iid = entry.get("intersection_id")
        if not sheet_iid:
            return None  # load_timing_sheet reports the missing id itself
        sheet_iid = str(sheet_iid)
        if item.intersection_id is not None and sheet_iid != item.intersection_id:
            raise TimingSheetCoverageError(
                f"baseline sheet {sheet_path}: intersections[{position}] is "
                f"{sheet_iid!r} but the resolved corridor has "
                f"{item.intersection_id!r} (tls {item.tls_id!r}) at that "
                "position; sheet order must match the corridor order"
            )
        mapping[sheet_iid] = item.tls_id
    return mapping


# ---------------------------------------------------------------------------
# Scoring / selection
# ---------------------------------------------------------------------------

def _bo_mode(optimizer: str, n_tls: int) -> "str | None":
    """Which Bayesian phase runs after the seeds.

    One of 'offsets', 'splits', 'corridor', or None (seeds only).
    """
    if optimizer == "exhaustive":
        return None
    if optimizer == "bayes_offsets":
        if n_tls < 2:
            raise ValueError(
                "optimizer 'bayes_offsets' needs a corridor of >= 2 TLS "
                f"(resolved corridor has {n_tls}); a single intersection has "
                "no relative offset — use 'auto' or 'bayes_splits'"
            )
        return "offsets"
    if optimizer == "bayes_splits":
        if n_tls >= 2:
            raise ValueError(
                "optimizer 'bayes_splits' needs a single-TLS corridor "
                f"(resolved corridor has {n_tls}); corridors coordinate via "
                "offsets — use 'auto' or 'bayes_offsets'"
            )
        return "splits"
    if optimizer == "bayes_corridor":
        if n_tls < 2:
            raise ValueError(
                "optimizer 'bayes_corridor' needs a corridor of >= 2 TLS "
                f"(resolved corridor has {n_tls}); use 'auto' or 'bayes_splits'"
            )
        return "corridor"
    return "offsets" if n_tls >= 2 else "splits"  # "auto"


def _skip_search(message: str, *args) -> str:
    """Log that the Bayesian phase is skipped and return the sentence.

    Every degrade to seeds-only goes through here, so the warning in the log and the
    ``reason`` in ``result["search"]`` are the same text.
    """
    sentence = message % args
    logger.warning("%s", sentence)
    return sentence


def _cycle_bounds(cfg: CorridorOptimizationConfig) -> "tuple[float, float] | None":
    """Cycle bounds for the Bayesian phase, or ``None`` when the cycle stays fixed."""
    return (cfg.cycle_min_s, cfg.cycle_max_s) if cfg.search_cycle else None


def _n_green_stages(plan: CorridorPlan) -> int:
    """Green-kind phase count of a single-TLS corridor plan's signal plan."""
    return sum(
        1 for phase in plan.plans[0].phases if phase.kind == "green"
    )


def _pick_baseline(
    cfg: CorridorOptimizationConfig,
    seeds: Sequence[CorridorPlan],
    seed_results: Sequence[dict],
) -> tuple[int, dict]:
    """Return ``(seed_index, baseline_moe)`` or raise when no seed is usable."""
    if cfg.baseline_sheet is not None and not cfg.seed_from_scenario_programs:
        # Seeds are exactly [baseline plan] by construction. (With the scenario's
        # programs in front of the sheet, the first valid seed is the baseline.)
        if seed_results[0].get("valid"):
            return 0, seed_results[0]["moe"]
        raise RuntimeError(_no_valid_seed_message(seeds, seed_results))
    for index, result in enumerate(seed_results):
        if result.get("valid"):
            return index, result["moe"]
    raise RuntimeError(_no_valid_seed_message(seeds, seed_results))


def _no_valid_seed_message(
    seeds: Sequence[CorridorPlan], seed_results: Sequence[dict]
) -> str:
    details = []
    for plan, result in zip(seeds, seed_results):
        reason = result.get("error") or "; ".join(
            result.get("quality_errors") or ()
        ) or "unknown failure"
        details.append(f"{plan.plan_id}: {reason}")
    return (
        "no seed plan produced a valid evaluation; optimization cannot "
        "establish a baseline. Per-seed errors: " + " | ".join(details)
    )


def _evaluation(
    plan: CorridorPlan, result: dict, baseline_moe: dict, kpi: KpiConfig
) -> PlanEvaluation:
    valid = bool(result.get("valid"))
    moe = result.get("moe") or {}
    score = score_moe(moe, baseline_moe, kpi) if valid else math.inf
    return PlanEvaluation(
        plan=plan,
        moe=moe,
        score=score,
        valid=valid,
        trial_dir=str(result.get("trial_dir", "")),
    )


def _pick_winner(evaluations: Sequence[PlanEvaluation]) -> PlanEvaluation:
    valid = [e for e in evaluations if e.valid and math.isfinite(e.score)]
    if not valid:
        # Unreachable in practice: _pick_baseline already requires a valid
        # seed. Kept as a fail-loud guard for future flow changes.
        raise RuntimeError(
            "no valid trial at the end of the optimization run; "
            f"{len(evaluations)} trial(s) were all invalid"
        )
    return min(valid, key=lambda e: e.score)  # min keeps the first of ties


# ---------------------------------------------------------------------------
# Refine stage
# ---------------------------------------------------------------------------

def _plan_fingerprint(plan) -> str:
    """Canonical identity of a plan's actual signal timing (dedup key).

    Deliberately independent of ``plan_id``/``provenance``: two trials can
    carry different plan_ids (e.g. a BO iteration that resamples a point
    already seen) while describing the exact same signal timing, and the
    top-K refine selection must treat those as ONE candidate.
    """
    if isinstance(plan, NemaCorridorPlan):
        payload = {
            "kind": "nema",
            "entries": [
                {"tls_id": entry.tls_id, "pattern": entry.pattern}
                for entry in sorted(plan.entries, key=lambda e: e.tls_id)
            ],
        }
    else:
        payload = {
            "kind": "corridor",
            "plans": [
                {
                    "tls_id": signal_plan.tls_id,
                    "offset_s": signal_plan.offset_s,
                    "phases": [phase.to_json_dict() for phase in signal_plan.phases],
                }
                for signal_plan in sorted(plan.plans, key=lambda sp: sp.tls_id)
            ],
        }
    return json.dumps(payload, sort_keys=True)


def _select_top_k_plans(
    evaluations: Sequence[PlanEvaluation], k: int
) -> list[PlanEvaluation]:
    """The K best DISTINCT-parameterization valid evaluations, ascending score.

    "Distinct" dedupes by :func:`_plan_fingerprint`, not by plan_id. Ties keep
    the earlier (ledger-order) evaluation, matching :func:`_pick_winner`.
    Returns fewer than ``k`` when fewer distinct valid evaluations exist —
    ``evaluations`` bounds ``k``, it is never padded. Generic over what pool
    it is given: ``_run_refine_stage`` passes it the coarse pool WITH the
    baseline excluded, so the baseline never consumes one of the K slots
    (see its own docstring).
    """
    valid = [e for e in evaluations if e.valid and math.isfinite(e.score)]
    ranked = sorted(valid, key=lambda e: e.score)
    selected: list[PlanEvaluation] = []
    seen: set[str] = set()
    for evaluation in ranked:
        fingerprint = _plan_fingerprint(evaluation.plan)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        selected.append(evaluation)
        if len(selected) >= k:
            break
    return selected


def _refine_evaluation(
    plan, result: dict, baseline_moe: "dict | None", kpi: KpiConfig
) -> PlanEvaluation:
    """Like :func:`_evaluation` but tolerates an unusable ``baseline_moe``.

    The refine stage re-evaluates its own baseline in the SAME behavior/
    process as the top-K candidates (see ``_run_refine_stage``) so
    ``score_moe``'s ratio semantics never mix behaviors — but if THAT
    baseline re-evaluation itself comes back invalid, ``score_moe`` would
    raise on a missing metric. A refine-stage-only failure must not crash a
    run that already produced a valid coarse result, so this scores
    ``math.inf`` instead in that case (the caller records why via the
    ``invalid`` list).
    """
    evaluator_valid = bool(result.get("valid"))
    moe = result.get("moe") or {}
    if evaluator_valid and baseline_moe is not None:
        score = score_moe(moe, baseline_moe, kpi)
    else:
        score = math.inf
    return PlanEvaluation(
        plan=plan,
        moe=moe,
        score=score,
        valid=evaluator_valid,
        trial_dir=str(result.get("trial_dir", "")),
    )


def _quality_counts(result: dict) -> dict:
    """The kpi.json quality-gate counts an evaluation already wrote (or {})."""
    kpi = result.get("kpi") or {}
    quality_gate = kpi.get("quality_gate") or {}
    return dict(quality_gate.get("counts") or {})


def _run_refine_stage(
    cfg: CorridorOptimizationConfig,
    *,
    evaluations: Sequence[PlanEvaluation],
    baseline_evaluation: PlanEvaluation,
    scenario_sumocfg: Path,
    net_file: Path,
    stalls: list[dict],
    map_dir: "Path | None",
    trials_root: Path,
    records: list[dict],
    ledger_path: Path,
) -> "tuple[dict, object | None]":
    """High-fidelity re-evaluation of the coarse top-K under a different behavior.

    Called only when ``cfg.refine_top_k > 0 and cfg.refine_vehicle_behavior``
    (the caller's gate). Builds its own ``BatchEvaluator`` sized by
    ``cfg.refine_n_workers`` — never by ``cfg.n_workers`` (the coarse pool
    size) and never by the ``TERASIM_CITY_INLINE_EVAL`` env var, so the
    process environment cannot silently change how this stage runs:

    * ``refine_n_workers == 1`` (default): ``inline=True, n_workers=1`` — the
      baseline and the K candidates are evaluated one after another in the
      calling process, sharing one loaded behavior runtime (for "cnde": one
      model copy, one CUDA context).
    * ``refine_n_workers > 1``: ``inline=False`` — a spawn pool of that many
      processes (``maxtasksperchild=1``: every candidate gets a fresh process
      that loads the runtime itself), so up to ``refine_n_workers``
      candidates run concurrently. Nothing caps the count to the number of
      candidates; surplus workers idle.

    Both modes produce the same results: the iterations whose order could
    depend on Python's per-process string-hash seed (vehicle registration in
    ``terasim.envs``, control-command execution, the csim/nnde model batch
    order) run in SUMO-id / insertion order, and every trial seeds its RNGs
    on entry (``terasim_city.optimize.evaluator._seed_trial_rngs``). Two
    independent processes write byte-identical tripinfo for the same plan,
    which makes the pooled mode a pure wall-clock vs. GPU-memory trade-off (each
    concurrent "cnde" worker holds the 972 MB checkpoint plus a CUDA
    context, ~1.5 GB). The baseline plan is still re-evaluated in the SAME
    batch as the top-K candidates so ``score_moe``'s baseline-ratio
    semantics are computed under ONE consistent behavior (never mixed with
    the coarse stage's baseline_moe, which was measured under a different
    behavior and is not comparable), and
    ``BatchEvaluator.evaluate`` returns results in ``candidate_plans`` order
    in both modes, so ``results[0]`` is always the baseline.

    Returns ``(refine_block, refine_winner_plan)``; ``refine_winner_plan`` is
    ``None`` when nothing is a usable winner candidate (baseline refine
    failure, or every candidate gated/invalid) — the caller then keeps
    exporting the coarse winner.
    """
    baseline_plan = baseline_evaluation.plan
    baseline_fingerprint = _plan_fingerprint(baseline_plan)

    # The baseline is ALWAYS refined (below, for score_moe's ratio
    # semantics) regardless of its coarse rank, so it must not consume one
    # of the K top-K slots meant for genuine alternatives: exclude it from
    # the ranking pool FIRST, select K from what remains, then union the
    # baseline back unconditionally. A competitive baseline therefore never
    # takes one of the K slots: a run refines K real alternatives whenever
    # that many distinct plans exist.
    non_baseline_pool = [
        evaluation
        for evaluation in evaluations
        if _plan_fingerprint(evaluation.plan) != baseline_fingerprint
    ]
    top_k = _select_top_k_plans(non_baseline_pool, cfg.refine_top_k)
    extra_plans = [evaluation.plan for evaluation in top_k]

    candidate_plans = [baseline_plan] + extra_plans
    # 1 -> sequential in this process (BatchEvaluator(inline=True), exactly
    # the pre-knob behavior); > 1 -> a real spawn pool of that many refine
    # processes. Both passed as constructor args, never read from the env.
    refine_inline = cfg.refine_n_workers == 1
    logger.info(
        "refine stage: behavior=%r requested_top_k=%d distinct_candidates=%d "
        "(baseline + %d) - %s",
        cfg.refine_vehicle_behavior,
        cfg.refine_top_k,
        len(candidate_plans),
        len(extra_plans),
        "inline, single process (refine_n_workers=1)"
        if refine_inline
        else f"spawn pool of {cfg.refine_n_workers} processes "
        f"(refine_n_workers={cfg.refine_n_workers})",
    )

    with BatchEvaluator(
        scenario_sumocfg=scenario_sumocfg,
        net_file=net_file,
        trials_root=trials_root,
        sim_end=cfg.sim_end,
        stalls=stalls,
        # No evaluator-level validity gate here either (see the coarse
        # BatchEvaluator construction above): the refine_max_collisions gate
        # is applied by THIS function, after the fact, from the counts the
        # evaluator already writes to kpi.json — never by changing what
        # counts as a "valid" simulation, and never by touching score_moe's
        # 0.7/0.3 weighting.
        quality_thresholds={
            "emergency_stops": None,
            "teleports": None,
            "collisions": None,
        },
        n_workers=cfg.refine_n_workers,
        vehicle_behavior=cfg.refine_vehicle_behavior,
        map_dir=map_dir,
        inline=refine_inline,
    ) as refine_evaluator:
        results = refine_evaluator.evaluate(candidate_plans)

    baseline_result = results[0]
    baseline_refine_moe = (
        baseline_result.get("moe")
        if bool(baseline_result.get("valid")) and baseline_result.get("moe") is not None
        else None
    )
    if baseline_refine_moe is None:
        logger.warning(
            "refine stage: baseline plan %r produced no valid evaluation "
            "under vehicle_behavior %r (%s); refine scores cannot be "
            "computed - no refine winner will be selected and the nema "
            "export stays at the coarse winner",
            baseline_plan.plan_id,
            cfg.refine_vehicle_behavior,
            baseline_result.get("error") or baseline_result.get("quality_errors"),
        )

    baseline_entry: dict | None = None
    evaluation_entries: list[dict] = []
    invalid_entries: list[dict] = []
    winner_candidates: list[tuple[object, float]] = []

    for plan, result in zip(candidate_plans, results):
        evaluation = _refine_evaluation(plan, result, baseline_refine_moe, cfg.kpi)
        _append_ledger(records, ledger_path, evaluation, stage="refine")

        counts = _quality_counts(result)
        collisions = counts.get("collisions")
        entry = {
            "plan_id": plan.plan_id,
            "score": _json_score(evaluation.score),
            "valid": evaluation.valid,
            # Full MOE alongside the scalar score: a consumer sourcing
            # "optimized" KPIs for the EXPORTED plan (which may be the refine
            # winner, not result["best"]) needs the refine-stage MOE to read
            # them from, not just the ratio score. See
            # result["export_source"] below and the module docstring.
            "moe": evaluation.moe or None,
            "collisions": collisions,
            "emergency_stops": counts.get("emergency_stops"),
            "trial_dir": evaluation.trial_dir,
        }

        reason = None
        if baseline_refine_moe is None:
            reason = (
                "refine baseline evaluation invalid: refine scores cannot "
                "be computed"
            )
        elif not evaluation.valid:
            reason = (
                result.get("error")
                or "; ".join(result.get("quality_errors") or ())
                or "invalid evaluation"
            )
        elif (
            cfg.refine_max_collisions is not None
            and collisions is not None
            and collisions > cfg.refine_max_collisions
        ):
            reason = (
                f"collisions={collisions} exceeds "
                f"refine_max_collisions={cfg.refine_max_collisions}"
            )

        if reason is not None:
            # A gate-failing (or otherwise invalid) BASELINE deliberately
            # appears BOTH here (via `baseline_entry`, below) AND in
            # `invalid_entries`: "baseline" is a fixed-identity slot (always
            # the baseline plan, valid or not) while "invalid" is the
            # complete list of every excluded candidate with its reason —
            # collapsing them would hide either the baseline's identity or
            # its exclusion reason.
            invalid_entries.append({**entry, "reason": reason})
        else:
            winner_candidates.append((plan, evaluation.score))

        if plan is baseline_plan:
            baseline_entry = entry
        else:
            evaluation_entries.append(entry)

    winner_plan = None
    winner_plan_id = None
    if winner_candidates:
        winner_plan, _ = min(winner_candidates, key=lambda item: item[1])
        winner_plan_id = winner_plan.plan_id

    refine_block = {
        "behavior": cfg.refine_vehicle_behavior,
        "top_k": cfg.refine_top_k,
        "refine_max_collisions": cfg.refine_max_collisions,
        # Audit trail of the process mode this block was produced under:
        # 1 = sequential inline, > 1 = spawn pool of that size.
        "n_workers": cfg.refine_n_workers,
        # Resolved count: baseline + however many
        # distinct alternatives were actually found, which can be < top_k
        # when fewer than top_k distinct valid coarse evaluations exist.
        "candidates_evaluated": len(candidate_plans),
        "executed": True,
        "baseline": baseline_entry,
        "evaluations": evaluation_entries,
        "winner_plan_id": winner_plan_id,
        "invalid": invalid_entries,
    }
    return refine_block, winner_plan


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _offsets(plan) -> dict[str, float]:
    if isinstance(plan, NemaCorridorPlan):
        return plan.offsets()
    return {signal_plan.tls_id: signal_plan.offset_s for signal_plan in plan.plans}


def _timing(plan) -> dict:
    """Per-TLS cycle / offset / greens of a plan for the result payload.

    NEMA plans carry patterns rather than phases, so they report no greens.
    """
    if isinstance(plan, NemaCorridorPlan):
        return {
            "cycle_s": {e.tls_id: float(e.cycle_s) for e in plan.entries},
            "offsets_s": {k: float(v) for k, v in plan.offsets().items()},
        }
    return {
        "cycle_s": {p.tls_id: float(p.cycle_s) for p in plan.plans},
        "offsets_s": {p.tls_id: float(p.offset_s) for p in plan.plans},
        "greens_s": {
            p.tls_id: [float(ph.duration_s) for ph in p.phases if ph.kind == "green"]
            for p in plan.plans
        },
    }


def _json_score(score: float) -> "float | None":
    return score if math.isfinite(score) else None


def _ledger_record(
    trial_no: int, evaluation: PlanEvaluation, *, stage: "str | None" = None
) -> dict:
    plan = evaluation.plan
    record = {
        "trial": trial_no,
        "plan_id": plan.plan_id,
        "source": plan.provenance.source,
        "score": _json_score(evaluation.score),
        "valid": evaluation.valid,
        "moe": evaluation.moe or None,
        "offsets": _offsets(plan),
        "trial_dir": evaluation.trial_dir,
    }
    if stage is not None:
        # Coarse trials never pass `stage`: its ABSENCE is what marks a
        # ledger row as coarse, so existing rows never need rewriting when
        # this key is introduced (see the run_corridor_optimization
        # docstring).
        record["stage"] = stage
    return record


def _append_ledger(
    records: list[dict],
    ledger_path: Path,
    evaluation: PlanEvaluation,
    *,
    stage: "str | None" = None,
) -> dict:
    """Build one ledger record, append it to ``records``, and flush it to disk."""
    record = _ledger_record(len(records), evaluation, stage=stage)
    records.append(record)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return record


def _config_echo(
    cfg: CorridorOptimizationConfig,
    *,
    tmc_json: Path,
    resolved_base_sumocfg: Path,
    map_id: "str | None" = None,
) -> dict:
    """Scalar config fields for the result payload (Paths as str).

    ``tmc_json`` is the TMC the run actually used (``cfg.tmc_json``, or the file
    derived from the scenario's routes), not necessarily the configured one.
    """
    return {
        "tmc_json": str(tmc_json),
        "base_sumocfg": str(resolved_base_sumocfg),
        "map_id": map_id,
        "output_dir": str(cfg.output_dir),
        "from_ref": cfg.from_ref,
        "to_ref": cfg.to_ref,
        "tls_ids": list(cfg.tls_ids),
        "baseline_sheet": None if cfg.baseline_sheet is None else str(cfg.baseline_sheet),
        "stall_data": None if cfg.stall_data is None else str(cfg.stall_data),
        "plan_data_root": None if cfg.plan_data_root is None else str(cfg.plan_data_root),
        "plan_time": cfg.plan_time,
        "optimizer": cfg.optimizer,
        "max_trials": cfg.budget.max_trials,
        "patience": cfg.budget.patience,
        "kpi_weights": cfg.kpi.to_dict(),
        "sim_end": cfg.sim_end,
        "seed": cfg.seed,
        "n_workers": cfg.n_workers,
        "vehicle_behavior": cfg.vehicle_behavior,
        "signal_groups_overrides": (
            None
            if cfg.signal_groups_overrides is None
            else str(cfg.signal_groups_overrides)
        ),
        "flow_source": cfg.flow_source,
        "counts_source": cfg.counts_source,
        "seed_from_scenario_programs": cfg.seed_from_scenario_programs,
        "stage_mode": cfg.stage_mode,
        "search_cycle": cfg.search_cycle,
        "cycle_min_s": cfg.cycle_min_s,
        "cycle_max_s": cfg.cycle_max_s,
    }
