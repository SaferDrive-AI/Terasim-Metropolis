"""Background worker for one corridor-optimization run.

Runs in its own process (one per job) and mirrors the signal-evaluation
worker's lifecycle exactly: ``set_running`` -> startup lease renew -> heartbeat
thread -> soft cancellation by raising :class:`LeaseLost` through the progress
callback between evaluation batches -> ``try/finally`` slot release. The pure
function ``run_city_optimization_job`` takes an already-built job store so
tests drive it with an ``InMemoryCityJobStore``; ``run_city_optimization_process``
is the multiprocessing entry point that rebuilds a Redis store inside the child.

Beyond the signal worker, every progress callback also refreshes the record's
``best_score``: the optimizer guarantees the trial that triggered a callback is
already in ``trials.jsonl``, so the callback reads the ledger's last record and
keeps the running minimum of valid scores.

Once the run is recorded as completed and its slots are released, the production
worker process goes on to compute the run's simulated signal metrics
(:mod:`terasim_service.city.sim_metrics_job`) when they are enabled.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Optional

from terasim_service.city.common import (
    DEFAULT_HEARTBEAT_INTERVAL,
    LeaseLost,
    _best_effort,
)
from terasim_service.city.run_outputs import finalize_run_outputs
from terasim_service.city.sim_metrics_job import (
    mark_pending,
    run_sim_metrics_job,
    sim_metrics_enabled,
)
from terasim_service.utils.base import _int_env

logger = logging.getLogger(__name__)

# Imported (not redeclared) so a rename in core cannot silently break the
# service's result endpoint.
from terasim_city.orchestration.corridor_optimization import (  # noqa: E402
    RESULT_FILENAME,
    TRIALS_FILENAME,
)

# Server defaults applied when the request omits a field. sim_end may arrive as
# an explicit null (the API model allows None = "use the server default").
DEFAULT_SIM_END = 1800.0
DEFAULT_SEED = 42
DEFAULT_N_WORKERS = 2

# Deployment-level CNDE/refine knobs. Deliberately env vars, NOT fields on
# OptimizationRequest/DirectOptimizationRequest: quality/fidelity controls are
# ops-controlled, not caller-controllable.
# Read by _build_optimization_config, which both optimize_api.submit_optimization
# (pre-validation, before any slot is reserved or worker launched) and
# optimize_api.run_direct_optimization call SYNCHRONOUSLY at submission time —
# so an unregistered behavior name or a non-integer top_k/max_collisions/
# n_workers raises ValueError there, which those callers turn into a 4xx,
# never a mid-run worker crash.
ENVIRONMENT_VARIABLE_CITY_VEHICLE_BEHAVIOR = "TERASIM_CITY_VEHICLE_BEHAVIOR"
ENVIRONMENT_VARIABLE_CITY_REFINE_TOP_K = "TERASIM_CITY_REFINE_TOP_K"
ENVIRONMENT_VARIABLE_CITY_REFINE_BEHAVIOR = "TERASIM_CITY_REFINE_BEHAVIOR"
ENVIRONMENT_VARIABLE_CITY_REFINE_MAX_COLLISIONS = "TERASIM_CITY_REFINE_MAX_COLLISIONS"
# Refine-stage process count: 1 = sequential in-process (the default), N > 1
# = a spawn pool of N concurrent refine processes, each loading its own copy
# of the refine behavior's runtime (~1.5 GB GPU per "cnde" worker). See
# corridor_optimization._run_refine_stage.
ENVIRONMENT_VARIABLE_CITY_REFINE_N_WORKERS = "TERASIM_CITY_REFINE_N_WORKERS"
DEFAULT_VEHICLE_BEHAVIOR = "sumo_passive"
DEFAULT_REFINE_N_WORKERS = 1

# The one traffic-model choice a caller does make, request by request
# (``traffic_model`` on OptimizationRequest/DirectOptimizationRequest): "sumo",
# the default, runs the deployment's behavior above; "cnde" runs every candidate
# of that one run under the CNDE (Neural NDE) learned driver behavior, which is
# much slower and needs a GPU, so a user asks for it explicitly.
DEFAULT_TRAFFIC_MODEL = "sumo"
# Most "cnde" trial processes one run keeps on the GPU at once: each holds the
# 972 MB checkpoint plus a CUDA context, ~1.5 GB growing to ~6 GB over a long
# run, and 3 of them fit a 16 GB card with headroom. A CNDE request's n_workers
# above it is lowered to it, and the run reserves only that many slots.
ENVIRONMENT_VARIABLE_CITY_CNDE_MAX_WORKERS = "TERASIM_CITY_CNDE_MAX_WORKERS"
DEFAULT_CNDE_MAX_WORKERS = 3
# Most Bayesian trials of a CNDE run (minutes per candidate instead of seconds);
# unset = the request's own max_trials. A scheduled-config entry's budget is then
# shared by its SUMO and its CNDE runs without making the SUMO runs smaller.
ENVIRONMENT_VARIABLE_CITY_CNDE_MAX_TRIALS = "TERASIM_CITY_CNDE_MAX_TRIALS"


def traffic_model_of(vehicle_behavior: str) -> str:
    """The traffic model a run's candidates ran under, named as requests name it."""
    return "cnde" if vehicle_behavior == "cnde" else DEFAULT_TRAFFIC_MODEL


