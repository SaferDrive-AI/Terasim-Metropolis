"""v1 HTTP endpoints for city signal-plan optimization.

The VSS-facing endpoint is synchronous and deliberately narrow::

    POST /v1/city/optimize -> 200 {nema_plan, kpis}
                              (409 + reason when the best plan has no NEMA export)

VSS resolves natural language and intersection names against the TMC data it
sent. This service receives only structured ids/coordinates, runs the full
optimization during the request and returns the NEMA plan plus KPI comparison.
The run is kept and recorded like an asynchronous one (origin ``direct``, under
``output_root/<run_id>/``), so the run listing and the per-run endpoints below
serve it too; the response body does not change.

The older scheduler/operations surface remains submit-and-poll:

    POST /v1/city/optimizations                 -> 202 {run_id}
    GET  /v1/city/optimizations                 (every run of every origin, newest
                                                 first; see runs_api)
    GET  /v1/city/optimizations/{id}/status     (+ best_score)
    GET  /v1/city/optimizations/{id}/result     (corridor_optimization_result.json)
    GET  /v1/city/optimizations/{id}/nema       (nema_plan.json; 409 until completed,
                                                 or with the reason if the run exported none)
    GET  /v1/city/optimizations/{id}/baseline   (the baseline plan as NEMA timing;
                                                 see runs_api)
    GET  /v1/city/optimizations/{id}/sim_metrics (simulated signal metrics, computed
                                                 in the background after every run)
    GET  /v1/city/optimizations/{id}/report.pdf (the run's timing report, rendered on
                                                 first request)

The console's audit log (operator feedback on plans, plan views and downloads, console
sessions) is on the same router; see audit_api:

    POST /v1/city/audit/events                  (one event, stamped by the service)
    GET  /v1/city/audit/events | events.csv     (newest first, filters; CSV export)
    GET  /v1/city/audit/summary                 (usage and feedback of the last N days)

Same injectable seams as the signal router (``store`` + ``launcher``) and the
same path-safety rules — the private validation helpers are imported from
``common`` rather than copied. One
difference: an optimization run spawns ``n_workers`` SUMO processes, so it
reserves ``n_workers`` concurrency slots up front (``try_acquire_slots``).

A request names its demand in EXACTLY one of two ways:
  * ``corridor_id`` (primary) — the key of a server-side snapshot queue fed by
    POST /v1/city/snapshots: the recently buffered TMC snapshots are
    aggregated into a server-generated demand file. A matching scheduled
    corridor config entry is OPTIONAL — when present it supplies request
    defaults (explicit request fields win). If no map is supplied or
    configured, the worker selects a covering cached map and downloads,
    converts, and caches an OSM extract when necessary. With
    ``counts_source="scenario_routes"`` (on the request or its config entry)
    the turning counts come from the scenario's own route files inside the
    worker, so no snapshots are needed: the demand file is then a placeholder
    carrying only the corridor id and the current time.
  * ``tmc_json`` (secondary/testing) — an explicit demand file (validated
    against the input root).

``corridor_id`` names the demand queue; the optional ``corridor``
(from/to|tls_ids) field names the geometry. They may be combined — an
explicit ``corridor`` always wins over a config entry's corridor.

With ``TERASIM_CITY_OSM_DOWNLOAD=0`` (maps shipped as files) nothing is downloaded:
a request without ``base_sumocfg`` must be covered by a stored map, checked before
capacity is reserved, else 422 naming the setting (see map_acquisition).

Both POSTs take an optional ``traffic_model``: ``"sumo"`` (the default) simulates
every candidate with the deployment's configured vehicle behavior, ``"cnde"`` with
the CNDE (Neural NDE) learned driver behavior, for that one run only. A CNDE run is
much slower and needs a GPU; at most ``TERASIM_CITY_CNDE_MAX_WORKERS`` of its trials
run at once (its ``n_workers`` and reserved slots are lowered to that), at most
``TERASIM_CITY_CNDE_MAX_TRIALS`` of them when that is set, and a deployment without
the CNDE runtime or its checkpoint answers 422 rather than running SUMO instead.
Such a run is recorded, and answered, with ``traffic_model: "cnde"``.

An optional inline ``incident`` (stalled-vehicle geometries) expedites the run:
the stalls are written next to the run's inputs, forwarded to the worker as
``stall_data`` with ``trigger="incident"``, and slot acquisition uses the
reserved emergency headroom (``emergency=True``). Server-generated input files
live under ``output_root/<run_id>/inputs/`` and are exempt from the input-root
confinement; user-supplied paths keep the existing validation.
"""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Mapping, Optional, Union

from fastapi import APIRouter, HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from terasim_city.plans.baseline_sheet import TimingSheetCoverageError
from terasim_service.city.job_store import (
    COMPLETED,
    FAILED,
    ORIGIN_API,
    ORIGIN_DIRECT,
    ORIGIN_SCHEDULED,
    RUNNING,
    SUBMITTED,
    CityJobStore,
    RedisCityJobStore,
)
from terasim_service.city.common import (
    _best_effort,
    _get_record_or_404,
    _is_within,
    _iso,
    _validate_input_file,
    _validate_map_references,
    store_config_from_env,
)
from terasim_service.city.worker_optimize import (
    DEFAULT_TRAFFIC_MODEL,
    _build_optimization_config,
    _exported_summary,
    run_city_optimization_process,
    traffic_model_of,
)
from terasim_service.city.run_outputs import finalize_run_outputs
from terasim_service.city.audit_api import register_audit_routes
from terasim_service.city.runs_api import register_run_routes
from terasim_service.city.sim_metrics_job import (
    make_sim_metrics_process_launcher,
    mark_pending,
    sim_metrics_enabled,
)
from terasim_service.city.scheduler import SCHEDULER_ONLY_KEYS, SubmissionRejected
from terasim_service.constants import (
    ENVIRONMENT_VARIABLE_CITY_EMERGENCY_SLOTS,
    ENVIRONMENT_VARIABLE_CITY_INPUT_ROOT,
    ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY,
    ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT,
)

logger = logging.getLogger(__name__)

RESULT_FILENAME = "corridor_optimization_result.json"
NEMA_FILENAME = "nema_plan.json"
AGGREGATED_FILENAME = "aggregated_tmc.json"
STALLS_FILENAME = "stalls.json"
INCIDENT_TRIGGER = "incident"
DEFAULT_WINDOW_S = 3600.0
DIRECT_HEARTBEAT_INTERVAL_S = 30.0


