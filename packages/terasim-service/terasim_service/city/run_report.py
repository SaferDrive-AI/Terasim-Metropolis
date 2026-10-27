"""The official timing report (PDF) of a completed run, whatever started it.

``GET /v1/city/optimizations/{run_id}/report.pdf`` renders the run with the agent-skill
timing-report renderer (:mod:`terasim_agent_skill.timing_report`, which needs reportlab)
on its first request and caches it as ``timing_report.pdf`` in the run directory; later
requests serve that file.

The renderer reads the run document the chat tools' artifact store writes
(:func:`terasim_agent_skill.artifacts._run_document`): the run's NEMA export as the
optimized plan, the KPI comparison of the exported plan with each side's timing (as
``POST /v1/city/optimize`` answers it), the run's intersections and its current plan.
The document also carries the run's optimizer settings and resolved corridor, so the
report names the search budget and matches signals by id. The current plan:

* seeded from stored NEMA plans: the stored plan as the run used it (the intersections
  of its baseline trial), i.e. the full controller-style report: phase timing, one page
  per coordination pattern with the optimized splits next to the current ones, and the
  time-of-day table;
* any other seed: the corridor's signals, labelled with where the baseline came from
  (the scenario's own programs, a timing sheet, or timings computed from the demand).
  There is no controller document to print, so the report is the cover, the optimized
  stages of every signal (nema_phase null rows are listed as stages S1, S2...) and the
  KPI page; the baseline's own stages are what the baseline endpoint serves.

The renderer's inputs are kept under ``report/`` (``current_plan.json``,
``optimization.json``). Without reportlab, for a run that exported no NEMA plan, or when
rendering fails, the endpoint answers 404 with the reason; a failed render leaves
``timing_report.error.json``, which :func:`has_report` reads.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from terasim_service.city.run_outputs import (
    NEMA_FILENAME,
    baseline_plan_payload,
    baseline_source,
    read_json,
    read_result,
    run_corridor_id,
    write_json_atomic,
)

logger = logging.getLogger(__name__)

REPORT_FILENAME = "timing_report.pdf"
REPORT_ERROR_FILENAME = "timing_report.error.json"
REPORT_INPUTS_DIR = "report"

#: Pages of a report whose current plan has no controller document to print.
SIGNALS_ONLY_SECTIONS = ("cover", "patterns", "kpi")

#: Where a static baseline came from -> (cover sentence, KPI page closing line).
_BASELINE_NOTES = {
    "sumo_tls": (
        "The current plan is the scenario's own signal programs (no controller timing "
        "report was provided); TeraSim re-timed it.",
        "Measured in TeraSim/SUMO on the demand window above; the current plan is the "
        "scenario's own signal programs.",
    ),
    "baseline_sheet": (
        "The current plan is the timing sheet provided with the scenario; TeraSim "
        "re-timed it.",
        "Measured in TeraSim/SUMO on the demand window above; the current plan is the "
        "timing sheet provided with the scenario.",
    ),
    "webster_init": (
        "No current plan was provided: the baseline is a plan computed from the measured "
        "demand (Webster), which TeraSim then re-timed.",
        "Measured in TeraSim/SUMO on the demand window above; the current plan is a plan "
        "computed from the measured demand.",
    ),
}
_GENERIC_NOTE = (
    "The current plan is the run's baseline plan; TeraSim re-timed it.",
    "Measured in TeraSim/SUMO on the demand window above; the current plan is the run's "
    "baseline plan.",
)


class ReportUnavailable(Exception):
    """The run's report cannot be served (the message says why)."""


@functools.lru_cache(maxsize=1)
def _missing_renderer() -> Optional[str]:
    if importlib.util.find_spec("reportlab") is None:
        return "the PDF renderer (reportlab) is not installed on this deployment"
    if importlib.util.find_spec("terasim_agent_skill") is None:
        return "the timing-report renderer (terasim-agent-skill) is not installed on this deployment"
    return None