def _deployment_optimization_overrides() -> dict:
    """Read + validate the five deployment-level env vars.

    ``vehicle_behavior`` is checked against the same registry
    ``CorridorOptimizationConfig.__post_init__`` already uses for
    ``refine_vehicle_behavior`` — that field validates itself, so
    it is not re-checked here. ``refine_top_k``/``refine_max_collisions``/
    ``refine_n_workers`` go through ``_int_env`` (same helper
    ``store_config_from_env`` uses), which raises a clear ``ValueError`` on a
    non-integer or out-of-range value instead of silently defaulting.

    No env vars set == today's behavior exactly: "sumo_passive",
    refine_top_k=0, refine_vehicle_behavior=None, refine_max_collisions=None,
    refine_n_workers=1 — the same defaults ``CorridorOptimizationConfig``
    already has.
    """
    from terasim_city.factories import VEHICLE_BEHAVIORS

    vehicle_behavior = (
        os.environ.get(ENVIRONMENT_VARIABLE_CITY_VEHICLE_BEHAVIOR)
        or DEFAULT_VEHICLE_BEHAVIOR
    )
    if vehicle_behavior not in VEHICLE_BEHAVIORS:
        raise ValueError(
            f"{ENVIRONMENT_VARIABLE_CITY_VEHICLE_BEHAVIOR}={vehicle_behavior!r} "
            "is not a registered vehicle behavior; registered behaviors: "
            f"{sorted(VEHICLE_BEHAVIORS)}"
        )
    return {
        "vehicle_behavior": vehicle_behavior,
        "refine_top_k": _int_env(
            ENVIRONMENT_VARIABLE_CITY_REFINE_TOP_K, 0, minimum=0
        ),
        "refine_vehicle_behavior": (
            os.environ.get(ENVIRONMENT_VARIABLE_CITY_REFINE_BEHAVIOR) or None
        ),
        "refine_max_collisions": _int_env(
            ENVIRONMENT_VARIABLE_CITY_REFINE_MAX_COLLISIONS, None, minimum=0
        ),
        "refine_n_workers": _int_env(
            ENVIRONMENT_VARIABLE_CITY_REFINE_N_WORKERS,
            DEFAULT_REFINE_N_WORKERS,
            minimum=1,
        ),
    }


def _cnde_request_overrides(behavior: dict, base_sumocfg) -> dict:
    """``behavior`` (the deployment's knobs, ``n_workers``, ``budget``) for a CNDE run.

    Every candidate runs under "cnde", never silently under SUMO instead: a deployment
    without the CNDE runtime or its checkpoint, or a named map (``base_sumocfg``) that
    cannot give CNDE its map artifacts, raises ValueError naming what is missing.
    ``n_workers`` is lowered to TERASIM_CITY_CNDE_MAX_WORKERS (that many trials share
    the GPU at once), the budget's ``max_trials`` to TERASIM_CITY_CNDE_MAX_TRIALS when
    that is set, and a deployment refine stage under "cnde" is dropped: it would only
    re-simulate the best plans under the behavior they were scored with.
    """
    from terasim_city.factories.cnde_behavior import missing_cnde_runtime
    from terasim_city.maps.cnde_artifacts import missing_cnde_map_artifacts

    missing = missing_cnde_runtime()
    if base_sumocfg:
        missing += missing_cnde_map_artifacts(base_sumocfg)
    if missing:
        raise ValueError(
            "traffic_model='cnde' is not available on this deployment: "
            + "; ".join(missing)
        )
    cap = _int_env(
        ENVIRONMENT_VARIABLE_CITY_CNDE_MAX_WORKERS, DEFAULT_CNDE_MAX_WORKERS, minimum=1
    )
    max_trials = _int_env(ENVIRONMENT_VARIABLE_CITY_CNDE_MAX_TRIALS, None, minimum=1)
    behavior = dict(
        behavior,
        vehicle_behavior="cnde",
        n_workers=min(behavior["n_workers"], cap),
        cnde_max_workers=cap,
    )
    if max_trials is not None and max_trials < behavior["budget"].max_trials:
        behavior["budget"] = replace(behavior["budget"], max_trials=max_trials)
    if behavior["refine_vehicle_behavior"] == "cnde":
        behavior["refine_vehicle_behavior"] = None
    return behavior