class CorridorRef(BaseModel):
    """Corridor selection: explicit TLS ids, or a from/to intersection pair.

    ``from``/``to`` are the wire names (Python keywords, hence the aliases);
    the field names ``from_ref``/``to_ref`` are also accepted for direct
    (in-process) callers.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_ref: Optional[str] = Field(None, alias="from")
    to_ref: Optional[str] = Field(None, alias="to")
    tls_ids: list[str] = Field(default_factory=list)


class GeoIntersectionRef(BaseModel):
    """A TMC intersection the caller resolved; the service performs no NLP/geocoding."""

    model_config = ConfigDict(extra="forbid")

    intersection_id: str = Field(..., min_length=1)
    name: Optional[str] = None
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)


class GeoCorridorRef(BaseModel):
    """Two explicit, separately-addressable corridor endpoints."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_ref: GeoIntersectionRef = Field(..., alias="from")
    to_ref: GeoIntersectionRef = Field(..., alias="to")


class OptimizationCost(BaseModel):
    """Configurable weighted cost exposed to synchronous callers."""

    model_config = ConfigDict(extra="forbid")

    weights: dict[str, float] = Field(
        default_factory=lambda: {"control_delay": 0.7, "stops": 0.3}
    )

    @field_validator("weights", mode="before")
    @classmethod
    def _require_finite_json_numbers(cls, value):
        """Reject Pydantic's bool/string coercions and non-finite weights."""

        if not isinstance(value, Mapping):
            raise ValueError("cost.weights must be a JSON object")
        for name, weight in value.items():
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ValueError(
                    f"cost weight {name!r} must be a numeric JSON value"
                )
            if not math.isfinite(float(weight)):
                raise ValueError(f"cost weight {name!r} must be finite")
        return value

    @model_validator(mode="after")
    def _validate_weights(self) -> "OptimizationCost":
        from terasim_city.evaluation.moe import KpiConfig

        KpiConfig.from_dict(self.weights)
        return self