def renderer_unavailable_reason() -> Optional[str]:
    """Why this deployment cannot render reports, or None when it can."""
    return _missing_renderer()


def load_renderer():
    """The timing-report module (imported on first use: it imports reportlab)."""
    return importlib.import_module("terasim_agent_skill.timing_report")


def has_report(run_dir: Path, *, completed: bool) -> bool:
    """Whether ``report.pdf`` can be served: rendered already, or renderable."""
    if not completed:
        return False
    run_dir = Path(run_dir)
    if (run_dir / REPORT_FILENAME).is_file():
        return True
    if renderer_unavailable_reason() is not None:
        return False
    if (run_dir / REPORT_ERROR_FILENAME).is_file():
        return False
    return (run_dir / NEMA_FILENAME).is_file()


def _nema_skip_reason(result: dict) -> str:
    block = result.get("nema_export") if isinstance(result, dict) else None
    if isinstance(block, dict) and block.get("exported") is False and block.get("reason"):
        return f"the run exported no NEMA plan: {block['reason']}"
    return "the run exported no NEMA plan"


def _tmc_names(result: dict) -> dict:
    """``{intersection_id: name}`` from the run's TMC file (best effort)."""
    tmc_json = (result.get("config") or {}).get("tmc_json")
    document = read_json(Path(str(tmc_json))) if tmc_json else None
    names = {}
    for item in (document or {}).get("intersections") or []:
        if isinstance(item, dict) and item.get("intersection_id") and item.get("name"):
            names[str(item["intersection_id"])] = str(item["name"])
    return names


def _report_intersections(result: dict, nema: dict) -> list[dict]:
    """The run's intersections in corridor order: id, name and position when known."""
    names = _tmc_names(result)
    intersections = []
    for item in result.get("resolved_corridor") or []:
        if not isinstance(item, dict):
            continue
        intersection_id = item.get("intersection_id") or item.get("ref") or item.get("tls_id")
        intersections.append({
            "intersection_id": intersection_id,
            "name": names.get(str(intersection_id)) or str(intersection_id),
            "lat": item.get("lat"),
            "lon": item.get("lon"),
            "tls_id": item.get("tls_id"),
        })
    if not intersections:
        intersections = [
            {"intersection_id": entry.get("tls_id"), "name": str(entry.get("tls_id")),
             "lat": None, "lon": None, "tls_id": entry.get("tls_id")}
            for entry in nema.get("intersections") or []
            if isinstance(entry, dict)
        ]
    return intersections


def _current_plan(
    payload: Optional[dict], result: dict, corridor_id: str, intersections: list[dict]
) -> tuple[dict, Optional[tuple]]:
    """(current plan document, sections to render; None = all)."""
    from terasim_city.plans.nema_plan import NEMA_PLAN_KIND

    if payload is not None and payload.get("kind") == NEMA_PLAN_KIND:
        stored, seen = [], set()
        for entry in payload.get("entries") or []:
            intersection = entry.get("intersection") if isinstance(entry, dict) else None
            if not isinstance(intersection, dict):
                continue
            key = intersection.get("intersection_id") or entry.get("tls_id")
            if key in seen:
                continue
            seen.add(key)
            stored.append(intersection)
        if stored:
            return (
                {"schema": "terasim.nema_plan.v1", "corridor_id": corridor_id,
                 "intersections": stored},
                None,
            )
    source = baseline_source(result, payload)
    cover_note, kpi_note = _BASELINE_NOTES.get(source, _GENERIC_NOTE)
    plan = {
        "schema": (payload or {}).get("schema") or "terasim.corridor_plan.v1",
        "corridor_id": corridor_id,
        "provenance": {
            "baseline_source": source,
            "current_plan_note": cover_note,
            "current_plan_kpi_note": kpi_note,
        },
        "intersections": [
            {**item, "phases": [], "patterns": [], "schedule": []} for item in intersections
        ],
    }
    return plan, SIGNALS_ONLY_SECTIONS


