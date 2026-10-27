"""Read endpoints over the optimization runs, for every run whatever started it.

    GET /v1/city/optimizations?corridor_id=&limit= every run of every origin (scheduled,
                                                  api, direct), newest first, with its
                                                  status, traffic model, KPI summary and
                                                  the state of its NEMA export, report
                                                  and metrics
    GET /v1/city/optimizations/{run_id}/baseline  the plan the run measured itself
                                                  against, as NEMA timing (409 until
                                                  completed, 404 + reason when the
                                                  baseline cannot be expressed)
    GET /v1/city/optimizations/{run_id}/sim_metrics
                                                  simulated signal-performance measures
                                                  (200 document, 202 pending, 404 +
                                                  reason when disabled or failed)
    GET /v1/city/optimizations/{run_id}/report.pdf
                                                  the run's timing report (rendered on
                                                  first request and cached; 409 until
                                                  completed, 404 + reason)

Mounted by :func:`terasim_service.city.optimize_api.create_v1_city_router` on its
router; the run directory is ``output_root/<run_id>/`` (see
:mod:`terasim_service.city.run_outputs` for what it holds).
"""

from __future__ import annotations

import logging
import math
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from terasim_service.city.common import _get_record_or_404, _is_within, _iso
from terasim_service.city.job_store import (
    COMPLETED,
    FAILED,
    RUNNING,
    SUBMITTED,
    ORIGINS,
    CityJobStore,
)
from terasim_service.city.run_outputs import (
    NEMA_FILENAME,
    RESULT_FILENAME,
    BaselineUnavailable,
    baseline_plan_payload,
    baseline_source,
    load_baseline_nema,
    read_json,
    read_result,
)
from terasim_service.city.run_report import ReportUnavailable, has_report, render_run_report
from terasim_service.city.sim_metrics_job import (
    PENDING,
    READY,
    SIM_METRICS_FILENAME,
    sim_metrics_state,
)

logger = logging.getLogger(__name__)

#: Most runs one listing returns; larger ``limit`` values are treated as this.
MAX_LISTED_RUNS = 200
DEFAULT_LISTED_RUNS = 20
#: Job-store status -> listing status (the rest are the same word).
_LISTED_STATUS = {SUBMITTED: "queued"}
#: Result summaries kept in memory (keyed by run, checked against the file's stat).
_SUMMARY_CACHE_SIZE = 1024


class RunKpis(BaseModel):
    """One side of a run's KPI summary (from the run's result, like the synchronous answer)."""

    model_config = ConfigDict(extra="forbid")

    cost: Optional[float] = Field(None, description="Weighted cost; the baseline is 1.0.")
    control_delay_s_per_veh: Optional[float] = None
    stops_per_veh: Optional[float] = None
    vehicles_total: Optional[int] = None


class RunKpiSummary(BaseModel):
    """Baseline versus optimized (the exported plan) KPIs of a completed run."""

    model_config = ConfigDict(extra="forbid")

    baseline: RunKpis
    optimized: RunKpis