class DirectOptimizationRequest(BaseModel):
    """Minimal synchronous optimization contract.

    Natural-language interpretation and name-to-coordinate resolution happen
    in the caller.  This model intentionally does not expose internal BO
    controls such as optimizer choice or trial budget. ``traffic_model`` picks
    what every candidate plan is simulated with: ``"sumo"`` (the default, the
    deployment's configured driver model) or ``"cnde"`` (the CNDE learned driver
    behavior: much slower, GPU only, 422 where it is not installed).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["intersection", "corridor"]
    corridor_id: str = Field(..., min_length=1)
    intersection: Optional[GeoIntersectionRef] = None
    corridor: Optional[GeoCorridorRef] = None
    cost: OptimizationCost = Field(default_factory=OptimizationCost)
    #: None means "sumo" unless the corridor's scheduled-config entry names one.
    traffic_model: Optional[Literal["sumo", "cnde"]] = None

    @model_validator(mode="after")
    def _target_matches_type(self) -> "DirectOptimizationRequest":
        if self.type == "intersection":
            if self.intersection is None or self.corridor is not None:
                raise ValueError(
                    "type='intersection' requires intersection and forbids corridor"
                )
        elif self.corridor is None or self.intersection is not None:
            raise ValueError("type='corridor' requires corridor and forbids intersection")
        return self

    def target_ids(self) -> list[str]:
        """The intersection ids this request names, ``from`` before ``to``."""
        if self.type == "intersection":
            assert self.intersection is not None
            return [self.intersection.intersection_id]
        assert self.corridor is not None
        return _unique(
            [self.corridor.from_ref.intersection_id, self.corridor.to_ref.intersection_id]
        )

    def as_internal_request(self) -> "OptimizationRequest":
        if self.type == "intersection":
            assert self.intersection is not None
            start = end = self.intersection.intersection_id
        else:
            assert self.corridor is not None
            start = self.corridor.from_ref.intersection_id
            end = self.corridor.to_ref.intersection_id
        return OptimizationRequest(
            corridor_id=self.corridor_id,
            corridor=CorridorRef(from_ref=start, to_ref=end),
            kpi_weights=dict(self.cost.weights),
            traffic_model=self.traffic_model,
        )


class DirectCostResult(BaseModel):
    """One side of the baseline-versus-optimized KPI comparison."""

    model_config = ConfigDict(extra="forbid")

    cost: float
    metrics: dict[str, object]
    #: This side's per-TLS ``{"cycle_s": {tls: s}, "offsets_s": {...}, "greens_s":
    #: {...}}``. A result that carries none leaves the key out of the payload, so
    #: such a response is exactly what it was before timing existed.
    timing: Optional[dict[str, object]] = None

    @model_serializer(mode="wrap")
    def _omit_missing_timing(self, handler: SerializerFunctionWrapHandler):
        payload = handler(self)
        if payload.get("timing") is None:
            payload.pop("timing", None)
        return payload


class DirectKpiResponse(BaseModel):
    """Stable KPI response contract of the synchronous optimization."""

    model_config = ConfigDict(extra="forbid")

    cost_weights: dict[str, float]
    baseline: DirectCostResult
    optimized: DirectCostResult


class DirectOptimizationResponse(BaseModel):
    """Complete synchronous response; no run id or internal path is exposed.

    ``unserved_movements`` (``{tls_id: [movement, ...]}``) names the vehicle
    movements the returned plan never lets move; it is present only when there
    are some. ``config`` (``{"flow_source", "counts_source"}``) says where the
    simulated demand came from; it is present only when either is the scenario's
    own routes. ``traffic_model`` (``"cnde"``) says every candidate was simulated
    with the CNDE learned driver behavior; it is present only for such a run. A
    default run answers exactly as before.
    """

    model_config = ConfigDict(extra="forbid")

    nema_plan: dict[str, object]
    kpis: DirectKpiResponse
    unserved_movements: Optional[dict[str, list[str]]] = None
    config: Optional[dict[str, str]] = None
    traffic_model: Optional[Literal["sumo", "cnde"]] = None

    @model_serializer(mode="wrap")
    def _omit_absent_optionals(self, handler: SerializerFunctionWrapHandler):
        payload = handler(self)
        for key in ("unserved_movements", "config", "traffic_model"):
            if payload.get(key) is None:
                payload.pop(key, None)
        return payload


class StallItem(BaseModel):
    """One stalled-vehicle geometry as reported by the video analytics."""

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)


class IncidentPayload(BaseModel):
    """Inline incident report: at least one stalled vehicle."""

    model_config = ConfigDict(extra="forbid")

    stalled_vehicles: list[StallItem] = Field(..., min_length=1)


class OptimizationRequest(BaseModel):
    """One corridor-optimization request. Unknown fields are rejected.

    Demand comes from EXACTLY one of ``corridor_id`` (primary: server-side
    aggregation of buffered snapshots) or ``tmc_json`` (secondary/testing:
    an explicit file, ``base_sumocfg`` required).

    ``corridor_id`` is the snapshot QUEUE key — it must match the
    ``corridor_id`` field carried by the TMC payloads pushed to
    POST /v1/city/snapshots. A scheduled-config entry for the id is optional:
    when present it supplies defaults (explicit request fields win, and
    ``base_sumocfg`` may be omitted). When it is absent, the worker selects a
    covering cached map or downloads and persists an OSM extract.

    ``corridor`` names the GEOMETRY (from/to intersections or tls_ids) and
    may be combined with either demand source; when given alongside
    ``corridor_id`` it wins over any config entry's corridor.

    An optional inline ``incident`` expedites the run via the reserved
    emergency concurrency headroom.

    ``sim_end=None`` means "use the server default" (1800 s, applied by the
    worker); when given it must be a realistic horizon (30..7200 s).

    ``stage_mode`` / ``search_cycle`` / ``cycle_min_s`` / ``cycle_max_s`` /
    ``flow_source`` / ``counts_source`` / ``seed_from_scenario_programs`` go
    straight to the optimization config, which validates them against each
    other (e.g. min < max). ``None`` means "server default", so a
    scheduled-config entry can supply them. A corridor_id request with
    ``counts_source="scenario_routes"`` needs no buffered snapshots, but then
    ``base_sumocfg`` and the corridor's ``tls_ids`` are required.

    ``traffic_model`` (``None`` = ``"sumo"``, the deployment's configured driver
    model) may be ``"cnde"``: every candidate of this run is then simulated with
    the CNDE learned driver behavior, much slower and GPU only, and ``n_workers``
    is capped at the deployment's CNDE worker limit (422 where the CNDE runtime
    is not installed).
    """

    model_config = ConfigDict(extra="forbid")

    tmc_json: Optional[str] = None
    corridor_id: Optional[str] = None
    base_sumocfg: Optional[str] = None
    corridor: Optional[CorridorRef] = None
    baseline_sheet: Optional[str] = None
    incident: Optional[IncidentPayload] = None
    optimizer: Literal[
        "auto", "exhaustive", "bayes_offsets", "bayes_splits", "bayes_corridor"
    ] = "auto"
    max_trials: int = Field(20, ge=1, le=200)
    patience: int = 8
    kpi_weights: Optional[dict[str, float]] = None
    #: Use uploaded signal plans (PlanStore on $TERASIM_CITY_DATA_ROOT) as the
    #: optimization baseline when they cover the corridor. True by default —
    #: with no uploaded plans the run silently falls back to the static seed
    #: ladder, so this is safe to leave on.
    use_stored_plans: bool = True
    #: ISO timestamp picking each intersection's time-of-day pattern; default
    #: is the TMC data's own timestamp_start.
    plan_time: Optional[str] = None
    sim_end: Optional[float] = Field(None, ge=30, le=7200)
    n_workers: int = Field(2, ge=1, le=8)
    seed: int = Field(42, ge=0)
    #: Seed structure / search switches. None means "server default" so that a
    #: scheduled-config entry can supply them (see _resolve_corridor_request).
    stage_mode: Optional[Literal["two_stage", "per_approach"]] = None
    search_cycle: Optional[bool] = None
    cycle_min_s: Optional[float] = Field(None, ge=20, le=600)
    cycle_max_s: Optional[float] = Field(None, ge=20, le=600)
    flow_source: Optional[Literal["corridor_graph", "scenario_routes"]] = None
    counts_source: Optional[Literal["tmc", "scenario_routes"]] = None
    seed_from_scenario_programs: Optional[bool] = None
    #: The traffic model every candidate is simulated with (see the docstring).
    traffic_model: Optional[Literal["sumo", "cnde"]] = None

    @model_validator(mode="after")
    def _exactly_one_demand_source(self) -> "OptimizationRequest":
        """Enforce exactly one DEMAND source (tmc_json xor corridor_id).

        ``corridor`` is geometry, not demand — it is legal alongside either
        source. In corridor_id mode ``base_sumocfg`` stays optional here
        because a scheduled-config entry may supply it (checked at submit
        time).
        """
        if (self.tmc_json is None) == (self.corridor_id is None):
            raise ValueError("exactly one of tmc_json / corridor_id must be provided")
        # base_sumocfg is optional: when omitted the worker auto-selects the
        # stored map covering the TMC intersections.
        return self


def _unique(values) -> list[str]:
    """``values`` as strings, without ``None`` and duplicates, in their first order."""
    seen: list[str] = []
    for value in values:
        if value is not None and str(value) not in seen:
            seen.append(str(value))
    return seen


def _requested_intersection_ids(corridor: Optional[CorridorRef]) -> list[str]:
    """The intersections a request names: its ``from``/``to`` refs, else its TLS ids."""
    if corridor is None:
        return []
    if corridor.from_ref is not None or corridor.to_ref is not None:
        return _unique([corridor.from_ref, corridor.to_ref])
    return _unique(corridor.tls_ids)


def _tmc_corridor_id(tmc_json: Optional[str]) -> Optional[str]:
    """The ``corridor_id`` a TMC demand file carries, or None (best effort, never raises)."""
    if not tmc_json:
        return None
    try:
        value = json.loads(Path(tmc_json).read_text(encoding="utf-8")).get("corridor_id")
    except Exception:  # noqa: BLE001 - the record is informative; the run validates the file
        return None
    return value if isinstance(value, str) and value else None


def make_optimize_process_launcher(store_config: dict) -> Callable[[str, dict, str], None]:
    """Production launcher: spawn one optimization worker built from store_config."""

    def launch(run_id: str, request: dict, output_dir: str) -> None:
        process = multiprocessing.Process(
            target=run_city_optimization_process,
            args=(run_id, request, output_dir, store_config),
            daemon=False,
        )
        process.start()

    return launch


def _resolve_corridor_request(
    request: OptimizationRequest,
    inputs_dir: Path,
    buffer,
    corridors: Optional[Mapping[str, dict]],
    window_s: float,
) -> OptimizationRequest:
    """Turn a corridor_id request into a concrete one (server-generated demand).

    ``corridor_id`` keys the snapshot queue; aggregates the recently buffered
    snapshots into ``<inputs_dir>/aggregated_tmc.json``. When the effective
    ``counts_source`` (the request's own, else the config entry's) is
    ``scenario_routes``, the counts come from the scenario's own routes inside
    the worker instead: no snapshots are read and that file is a placeholder.
    A scheduled-config entry for the id is optional: when present its defaults
    merge under the request (explicitly-set, non-None request fields win —
    including an explicit ``corridor`` geometry). A missing ``base_sumocfg`` is
    resolved by the worker's cached-map/online-OSM acquisition layer. 400
    merged request invalid; 400 no buffer wired and 409 empty snapshot window
    (the last two only when the demand comes from snapshots).
    """
    corridor_id = request.corridor_id
    cfg = corridors.get(corridor_id) if corridors else None
    # Where the demand comes from follows the counts source the MERGED request
    # will carry: the request's own, else the entry's, else the default "tmc".
    effective_counts = request.counts_source or (cfg or {}).get("counts_source") or "tmc"
    if effective_counts == "scenario_routes":
        # The turning counts are derived from the scenario's own route files in
        # the worker, so the TMC file is a placeholder: the worker reads only its
        # corridor id and timestamp (the time of day that picks stored-plan
        # patterns); the rest is well-formed filler.
        now = datetime.now(timezone.utc).replace(microsecond=0)
        demand = {
            "corridor_id": corridor_id,
            "timestamp_start": now.isoformat(),
            "observation_interval": "1h",
            "intersections": [
                {"intersection_id": "placeholder", "lat": 0.0, "lon": 0.0, "counts": {}}
            ],
        }
    else:
        if buffer is None:
            raise HTTPException(
                status_code=400,
                detail="corridor_id mode requires the snapshot buffer, which is "
                "not wired on this deployment",
            )
        snapshots = buffer.window(corridor_id, window_s)
        if not snapshots:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"no recent traffic data buffered for corridor "
                    f"{corridor_id!r} (window {window_s:.0f} s)"
                ),
            )

        from terasim_service.city.aggregate import aggregate_snapshots

        try:
            demand = aggregate_snapshots(snapshots)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"cannot aggregate buffered TMC for {corridor_id!r}: {exc}",
            ) from exc
    inputs_dir.mkdir(parents=True, exist_ok=True)
    aggregated_path = inputs_dir / AGGREGATED_FILENAME
    aggregated_path.write_text(json.dumps(demand, indent=2) + "\n", encoding="utf-8")

    # Config entry (when present) supplies the defaults; explicitly-set,
    # non-None request fields win — so an explicit `corridor` geometry beats
    # the config entry's. corridor_id/incident/tmc_json never merge as plain
    # fields.
    merged = (
        {
            key: value
            for key, value in cfg.items()
            if key != "corridor_id" and key not in SCHEDULER_ONLY_KEYS
        }
        if cfg is not None
        else {}
    )
    explicit = request.model_dump(exclude_unset=True, exclude_none=True)
    for key in ("corridor_id", "incident", "tmc_json"):
        explicit.pop(key, None)
    merged.update(explicit)
    merged["tmc_json"] = str(aggregated_path)
    # base_sumocfg is optional: the worker auto-selects a covering map when
    # it is not present.
    merged["incident"] = request.incident
    try:
        return OptimizationRequest.model_validate(merged)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"corridor config for {corridor_id!r} yields an invalid request: {exc}",
        ) from exc


def _reject_unreachable_slot_count(
    store: CityJobStore, n_workers: int, *, emergency: bool = False
) -> None:
    """422 when ``n_workers`` exceeds what this deployment can EVER grant.

    A request needing more worker slots than the configured maximum concurrency
    can never start, whatever else is running, so it is a configuration error
    (422 naming the setting), not the transient "busy, retry later" (429) of a
    request that fits the cap while other runs hold the slots. Incident runs may
    also use the emergency headroom.
    """
    cap = getattr(store, "max_concurrency", None)
    if cap is None:
        return
    if not emergency:
        if n_workers > cap:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"n_workers={n_workers} exceeds this deployment's maximum "
                    f"concurrency of {cap} worker slot(s) "
                    f"({ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY}={cap}), so the run "
                    f"can never start: lower n_workers to {cap} or less, or raise "
                    f"{ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY}"
                ),
            )
        return
    headroom = int(getattr(store, "emergency_slots", 0) or 0)
    if n_workers > cap + headroom:
        raise HTTPException(
            status_code=422,
            detail=(
                f"n_workers={n_workers} exceeds this deployment's maximum "
                f"concurrency for incident runs, {cap} + {headroom} emergency worker "
                f"slot(s) ({ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY}={cap}, "
                f"{ENVIRONMENT_VARIABLE_CITY_EMERGENCY_SLOTS}={headroom}), so the run "
                f"can never start: lower n_workers to {cap + headroom} or less, or "
                f"raise {ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY}"
            ),
        )


def _osm_download_enabled() -> bool:
    from terasim_service.city.map_acquisition import osm_download_enabled

    return osm_download_enabled()


def _stored_map_or_422(map_acquirer, tmc_json) -> Path:
    """The base_sumocfg of a stored map covering the demand, selected without downloading.

    For deployments with OpenStreetMap downloads switched off: a store lookup only, so
    it runs before capacity is reserved and a request no stored map covers gets its 422
    (naming TERASIM_CITY_OSM_DOWNLOAD) at once, leaving no run behind.
    """
    from terasim_service.city.map_acquisition import (
        MapAcquirer,
        OsmDownloadDisabled,
        no_stored_map_message,
    )

    try:
        acquirer = map_acquirer or MapAcquirer.from_environment()
    except ValueError as exc:  # no map store configured (TERASIM_CITY_DATA_ROOT)
        raise HTTPException(status_code=422, detail=f"{no_stored_map_message()} ({exc})") from exc
    try:
        return Path(acquirer(Path(tmc_json))).resolve()
    except OsmDownloadDisabled as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def submit_optimization(
    request: Union[dict, OptimizationRequest],
    store: CityJobStore,
    launcher: Callable[[str, dict, str], None],
    *,
    output_root: Path,
    input_root: Optional[Path] = None,
    buffer=None,
    corridors: Optional[Mapping[str, dict]] = None,
    window_s: float = DEFAULT_WINDOW_S,
    origin: str = ORIGIN_API,
    corridor_id: Optional[str] = None,
) -> str:
    """Validate inputs, reserve n_workers concurrency slots, launch the worker.

    Returns the new run_id. Also the direct entry point for the scheduler
    (which passes a plain dict and no input_root — its tmc_json is a
    server-generated file under output_root — with ``origin="scheduled"`` and the
    entry's ``corridor_id``). The job record carries ``origin``, the corridor (the
    request's ``corridor_id``, else ``corridor_id``, else the one the TMC file
    names) and the intersections the request names. Server-generated inputs
    (aggregated demand / incident stalls, written under
    ``output_root/<run_id>/inputs/``) are exempt from the input-root
    confinement. An inline ``incident`` acquires slots with ``emergency=True``
    (reserved headroom) and forwards ``stall_data`` + ``trigger="incident"``
    to the worker. A ``traffic_model="cnde"`` run reserves, and runs, at most
    TERASIM_CITY_CNDE_MAX_WORKERS workers, and its record carries
    ``traffic_model="cnde"``. Raises HTTPException on any failure: 400 bad input
    (including corridor_id mode without a wired snapshot buffer), 409 empty
    snapshot window, 422 malformed request or ``n_workers`` above the
    deployment's maximum concurrency (it could never start) or, with
    ``TERASIM_CITY_OSM_DOWNLOAD=0``, no stored map covering a request without
    ``base_sumocfg``, or ``traffic_model="cnde"`` where the CNDE runtime is not
    installed, 429 at capacity (other runs hold the slots; retry later), 503
    store unavailable, 500 launch failure.
    """
    if isinstance(request, dict):
        try:
            request = OptimizationRequest.model_validate(request)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=f"invalid optimization request: {exc}"
            ) from exc

    run_id = uuid.uuid4().hex
    output_dir = output_root / run_id
    inputs_dir = output_dir / "inputs"
    run_corridor_id = request.corridor_id or corridor_id

    if request.corridor_id is not None:
        request = _resolve_corridor_request(
            request, inputs_dir, buffer, corridors, window_s
        )
        # The aggregated demand file is server-generated under output_root,
        # so it is trusted and not confined to the client input root.
        _validate_input_file(request.tmc_json, None)
    else:
        _validate_input_file(request.tmc_json, input_root)
    # base_sumocfg is optional: when omitted the worker auto-selects the
    # stored map covering the corridor, so there is nothing to validate here.
    if request.base_sumocfg is not None:
        sumocfg = _validate_input_file(request.base_sumocfg, input_root)
        _validate_map_references(sumocfg, input_root)
    if request.baseline_sheet is not None:
        _validate_input_file(request.baseline_sheet, input_root)

    # The forwarded dict keeps the pre-incident wire shape the worker expects:
    # incident/corridor_id are API-level concepts, stall_data/trigger are what
    # the worker consumes (_build_optimization_config maps stall_data).
    forwarded = request.model_dump(exclude={"incident", "corridor_id"})
    emergency = request.incident is not None
    if emergency:
        inputs_dir.mkdir(parents=True, exist_ok=True)
        stalls_path = inputs_dir / STALLS_FILENAME
        stalls_path.write_text(
            json.dumps(
                {
                    "stalled_vehicles": [
                        stall.model_dump()
                        for stall in request.incident.stalled_vehicles
                    ]
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        forwarded["stall_data"] = str(stalls_path)
        forwarded["trigger"] = INCIDENT_TRIGGER

    # Deployment-level CNDE/refine knobs (env vars — see
    # worker_optimize._build_optimization_config) are validated NOW, before a
    # slot is reserved or the worker is launched: an ops misconfiguration
    # (unregistered vehicle behavior, non-integer top_k/max_collisions) must
    # surface as a submission-time 4xx, never a mid-run worker crash (nor a
    # slot held by a job that can never succeed). So is a traffic_model="cnde"
    # request on a deployment without the CNDE runtime. The worker rebuilds its
    # own config once base_sumocfg is resolved; only the worker count and the
    # traffic model are taken from this one.
    try:
        config = _build_optimization_config(forwarded, output_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid optimization config: {exc}"
        ) from exc
    # A CNDE run may run fewer workers than the request names (at most
    # TERASIM_CITY_CNDE_MAX_WORKERS of its trials share the GPU): it reserves,
    # and its worker runs, that many.
    n_workers = forwarded["n_workers"] = config.n_workers

    # Without OpenStreetMap downloads the worker can only select a stored map:
    # check now that one covers the demand (no download), so the request gets its
    # 422 instead of a run that fails in the worker. The worker selects it again.
    if forwarded.get("base_sumocfg") is None and not _osm_download_enabled():
        _stored_map_or_422(None, request.tmc_json)

    _reject_unreachable_slot_count(store, n_workers, emergency=emergency)
    try:
        acquired = store.try_acquire_slots(run_id, n_workers, emergency=emergency)
    except Exception as exc:
        logger.exception("job store unavailable acquiring slots")
        raise HTTPException(status_code=503, detail="job store unavailable") from exc
    if not acquired:
        raise HTTPException(
            status_code=429,
            detail=(
                f"not enough capacity for {n_workers} worker slot(s); "
                "retry later"
            ),
        )

    created = False
    try:
        store.create(
            run_id,
            origin=origin,
            corridor_id=run_corridor_id or _tmc_corridor_id(request.tmc_json),
            intersection_ids=_requested_intersection_ids(request.corridor),
            traffic_model=traffic_model_of(config.vehicle_behavior),
        )
        created = True
        launcher(run_id, forwarded, str(output_dir))
    except Exception as exc:
        logger.exception("failed to start city optimization %s", run_id)
        if created:
            _best_effort(store.set_failed, run_id, f"failed to start: {exc}")
        _best_effort(store.release_slot, run_id)
        raise HTTPException(status_code=500, detail="failed to start optimization") from exc

    return run_id


def make_scheduled_submit(
    store: CityJobStore,
    launcher: Callable[[str, dict, str], None],
    *,
    output_root: Path,
    on_submitted: Optional[Callable[[str, str], None]] = None,
) -> Callable[[dict, Path], str]:
    """The scheduler's submit callback over :func:`submit_optimization`.

    The entry (minus its id and the scheduler's own keys) becomes the request, on
    the server-generated demand file (trusted, so no input-root confinement). A
    rejected submission raises :class:`SubmissionRejected` with the HTTP status
    and detail, which the scheduler logs; ``on_submitted(corridor_id, run_id)``
    records an accepted run (production: the ``city_sched:last:{id}`` Redis key).
    """

    def submit(corridor_cfg: dict, aggregated_path: Path) -> str:
        corridor_id = corridor_cfg["corridor_id"]
        request = {
            key: value
            for key, value in corridor_cfg.items()
            if key != "corridor_id" and key not in SCHEDULER_ONLY_KEYS
        }
        request["tmc_json"] = str(aggregated_path)
        try:
            run_id = submit_optimization(
                request,
                store,
                launcher,
                output_root=output_root,
                input_root=None,
                origin=ORIGIN_SCHEDULED,
                corridor_id=corridor_id,
            )
        except HTTPException as exc:
            raise SubmissionRejected(exc.status_code, str(exc.detail)) from exc
        if on_submitted is not None:
            on_submitted(corridor_id, run_id)
        return run_id

    return submit


def make_run_status(store: CityJobStore) -> Callable[[str], Optional[str]]:
    """The scheduler's run-status lookup over the job store.

    Returns the job's status, ``None`` for an unknown run, and ``"lost"`` for a run
    still recorded as submitted or running whose lease has lapsed: its worker died
    before it could record the outcome, so the run is not in progress anymore.
    """

    def run_status(run_id: str) -> Optional[str]:
        record = store.get(run_id)
        if record is None:
            return None
        if record.status in (SUBMITTED, RUNNING) and not store.holds_slots(run_id):
            return "lost"
        return record.status

    return run_status


def _nema_skip_reason(result) -> Optional[str]:
    """Why a finished optimization wrote no NEMA plan, as its result says (else None).

    The optimizer skips the export, and records ``nema_export`` in its result, when
    the plan it picked has no common cycle (a customer's own programs often run a
    different cycle at every signal). The run itself still succeeded.
    """
    block = result.get("nema_export") if isinstance(result, dict) else None
    if isinstance(block, dict) and block.get("exported") is False:
        return str(block.get("reason") or "the optimizer skipped the NEMA export")
    return None


def _stored_result(output_root: Path, run_id: str) -> Optional[dict]:
    """The run's result JSON, or None when it is missing, unreadable or outside the root."""
    path = (output_root / run_id / RESULT_FILENAME).resolve()
    if not _is_within(path, output_root) or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_completed_file(
    store: CityJobStore, run_id: str, output_root: Path, filename: str
) -> dict:
    """Return a JSON artifact of a completed run, or raise the right error.

    404 unknown / 409 failed-or-not-finished / 410 artifact file gone. The path
    is derived from server-controlled values only and re-confirmed to stay
    under output_root. A completed run that exported no NEMA plan (its result's
    ``nema_export`` says so) answers 409 with that reason instead of 410: the file
    was never written, it has not vanished.
    """
    record = _get_record_or_404(store, run_id)
    if record.status == FAILED:
        raise HTTPException(
            status_code=409,
            detail=f"optimization failed: {record.error or 'unknown error'}",
        )
    if record.status != COMPLETED:
        raise HTTPException(
            status_code=409,
            detail=f"optimization not finished (status: {record.status})",
        )
    result_path = (output_root / run_id / filename).resolve()
    if not _is_within(result_path, output_root) or not result_path.is_file():
        if filename == NEMA_FILENAME:
            reason = _nema_skip_reason(_stored_result(output_root, run_id))
            if reason is not None:
                raise HTTPException(status_code=409, detail=reason)
        raise HTTPException(status_code=410, detail=f"{filename} is no longer available")
    return json.loads(result_path.read_text(encoding="utf-8"))


def _direct_kpis(result: dict, weights: Mapping[str, float]) -> dict:
    """Project the optimizer result to the stable VSS-facing KPI response.

    Must track ``result["export_source"]`` (see corridor_optimization's
    module docstring): when the refine
    stage actually produced the exported plan (``export_source ==
    "refine"``), "optimized" reports THAT winner's cost/metrics — sourced
    from ``result["refine"]``, not ``result["best"]``, which is a different,
    coarse-stage plan evaluated under a different ``vehicle_behavior``. The
    winner is looked up by ``winner_plan_id`` among the refine block's
    baseline + evaluations entries (the winner can legitimately BE the
    refine-refined baseline plan). "baseline" switches together, to the
    refine-stage baseline — it is not comparable to the coarse
    ``result["baseline"]``, which was measured under a different behavior
    (see corridor_optimization._run_refine_stage's docstring).

    ``export_source == "coarse"`` or absent: unchanged legacy sourcing from
    ``result["best"]`` / ``result["baseline"]`` — a sumo_passive deployment
    with no refine env vars set never sets ``export_source`` at all, so the
    coarse sourcing applies.

    Each side also carries the ``timing`` of its plan (per-TLS cycle, offsets
    and greens) when the chosen block has one, else ``None``: the coarse
    ``baseline`` / ``best`` blocks record it, the refine-stage entries do not.
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
            raise ValueError(
                f"refine.winner_plan_id={winner_plan_id!r} not found among "
                "the refine block's baseline/evaluations entries"
            )
    else:
        baseline = result["baseline"]
        optimized = result["best"]
    return {
        "cost_weights": dict(weights),
        "baseline": {
            "cost": baseline["score"],
            "metrics": baseline["moe"],
            "timing": baseline.get("timing"),
        },
        "optimized": {
            "cost": optimized["score"],
            "metrics": optimized["moe"],
            "timing": optimized.get("timing"),
        },
    }


def _scenario_demand_sources(result: dict) -> Optional[dict[str, str]]:
    """``{"flow_source", "counts_source"}`` of the run, when either is ``scenario_routes``.

    Lets the chat summary say the demand came from the scenario's own route files
    rather than from the measured counts; ``None`` (key left out) for a default run.
    """
    config = result.get("config") if isinstance(result, dict) else None
    if not isinstance(config, dict):
        return None
    sources = {
        key: str(config[key])
        for key in ("flow_source", "counts_source")
        if config.get(key) is not None
    }
    return sources if "scenario_routes" in sources.values() else None


def _failure_message(exc: BaseException) -> str:
    """What a failed direct run records as its error."""
    if isinstance(exc, HTTPException):
        return f"HTTP {exc.status_code}: {exc.detail}"
    return str(exc) or type(exc).__name__


def _launch_sim_metrics(launcher, run_id: str, run_dir: Path) -> None:
    """Start a completed run's metrics job; a failure to start is recorded, never raised."""
    try:
        launcher(run_id, run_dir)
    except Exception as exc:  # noqa: BLE001 - the run itself is done and recorded
        logger.exception("could not start the simulated signal metrics of run %s", run_id)
        from terasim_service.city.sim_metrics_job import (
            SIM_METRICS_ERROR_FILENAME,
            SIM_METRICS_PENDING_FILENAME,
        )

        try:
            (run_dir / SIM_METRICS_ERROR_FILENAME).write_text(
                json.dumps({"reason": f"the computation could not be started: {exc}"}),
                encoding="utf-8",
            )
            (run_dir / SIM_METRICS_PENDING_FILENAME).unlink(missing_ok=True)
        except OSError:
            pass


def run_direct_optimization(
    request: DirectOptimizationRequest,
    store: CityJobStore,
    *,
    output_root: Path,
    buffer,
    corridors: Optional[Mapping[str, dict]],
    window_s: float,
    direct_optimizer=None,
    map_acquirer: Optional[Callable[[Path], Path]] = None,
    heartbeat_interval: float = DIRECT_HEARTBEAT_INTERVAL_S,
    sim_metrics_launcher: Optional[Callable[[str, Path], None]] = None,
) -> dict:
    """Execute one VSS request synchronously and record it as a run.

    The run works in ``output_root/<run_id>/`` exactly like an asynchronous run
    and stays there. It holds its concurrency lease under its run id; once the
    slots are reserved the job store records it (origin ``direct``, its corridor
    and the intersections the request names) and marks it running, then completed
    with the result path, or failed with the error. The status, result, nema and
    listing endpoints therefore serve it like any other run. The response body is
    unchanged: the NEMA plan plus the KPI comparison, or the explained 409 when
    the run exported no NEMA plan (the run is still recorded as completed). A
    request rejected before its run starts (invalid config, no capacity, no demand)
    leaves neither a record nor a directory behind. A corridor timing sheet with no
    entry for a requested signal is a 422 naming it (the run is recorded as failed);
    a sheet covering more signals than requested seeds the run with the requested
    ones. With ``TERASIM_CITY_OSM_DOWNLOAD=0`` a request no stored map covers is a 422
    naming the setting before anything is reserved or recorded, and so is a
    ``traffic_model="cnde"`` request where the CNDE runtime is not installed; a CNDE
    run is answered with ``traffic_model: "cnde"``. ``sim_metrics_launcher(run_id,
    run_dir)`` starts the run's simulated signal metrics (when enabled) once the run
    is recorded as completed and its slots are released; it never delays or fails the
    response.
    """

    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    work_dir = output_root / run_id
    acquired = False
    registered = False
    completed = False
    metrics_after = False
    heartbeat = None
    stop = threading.Event()
    lease_lost = threading.Event()

    try:
        concrete = _resolve_corridor_request(
            request.as_internal_request(),
            work_dir / "inputs",
            buffer,
            corridors,
            window_s,
        )

        # Deployment-level CNDE/refine knobs (env vars — see
        # worker_optimize._build_optimization_config) are validated HERE,
        # before a slot is reserved or a map is downloaded/converted: none of
        # those env vars depend on base_sumocfg/tmc content, so there is
        # no reason an ops misconfiguration should burn a slot or a
        # potentially expensive OSM fetch before 422ing — same ordering
        # rationale submit_optimization already applies to slot acquisition
        # vs. the worker's own map download (see the comment there).
        # base_sumocfg may still be None here (resolved below), so this cannot
        # double as the config the run actually uses; the real config is
        # (re)built after base_sumocfg is resolved. Only the worker count and
        # the traffic model are taken from this one.
        try:
            validated = _build_optimization_config(
                concrete.model_dump(exclude={"incident", "corridor_id"}),
                work_dir,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"invalid optimization config: {exc}"
            ) from exc
        # A CNDE run may run fewer workers than the request names (at most
        # TERASIM_CITY_CNDE_MAX_WORKERS of its trials share the GPU): it
        # reserves, and runs, that many.
        concrete = concrete.model_copy(update={"n_workers": validated.n_workers})

        # Without OpenStreetMap downloads (TERASIM_CITY_OSM_DOWNLOAD=0) the map
        # must already be stored: select it now - a store lookup, nothing to
        # download - so a request no stored map covers is a 422 before capacity
        # is reserved or a run is recorded.
        if concrete.base_sumocfg is None and not _osm_download_enabled():
            stored = _stored_map_or_422(map_acquirer, concrete.tmc_json)
            concrete = concrete.model_copy(update={"base_sumocfg": str(stored)})

        # Reserve capacity before any potentially expensive map download or
        # conversion. A request that cannot run must fail fast — 422 when it
        # needs more slots than the deployment ever grants, 429 when the slots
        # are busy — and leave the shared map cache untouched.
        _reject_unreachable_slot_count(store, concrete.n_workers)
        try:
            acquired = store.try_acquire_slots(run_id, concrete.n_workers)
        except Exception as exc:
            logger.exception("job store unavailable acquiring direct optimization slots")
            raise HTTPException(status_code=503, detail="job store unavailable") from exc
        if not acquired:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"not enough capacity for {concrete.n_workers} worker slot(s); "
                    "retry later"
                ),
            )

        try:
            store.create(
                run_id,
                origin=ORIGIN_DIRECT,
                corridor_id=request.corridor_id,
                intersection_ids=request.target_ids(),
                traffic_model=traffic_model_of(validated.vehicle_behavior),
            )
            registered = True
            store.set_running(run_id)
        except Exception as exc:
            logger.exception("job store unavailable recording direct optimization %s", run_id)
            raise HTTPException(status_code=503, detail="job store unavailable") from exc

        def _heartbeat() -> None:
            while not stop.wait(heartbeat_interval):
                try:
                    renewed = store.renew_slot(run_id)
                except Exception:
                    renewed = False
                if not renewed:
                    lease_lost.set()
                    return

        def _progress(done: int, total: int, plan_id) -> None:
            if lease_lost.is_set():
                raise RuntimeError("optimization concurrency lease was lost")
            _best_effort(store.set_progress, run_id, done, total, plan_id)

        heartbeat = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat.start()

        if concrete.base_sumocfg is None:
            from terasim_service.city.map_acquisition import MapAcquirer, OsmDownloadDisabled

            if map_acquirer is None:
                map_acquirer = MapAcquirer.from_environment()
            try:
                base_sumocfg = Path(map_acquirer(Path(concrete.tmc_json))).resolve()
            except OsmDownloadDisabled as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            concrete = concrete.model_copy(
                update={"base_sumocfg": str(base_sumocfg)}
            )

        if lease_lost.is_set():
            raise RuntimeError("optimization concurrency lease was lost")

        forwarded = concrete.model_dump(exclude={"incident", "corridor_id"})
        # The config actually used for the run, now that base_sumocfg is
        # resolved. Deployment env vars were already validated above (before
        # the slot/map cost was paid); the same try/except stays here as a
        # cheap belt-and-suspenders guard (e.g. an env var edited mid-request)
        # so any residual failure still maps to a 4xx, never a mid-run crash.
        try:
            config = _build_optimization_config(forwarded, work_dir)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"invalid optimization config: {exc}"
            ) from exc
        if direct_optimizer is None:
            from terasim_city.orchestration.corridor_optimization import (
                run_corridor_optimization,
            )

            direct_optimizer = run_corridor_optimization
        try:
            result = direct_optimizer(config, progress_callback=_progress)
        except TimingSheetCoverageError as exc:
            # The corridor's timing sheet (its scheduled entry's baseline_sheet) has
            # no entry for a signal the request covers: the request cannot run as
            # asked. Sheets covering more signals than requested are fine.
            raise HTTPException(
                status_code=422,
                detail=f"the corridor's baseline timing sheet does not cover the request: {exc}",
            ) from exc
        if lease_lost.is_set():
            raise RuntimeError("optimization concurrency lease was lost")

        # The result file (the optimizer writes it; a stand-in that only returns
        # the result still gets one) and the baseline's NEMA copy.
        result_path = work_dir / RESULT_FILENAME
        finalize_run_outputs(work_dir, result)
        summary = _exported_summary(result)
        final_score = summary.get("score")
        if final_score is not None and math.isfinite(float(final_score)):
            _best_effort(store.set_best_score, run_id, float(final_score))
        if sim_metrics_launcher is not None and sim_metrics_enabled():
            mark_pending(work_dir)
            metrics_after = True
        store.set_completed(run_id, str(result_path), summary)
        completed = True

        nema_path = work_dir / NEMA_FILENAME
        if not nema_path.is_file():
            # The optimization succeeded but exported no plan to return (see
            # _nema_skip_reason): an explained 409, not an unhandled file error.
            reason = _nema_skip_reason(result)
            if reason is not None:
                raise HTTPException(status_code=409, detail=reason)
        nema = json.loads(nema_path.read_text(encoding="utf-8"))
        response = {
            "nema_plan": nema,
            "kpis": _direct_kpis(result, concrete.kpi_weights or {}),
        }
        unserved = result.get("unserved_movements")
        if isinstance(unserved, dict) and unserved:
            response["unserved_movements"] = unserved
        sources = _scenario_demand_sources(result)
        if sources is not None:
            response["config"] = sources
        traffic_model = traffic_model_of(config.vehicle_behavior)
        if traffic_model != DEFAULT_TRAFFIC_MODEL:
            response["traffic_model"] = traffic_model
        return response
    except BaseException as exc:
        if registered and not completed:
            _best_effort(store.set_failed, run_id, _failure_message(exc))
        raise
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=5)
        if acquired:
            _best_effort(store.release_slot, run_id)
        if not registered and work_dir.exists():
            # Rejected before the run started: nothing worth keeping.
            shutil.rmtree(work_dir, ignore_errors=True)
        if completed and metrics_after:
            _launch_sim_metrics(sim_metrics_launcher, run_id, work_dir)