def build_report_inputs(
    run_dir: Path, result: dict, nema: dict, *, run_id: str, corridor_id: Optional[str] = None
) -> tuple[dict, dict, Optional[tuple]]:
    """(run document, current plan, sections) the renderer reads for this run."""
    from terasim_agent_skill.artifacts import _run_document

    from terasim_service.city.optimize_api import _direct_kpis

    config = result.get("config") if isinstance(result.get("config"), dict) else {}
    kpis = _direct_kpis(result, config.get("kpi_weights") or {})
    corridor = run_corridor_id(run_dir, result, corridor_id) or ""
    intersections = _report_intersections(result, nema)
    plan, sections = _current_plan(
        baseline_plan_payload(run_dir, result), result, corridor, intersections
    )
    document = _run_document(
        {"nema_plan": nema, "kpis": kpis},
        artifact_id=run_id,
        corridor_id=corridor,
        intersections=[
            {key: item.get(key) for key in ("intersection_id", "name", "lat", "lon")}
            for item in intersections
        ],
        plan=plan,
        now=datetime.now(timezone.utc),
    )
    document["result"]["config"].update(
        {key: config[key] for key in ("optimizer", "max_trials", "sim_end")
         if config.get(key) is not None}
    )
    if result.get("resolved_corridor"):
        document["result"]["resolved_corridor"] = result["resolved_corridor"]
    return document, plan, sections


def _record_failure(run_dir: Path, reason: str) -> None:
    try:
        write_json_atomic(
            run_dir / REPORT_ERROR_FILENAME,
            {"reason": reason, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
    except OSError:
        logger.warning("could not record the report failure of %s", run_dir, exc_info=True)


def render_run_report(
    run_dir: Path, *, run_id: str, corridor_id: Optional[str] = None
) -> Path:
    """The run's ``timing_report.pdf``, rendered now unless cached. Raises ReportUnavailable."""
    run_dir = Path(run_dir)
    pdf = run_dir / REPORT_FILENAME
    if pdf.is_file():
        return pdf
    missing = renderer_unavailable_reason()
    if missing is not None:
        raise ReportUnavailable(missing)
    result = read_result(run_dir)
    if result is None:
        raise ReportUnavailable("the run's result is no longer available")
    nema = read_json(run_dir / NEMA_FILENAME)
    if nema is None:
        raise ReportUnavailable(_nema_skip_reason(result))
    scratch = run_dir / f".{REPORT_FILENAME}.{uuid.uuid4().hex}"
    try:
        renderer = load_renderer()
        document, plan, sections = build_report_inputs(
            run_dir, result, nema, run_id=run_id, corridor_id=corridor_id
        )
        inputs = run_dir / REPORT_INPUTS_DIR
        inputs.mkdir(parents=True, exist_ok=True)
        plan_path = write_json_atomic(inputs / "current_plan.json", plan)
        opt_path = write_json_atomic(inputs / "optimization.json", document)
        optimized = renderer.OptimizedResult(document, source=opt_path.name)
        if not optimized.intersections:
            raise ValueError("the run's NEMA plan lists no intersection")
        renderer.build_report(
            plan, plan_path, optimized, opt_path, scratch, None, None, sections=sections
        )
        os.replace(scratch, pdf)
    except (Exception, SystemExit) as exc:  # the renderer exits on unusable input
        reason = f"the timing report could not be rendered: {exc}"
        logger.warning("report of run %s: %s", run_id, reason, exc_info=True)
        _record_failure(run_dir, reason)
        raise ReportUnavailable(reason) from exc
    finally:
        scratch.unlink(missing_ok=True)
    (run_dir / REPORT_ERROR_FILENAME).unlink(missing_ok=True)
    return pdf


__all__ = [
    "REPORT_ERROR_FILENAME",
    "REPORT_FILENAME",
    "ReportUnavailable",
    "build_report_inputs",
    "has_report",
    "load_renderer",
    "render_run_report",
    "renderer_unavailable_reason",
]