class OptimizationRunSummary(BaseModel):
    """One optimization run of the listing."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    corridor_id: Optional[str] = None
    origin: Literal["scheduled", "api", "direct"] = Field(
        ...,
        description=(
            "scheduled: the cadence scheduler; api: POST /v1/city/optimizations; "
            "direct: the synchronous POST /v1/city/optimize."
        ),
    )
    status: Literal["queued", "running", "completed", "failed", "lost"] = Field(
        ...,
        description="lost: queued or running, but its worker stopped without recording an outcome.",
    )
    submitted_at: Optional[str] = Field(None, description="ISO-8601 UTC.")
    completed_at: Optional[str] = Field(
        None, description="ISO-8601 UTC; null until the run completed or failed."
    )
    intersection_ids: list[str] = Field(
        ...,
        description=(
            "The run's intersections: those of its resolved corridor once completed, "
            "else those the request named."
        ),
    )
    traffic_model: Optional[Literal["sumo", "cnde"]] = Field(
        None,
        description=(
            "The traffic model every candidate plan was simulated with: sumo (the "
            "deployment's default driver model) or cnde (the CNDE learned driver "
            "behavior, requested per run). Null for runs recorded before it was kept."
        ),
    )
    kpis: Optional[RunKpiSummary] = Field(None, description="Null until completed.")
    nema_exported: bool = Field(..., description="Whether /nema serves a plan.")
    has_report: bool = Field(..., description="Whether report.pdf can be served (rendered or renderable).")
    sim_metrics: Literal["ready", "pending", "unavailable"] = Field(
        ..., description="State of the run's simulated signal metrics (/sim_metrics)."
    )


class BaselinePlanResponse(BaseModel):
    """The plan a run measured its optimization against, as NEMA timing."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    plan_id: str = Field(..., description="The baseline's plan id in the run's trial ledger.")
    source: str = Field(
        ...,
        description=(
            "Where the baseline came from: stored_nema (the uploaded signal plans), "
            "baseline_sheet (a timing sheet), webster_init (timings computed from the "
            "demand) or sumo_tls (the scenario's own signal programs)."
        ),
    )
    nema_plan: dict[str, Any] = Field(
        ...,
        description=(
            "The baseline in the terasim.nema_timing.v1 shape of /nema: the stored plan "
            "as the run used it, or the export of the baseline static plan (stage rows "
            "with nema_phase null unless the plan numbers its phases)."
        ),
    )


class SimMetricsPending(BaseModel):
    """The simulated signal metrics of the run are still being computed."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field("pending", description="Always \"pending\".")


class SimMetricsDocument(BaseModel):
    """Simulated signal-performance measures of a run (schema corridor_ui.sim_metrics.v1).

    The run's baseline and optimized trials re-simulated with virtual lane-area
    detectors: per approach, per signal and per corridor arrivals on green / red,
    occupancy, split failures, queues, delay, stops, phase utilization and degree of
    saturation. Simulated estimates of the plans under the run's demand, not field
    measurements.
    """

    model_config = ConfigDict(extra="forbid")

    schema_: str = Field(..., alias="schema", description="corridor_ui.sim_metrics.v1")
    corridor_id: Optional[str] = None
    run_id: str
    generated_at: str
    method: dict[str, Any] = Field(..., description="How the measures were computed.")
    corridor_order: list[str] = Field(..., description="Signals in corridor order.")
    mainline: list[str] = Field(..., description="Mainline directions (NB/SB or EB/WB).")
    plans: dict[str, Any] = Field(
        ..., description='"optimized" and "baseline": corridor, intersections, phase0_starts_s.'
    )


_RUN_ERRORS = {
    404: {"description": "Unknown run, or the item cannot be produced (the detail says why)"},
    409: {"description": "The run has not completed (or failed)"},
}


def _completed_record(store: CityJobStore, run_id: str):
    """The run's record when it completed; 404 unknown, 409 failed or not finished."""
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
    return record


def _run_dir(output_root: Path, run_id: str) -> Path:
    """``output_root/<run_id>``, confirmed to stay under the root (404 otherwise)."""
    run_dir = (output_root / run_id).resolve()
    if not _is_within(run_dir, output_root) or run_dir == output_root:
        raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}")
    return run_dir


def _run_status(store: CityJobStore, record) -> str:
    """The record's status, or ``"lost"`` for a queued/running run whose lease lapsed."""
    if record.status in (SUBMITTED, RUNNING):
        try:
            alive = store.holds_slots(record.run_id)
        except Exception:  # noqa: BLE001 - unknown liveness: report what the record says
            alive = True
        if not alive:
            return "lost"
    return record.status