def create_v1_city_router(
    store: CityJobStore,
    launcher: Callable[[str, dict, str], None],
    *,
    output_root,
    input_root=None,
    buffer=None,
    corridors: Optional[Mapping[str, dict]] = None,
    window_s: float = DEFAULT_WINDOW_S,
    direct_optimizer=None,
    map_acquirer: Optional[Callable[[Path], Path]] = None,
    sim_metrics_launcher: Optional[Callable[[str, Path], None]] = None,
) -> APIRouter:
    """Build the /v1/city optimization router around an injected store + launcher.

    ``buffer``/``window_s`` power corridor_id mode (snapshot aggregation);
    without a buffer, corridor_id requests are rejected with 400 and
    everything else works as before. ``corridors`` (the scheduled corridor
    config) is optional: entries supply per-corridor request defaults. A
    request without ``base_sumocfg`` uses the cached-map/online-OSM acquisition
    layer, so no scheduled-config entry is required for the VSS flow.
    ``sim_metrics_launcher(run_id, run_dir)`` starts the simulated signal metrics of
    a completed synchronous run (asynchronous runs compute theirs in their worker).
    The audit log endpoints (``/v1/city/audit/*``, kept under ``output_root/audit/``,
    switched by TERASIM_CITY_AUDIT_ENABLED) are on this router too.
    """
    router = APIRouter(prefix="/v1/city", tags=["city-optimization"])
    output_root = Path(output_root).resolve()
    input_root = Path(input_root).resolve() if input_root is not None else None
    window_s = float(window_s)

    @router.post("/optimizations", status_code=202)
    def submit(request: OptimizationRequest):
        run_id = submit_optimization(
            request,
            store,
            launcher,
            output_root=output_root,
            input_root=input_root,
            buffer=buffer,
            corridors=corridors,
            window_s=window_s,
        )
        return {"run_id": run_id, "status": "submitted"}

    @router.post(
        "/optimize", status_code=200, response_model=DirectOptimizationResponse
    )
    def optimize_direct(request: DirectOptimizationRequest):
        return run_direct_optimization(
            request,
            store,
            output_root=output_root,
            buffer=buffer,
            corridors=corridors,
            window_s=window_s,
            direct_optimizer=direct_optimizer,
            map_acquirer=map_acquirer,
            sim_metrics_launcher=sim_metrics_launcher,
        )

    @router.get("/optimizations/{run_id}/status")
    def get_status(run_id: str):
        record = _get_record_or_404(store, run_id)
        return {
            "run_id": run_id,
            "status": record.status,
            "progress": {"done": record.done, "total": record.total},
            "best_score": record.best_score,
            "updated_at": _iso(record.updated_at),
        }

    @router.get("/optimizations/{run_id}/result")
    def get_result(run_id: str):
        return _read_completed_file(store, run_id, output_root, RESULT_FILENAME)

    @router.get("/optimizations/{run_id}/nema")
    def get_nema(run_id: str):
        return _read_completed_file(store, run_id, output_root, NEMA_FILENAME)

    register_run_routes(router, store, output_root=output_root)
    register_audit_routes(router, output_root=output_root)
    return router