def _build_optimization_config(request: dict, output_dir: Path):
    """Map an HTTP request dict to a CorridorOptimizationConfig.

    Quality policy is not caller-controlled: optimization runs disable
    teleports and score congestion directly (see corridor_optimization), so
    there are no per-request validity thresholds. The corridor sub-dict
    accepts both the pydantic field names (``from_ref``/``to_ref``) and the
    wire aliases (``from``/``to``) so
    scheduler-built requests need no extra mapping step.

    ``vehicle_behavior``/``refine_top_k``/``refine_vehicle_behavior``/
    ``refine_max_collisions``/``refine_n_workers`` come from deployment env
    vars, never the request dict (see ``_deployment_optimization_overrides``).
    The request's ``traffic_model`` is the one exception: "cnde" runs every
    candidate under "cnde" with at most TERASIM_CITY_CNDE_MAX_WORKERS workers
    and TERASIM_CITY_CNDE_MAX_TRIALS trials (``_cnde_request_overrides``), and
    "sumo" or none keeps the deployment's behavior exactly.

    The stage / cycle / scenario switches (``stage_mode``, ``search_cycle``,
    ``cycle_min_s``, ``cycle_max_s``, ``flow_source``, ``counts_source``,
    ``seed_from_scenario_programs``) are forwarded only when the request sets
    them (not None); anything unset keeps the config's own default, and the
    config validates what was set (``ValueError``, which the callers turn into
    a 4xx).
    """
    from terasim_city.evaluation.moe import KpiConfig
    from terasim_city.optimize.base import Budget
    from terasim_city.orchestration.corridor_optimization import (
        CorridorOptimizationConfig,
    )

    behavior = {
        "n_workers": int(request.get("n_workers", DEFAULT_N_WORKERS)),
        "budget": Budget(
            max_trials=int(request.get("max_trials", 20)),
            patience=int(request.get("patience", 8)),
        ),
        **_deployment_optimization_overrides(),
    }
    if request.get("traffic_model") == "cnde":
        behavior = _cnde_request_overrides(behavior, request.get("base_sumocfg"))
    corridor = request.get("corridor") or {}
    kpi_weights = request.get("kpi_weights")
    baseline_sheet = request.get("baseline_sheet")
    stall_data = request.get("stall_data")
    sim_end = request.get("sim_end")
    # Uploaded signal plans: when enabled (default) the orchestration checks
    # the PlanStore on the asset volume; corridors it does not cover fall back
    # to the static seed ladder, so this is safe without any uploads.
    plan_data_root = None
    if request.get("use_stored_plans", True):
        env_root = os.environ.get("TERASIM_CITY_DATA_ROOT")
        if env_root:
            plan_data_root = Path(env_root)
    extra = {
        key: request[key]
        for key in (
            "stage_mode", "search_cycle", "cycle_min_s", "cycle_max_s",
            "flow_source", "counts_source", "seed_from_scenario_programs",
        )
        if request.get(key) is not None
    }
    return CorridorOptimizationConfig(
        tmc_json=Path(request["tmc_json"]),
        base_sumocfg=(
            Path(request["base_sumocfg"]) if request.get("base_sumocfg") else None
        ),
        output_dir=Path(output_dir),
        from_ref=corridor.get("from_ref", corridor.get("from")),
        to_ref=corridor.get("to_ref", corridor.get("to")),
        tls_ids=tuple(corridor.get("tls_ids") or ()),
        baseline_sheet=None if baseline_sheet is None else Path(baseline_sheet),
        stall_data=None if stall_data is None else Path(stall_data),
        plan_data_root=plan_data_root,
        plan_time=request.get("plan_time"),
        optimizer=request.get("optimizer", "auto"),
        kpi=KpiConfig.from_dict(kpi_weights) if kpi_weights else KpiConfig(),
        sim_end=DEFAULT_SIM_END if sim_end is None else float(sim_end),
        seed=int(request.get("seed", DEFAULT_SEED)),
        **behavior,
        **extra,
    )