def _finite(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _count(value) -> Optional[int]:
    number = _finite(value)
    return int(number) if number is not None and number.is_integer() else None


def _listed_kpis(result: dict) -> Optional[dict]:
    """The KPI summary of a result: the sides ``POST /v1/city/optimize`` reports."""
    from terasim_service.city.optimize_api import _direct_kpis

    try:
        kpis = _direct_kpis(result, {})
    except (KeyError, TypeError, ValueError, AttributeError):
        return None

    def side(block: dict) -> dict:
        metrics = block.get("metrics") if isinstance(block.get("metrics"), dict) else {}
        return {
            "cost": _finite(block.get("cost")),
            "control_delay_s_per_veh": _finite(metrics.get("control_delay_s_per_veh")),
            "stops_per_veh": _finite(metrics.get("stops_per_veh")),
            "vehicles_total": _count(metrics.get("vehicles_total")),
        }

    return {"baseline": side(kpis["baseline"]), "optimized": side(kpis["optimized"])}


def _resolved_ids(result: dict) -> list[str]:
    ids = []
    for item in result.get("resolved_corridor") or []:
        if isinstance(item, dict):
            value = item.get("intersection_id") or item.get("ref") or item.get("tls_id")
            if value is not None and str(value) not in ids:
                ids.append(str(value))
    return ids


class _ResultSummaries:
    """Per-run summaries of result files, re-read only when the file changes."""

    def __init__(self, size: int = _SUMMARY_CACHE_SIZE) -> None:
        self._size = size
        self._entries: "OrderedDict[str, tuple]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, run_dir: Path) -> Optional[dict]:
        path = run_dir / RESULT_FILENAME
        try:
            stat = path.stat()
        except OSError:
            return None
        key, stamp = str(run_dir), (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and cached[0] == stamp:
                self._entries.move_to_end(key)
                return cached[1]
        result = read_json(path)
        summary = None
        if result is not None:
            summary = {"kpis": _listed_kpis(result), "intersection_ids": _resolved_ids(result)}
        with self._lock:
            self._entries[key] = (stamp, summary)
            self._entries.move_to_end(key)
            while len(self._entries) > self._size:
                self._entries.popitem(last=False)
        return summary


def register_run_routes(router: APIRouter, store: CityJobStore, *, output_root: Path) -> None:
    """Add the run listing and the per-run read endpoints to ``router`` (prefix ``/v1/city``)."""
    output_root = Path(output_root).resolve()
    summaries = _ResultSummaries()

    def _listed(record) -> dict:
        status = _run_status(store, record)
        run_dir = output_root / record.run_id
        completed = status == COMPLETED
        summary = summaries.get(run_dir) if completed else None
        return {
            "run_id": record.run_id,
            "corridor_id": record.corridor_id,
            "origin": record.origin,
            "status": _LISTED_STATUS.get(status, status),
            "submitted_at": _iso(record.submitted_at or record.updated_at),
            "completed_at": _iso(record.completed_at) if status in (COMPLETED, FAILED) else None,
            "intersection_ids": (summary or {}).get("intersection_ids")
            or list(record.intersection_ids or []),
            "traffic_model": record.traffic_model,
            "kpis": (summary or {}).get("kpis"),
            "nema_exported": completed and (run_dir / NEMA_FILENAME).is_file(),
            "has_report": has_report(run_dir, completed=completed),
            "sim_metrics": sim_metrics_state(run_dir, status).state,
        }

    @router.get(
        "/optimizations",
        response_model=list[OptimizationRunSummary],
        responses={503: {"description": "The job store is unavailable"}},
        summary="Recent optimization runs",
    )
    def list_runs(
        corridor_id: Optional[str] = Query(None, description="Only this corridor's runs."),
        limit: int = Query(
            DEFAULT_LISTED_RUNS, ge=1,
            description=f"How many runs, newest first (at most {MAX_LISTED_RUNS}).",
        ),
    ):
        """Every optimization run, newest first: scheduled, API and synchronous ones.

        Runs are kept as long as their job record (seven days by default).
        """
        try:
            records = store.list_runs(
                corridor_id=corridor_id, limit=min(limit, MAX_LISTED_RUNS)
            )
        except Exception as exc:
            logger.exception("job store unavailable listing runs")
            raise HTTPException(status_code=503, detail="job store unavailable") from exc
        return [_listed(record) for record in records if record.origin in ORIGINS]

    @router.get(
        "/optimizations/{run_id}/baseline",
        response_model=BaselinePlanResponse,
        responses=_RUN_ERRORS,
        summary="Baseline (current) plan of a run",
    )
    def get_baseline(run_id: str):
        """The plan the run's optimization was measured against, as NEMA timing.

        409 until the run has completed; 404 with the reason when the baseline cannot
        be expressed as NEMA timing (or its files are gone).
        """
        record = _completed_record(store, run_id)
        run_dir = _run_dir(output_root, run_id)
        result = read_result(run_dir)
        if result is None:
            raise HTTPException(
                status_code=404, detail="the run's result is no longer available"
            )
        try:
            nema = load_baseline_nema(run_dir, result, corridor_id=record.corridor_id)
        except BaselineUnavailable as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        source = (result.get("seed") or {}).get("source")
        if not source:  # results written before the seed was recorded
            source = baseline_source(result, baseline_plan_payload(run_dir, result))
        return {
            "run_id": run_id,
            "plan_id": str((result.get("baseline") or {}).get("plan_id")),
            "source": str(source),
            "nema_plan": nema,
        }

    @router.get(
        "/optimizations/{run_id}/sim_metrics",
        response_model=None,
        responses={
            200: {"model": SimMetricsDocument, "description": "The metrics document"},
            202: {"model": SimMetricsPending, "description": "Still being computed"},
            404: {"description": "Unknown run, metrics disabled, or the computation failed "
                                 "(the detail says why)"},
        },
        summary="Simulated signal-performance measures of a run",
    )
    def get_sim_metrics(run_id: str):
        """Simulated signal-performance measures of the run's baseline and optimized plans.

        Computed in the background after every completed run while
        TERASIM_CITY_SIM_METRICS is on (two extra SUMO runs per optimization):
        202 {"status": "pending"} until then, 404 with the reason when disabled or
        when the computation failed.
        """
        record = _get_record_or_404(store, run_id)
        run_dir = _run_dir(output_root, run_id)
        state = sim_metrics_state(run_dir, _run_status(store, record))
        if state.state == READY:
            try:
                content = (run_dir / SIM_METRICS_FILENAME).read_bytes()
            except OSError as exc:
                raise HTTPException(
                    status_code=404, detail="the metrics file is no longer available"
                ) from exc
            return Response(content=content, media_type="application/json")
        if state.state == PENDING:
            return JSONResponse(status_code=202, content={"status": "pending"})
        raise HTTPException(status_code=404, detail=state.reason)

    @router.get(
        "/optimizations/{run_id}/report.pdf",
        response_class=FileResponse,
        responses={
            200: {"content": {"application/pdf": {}}, "description": "The timing report"},
            **_RUN_ERRORS,
        },
        summary="Timing report (PDF) of a run",
    )
    def get_report(run_id: str):
        """The run's signal timing report: current plan, optimized plan and KPI comparison.

        Rendered on the first request and kept; 409 until the run has completed; 404 with
        the reason when it cannot be rendered (no NEMA plan exported, no PDF renderer on
        this deployment, or a rendering error).
        """
        record = _completed_record(store, run_id)
        run_dir = _run_dir(output_root, run_id)
        try:
            path = render_run_report(run_dir, run_id=run_id, corridor_id=record.corridor_id)
        except ReportUnavailable as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"timing_report_{run_id}.pdf",
            content_disposition_type="inline",
        )


__all__ = [
    "BaselinePlanResponse",
    "OptimizationRunSummary",
    "SimMetricsDocument",
    "SimMetricsPending",
    "register_run_routes",
]