def build_v1_production_router() -> dict:
    """Build the shared store + launcher + buffer + v1 router from the environment.

    The store uses the SAME store config
    (redis connection, cap, TTL, key prefix, emergency headroom) as the
    signal-evaluation surface, so the concurrency cap is global across all
    city endpoints. Builds ONE Redis client and ONE snapshot buffer that the
    app layer shares with the ingest router and the scheduler, plus the
    corridors from TERASIM_CITY_SCHEDULED_CONFIG (when set) and the window
    from TERASIM_CITY_WINDOW_S so corridor_id/incident requests work on the
    production router. Returns the router plus the pieces (store / launcher /
    buffer / redis_client / roots / store_config / corridors / window_s) the
    app layer reuses. Fails loud (RuntimeError) if the required output root is
    missing. Called at app-startup time, never at import time.
    """
    from terasim_service.constants import (
        ENVIRONMENT_VARIABLE_CITY_SCHEDULED_CONFIG,
        ENVIRONMENT_VARIABLE_CITY_WINDOW_S,
    )

    output_root = os.environ.get(ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT)
    if not output_root:
        raise RuntimeError(
            f"{ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT} must be set to enable city endpoints"
        )
    input_root = os.environ.get(ENVIRONMENT_VARIABLE_CITY_INPUT_ROOT) or None

    config = store_config_from_env()
    import redis

    from terasim_service.city.ingest_api import RedisSnapshotBuffer

    redis_client = redis.Redis(**config["redis"])
    store = RedisCityJobStore(
        client=redis_client,
        max_concurrency=config["max_concurrency"],
        lease_ttl=config["lease_ttl"],
        key_prefix=config["key_prefix"],
        emergency_slots=config["emergency_slots"],
    )
    launcher = make_optimize_process_launcher(config)
    buffer = RedisSnapshotBuffer(redis_client)
    map_acquirer = None
    if os.environ.get("TERASIM_CITY_DATA_ROOT"):
        from terasim_service.city.map_acquisition import MapAcquirer

        map_acquirer = MapAcquirer.from_environment()

    corridors = None
    scheduled_config = os.environ.get(ENVIRONMENT_VARIABLE_CITY_SCHEDULED_CONFIG)
    if scheduled_config:
        from terasim_service.city.scheduler import load_scheduled_config

        corridors = load_scheduled_config(scheduled_config)
    window_s = float(
        os.environ.get(ENVIRONMENT_VARIABLE_CITY_WINDOW_S) or DEFAULT_WINDOW_S
    )

    return {
        "router": create_v1_city_router(
            store,
            launcher,
            output_root=output_root,
            input_root=input_root,
            buffer=buffer,
            corridors=corridors,
            window_s=window_s,
            map_acquirer=map_acquirer,
            sim_metrics_launcher=make_sim_metrics_process_launcher(config),
        ),
        "store": store,
        "launcher": launcher,
        "buffer": buffer,
        "redis_client": redis_client,
        "output_root": Path(output_root).resolve(),
        "input_root": Path(input_root).resolve() if input_root is not None else None,
        "store_config": config,
        "corridors": corridors,
        "window_s": window_s,
        "map_acquirer": map_acquirer,
    }