def _last_ledger_score(ledger_path: Path) -> Optional[float]:
    """Score of the last trials.jsonl record, or None (invalid trial / no file).

    Best-effort by design: the ledger is an observability side channel; a read
    hiccup must never fail the run.
    """
    try:
        text = ledger_path.read_text(encoding="utf-8").rstrip()
        if not text:
            return None
        record = json.loads(text.rsplit("\n", 1)[-1])
        score = record.get("score")
        if record.get("valid") and score is not None and math.isfinite(float(score)):
            return float(score)
    except Exception as exc:
        logger.warning("could not read trial ledger %s: %s", ledger_path, exc)
    return None


def _exported_summary(result: dict) -> dict:
    """``{"plan_id", "score"}`` for the plan corridor_optimization actually EXPORTED.

    Mirrors optimize_api._direct_kpis's branch (see corridor_optimization's
    module docstring): reading
    ``result["best"]`` unconditionally would report the COARSE plan's
    plan_id/score as ``best_score`` even when the NEMA export (and thus the
    plan a caller actually gets) is the refine winner.

    ``export_source == "refine"``: the refine winner, looked up by
    ``winner_plan_id`` among the refine block's baseline + evaluations
    entries (the winner can legitimately BE the refine-refined baseline).
    A miss (``winner_plan_id`` not found among those entries) is a contract
    violation, not a fallback opportunity — this raises ``ValueError``,
    mirroring ``optimize_api._direct_kpis``'s identical failure mode, rather
    than silently reporting the coarse ``result["best"]`` for a
    ``winner_plan_id`` that the refine stage itself does not recognize.
    ``export_source == "coarse"`` or absent: ``result["best"]``, unchanged —
    a sumo_passive deployment with no refine env vars set never sets
    ``export_source`` at all, so this reports exactly the coarse winner.
    """
    if result.get("export_source") == "refine":
        refine = result.get("refine") or {}
        winner_plan_id = refine.get("winner_plan_id")
        candidates = [refine.get("baseline"), *(refine.get("evaluations") or ())]
        for entry in candidates:
            if entry is not None and entry.get("plan_id") == winner_plan_id:
                return {"plan_id": entry.get("plan_id"), "score": entry.get("score")}
        raise ValueError(
            f"refine.winner_plan_id={winner_plan_id!r} not found among "
            "the refine block's baseline/evaluations entries"
        )
    best = result.get("best") or {}
    return {"plan_id": best.get("plan_id"), "score": best.get("score")}


def run_city_optimization_job(
    store,
    request: dict,
    run_id: str,
    output_dir,
    *,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    opt_fn=None,
    map_acquirer=None,
    sim_metrics_runner=None,
) -> None:
    """Run one corridor optimization, keeping status + lease + best_score fresh.

    ``store`` is an already-built CityJobStore. ``opt_fn`` defaults to
    terasim-city's ``run_corridor_optimization`` (injectable for tests). When
    ``base_sumocfg`` is absent, a covering cached map is selected; if none
    exists, the production acquirer downloads, converts, and persists OSM.

    ``sim_metrics_runner(run_id, output_dir)``, when given and the metrics are
    enabled (``TERASIM_CITY_SIM_METRICS``), runs after the run has been recorded as
    completed and its slots released; the metrics are marked pending before that.
    """
    output_dir = Path(output_dir)
    if opt_fn is None:
        from terasim_city.orchestration.corridor_optimization import (
            run_corridor_optimization,
        )

        opt_fn = run_corridor_optimization

    lease_lost = threading.Event()
    stop = threading.Event()
    heartbeat = None
    ledger_path = output_dir / TRIALS_FILENAME
    best_score = math.inf
    metrics_after = False

    def _heartbeat() -> None:
        # stop.wait() returns True the moment the job ends, so the thread exits
        # promptly (no lingering sleep). On renew failure/error, flag the loss.
        while not stop.wait(heartbeat_interval):
            try:
                ok = store.renew_slot(run_id)
            except Exception as exc:  # Redis hiccup etc. — treat as lost, don't die silently
                logger.warning("heartbeat renew error for %s: %s", run_id, exc)
                ok = False
            if not ok:
                lease_lost.set()
                return

    def _progress(done: int, total: int, plan_id) -> None:
        nonlocal best_score
        if lease_lost.is_set():
            raise LeaseLost(run_id)
        store.set_progress(run_id, done, total, plan_id)
        # The optimizer writes the ledger record BEFORE firing its callback,
        # so the last line is exactly the trial this callback reports.
        score = _last_ledger_score(ledger_path)
        if score is not None and score < best_score:
            best_score = score
            _best_effort(store.set_best_score, run_id, score)

    # The whole lifecycle is inside try/finally so the slots are always
    # released — even if set_running or the startup renew raises, which happens
    # before the heartbeat exists. Cleanup is best-effort: if Redis is truly
    # down, set_failed/release_slot may also fail, and the lease TTL is the
    # final backstop.
    try:
        store.set_running(run_id)
        # Verify + refresh the lease before heavy work: a slow spawn could let
        # the parent's lease lapse before the first heartbeat would fire.
        if not store.renew_slot(run_id):
            _best_effort(store.set_failed, run_id, "lease lost before worker start")
            return
        heartbeat = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat.start()
        concrete_request = dict(request)
        if not concrete_request.get("base_sumocfg"):
            if map_acquirer is None:
                from terasim_service.city.map_acquisition import MapAcquirer

                map_acquirer = MapAcquirer.from_environment()
            concrete_request["base_sumocfg"] = str(
                Path(map_acquirer(Path(concrete_request["tmc_json"]))).resolve()
            )
        config = _build_optimization_config(concrete_request, output_dir)
        result = opt_fn(config, progress_callback=_progress)
        # The result file (when a stand-in optimizer wrote none) and the copy of the
        # baseline plan as NEMA timing (run_outputs) - before the run reads completed.
        finalize_run_outputs(output_dir, result)
        if lease_lost.is_set():
            logger.warning(
                "run %s finished but its lease had lapsed; reporting completed "
                "(the result is valid; this was a concurrency-control blip)",
                run_id,
            )
        summary = _exported_summary(result)
        final_score = summary.get("score")
        if final_score is not None and math.isfinite(float(final_score)):
            _best_effort(store.set_best_score, run_id, float(final_score))
        result_path = output_dir / RESULT_FILENAME
        want_metrics = sim_metrics_runner is not None and sim_metrics_enabled()
        if want_metrics:
            # Pending before the run reads completed: a reader never sees a
            # completed run whose metrics look "never computed".
            mark_pending(output_dir)
        store.set_completed(run_id, str(result_path), summary)
        metrics_after = want_metrics
    except LeaseLost:
        _best_effort(
            store.set_failed, run_id, "lease lost during run; stopped after current batch"
        )
    except Exception as exc:
        logger.exception("city optimization job %s failed", run_id)
        _best_effort(store.set_failed, run_id, str(exc))
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=5)
        _best_effort(store.release_slot, run_id)
    if metrics_after:
        try:
            sim_metrics_runner(run_id, output_dir)
        except Exception:  # noqa: BLE001 - the run itself is done and recorded
            logger.exception("simulated signal metrics of run %s failed to start", run_id)


def run_city_optimization_process(
    run_id: str, request: dict, output_dir: str, store_config: dict
) -> None:
    """multiprocessing entry point (module-level so macOS spawn can import it).

    Builds a fresh Redis store inside the child from ``store_config`` — the SAME
    config (redis connection + max_concurrency + lease_ttl) the parent used, so
    the lease semantics match across processes. Only picklable plain data
    crosses the process boundary.
    """
    import redis

    from terasim_service.city.job_store import RedisCityJobStore

    store = RedisCityJobStore(
        client=redis.Redis(**store_config["redis"]),
        max_concurrency=store_config["max_concurrency"],
        lease_ttl=store_config["lease_ttl"],
        key_prefix=store_config.get("key_prefix", "city_eval"),
        emergency_slots=store_config.get("emergency_slots", 2),
    )
    run_city_optimization_job(
        store,
        request,
        run_id,
        output_dir,
        sim_metrics_runner=lambda rid, out: run_sim_metrics_job(store, rid, Path(out)),
    )
