"""Files a finished optimization run keeps next to the optimizer's own output.

Every run, whatever started it, works in ``output_root/<run_id>/``: the optimizer
writes ``corridor_optimization_result.json``, ``trials.jsonl``, ``nema_plan.json``
(when it exported one) and one directory per trial (``trials/trial_NNNN/`` with the
trial's ``plan.json`` and ``simulation.sumocfg``). When the run completes, the
service adds ``baseline_nema.json``: the run's baseline plan in the
``terasim.nema_timing.v1`` shape of ``nema_plan.json``, so the baseline endpoint
is a file read. It is rebuilt from the baseline trial when it is missing.

The baseline is the plan the optimization measured itself against
(``result["baseline"]``, recorded in the ledger with its trial):

* seeded from stored NEMA plans (``seed.source == "stored_nema"``): the stored plan as
  the run used it — each signal's pattern for the requested time — exported like an
  optimized NEMA plan (pattern ids, descriptions, valid windows);
* any other seed (``baseline_sheet``, ``webster_init``, ``sumo_tls``): the static plan,
  exported loop-aware by :func:`terasim_city.nema.export.export_nema` (stage rows,
  ``nema_phase: null`` unless the plan numbers its phases). Signals without a common
  cycle are exported one by one, each with its own ``cycle_s`` (the coordination
  cycle is then the longest), as stored NEMA exports do.

A baseline the exporter refuses (a program without a green phase, stage splits that
do not add up to the cycle) or whose trial is gone raises
:class:`BaselineUnavailable` with the reason.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RESULT_FILENAME = "corridor_optimization_result.json"
NEMA_FILENAME = "nema_plan.json"
BASELINE_NEMA_FILENAME = "baseline_nema.json"
PLAN_JSON_FILENAME = "plan.json"
#: Trial directories live under one of these inside the run directory.
_TRIAL_ROOTS = ("trials", "trials_refine")


class BaselineUnavailable(Exception):
    """The run's baseline cannot be expressed as NEMA timing (the message says why)."""


def read_json(path: Path) -> Optional[dict]:
    """A JSON object from ``path``, or None when it is missing, unreadable or not an object."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_result(run_dir: Path) -> Optional[dict]:
    """The run's result JSON, or None."""
    return read_json(Path(run_dir) / RESULT_FILENAME)


def write_json_atomic(path: Path, document) -> Path:
    """Write ``document`` (indent 2) to ``path`` through a temporary sibling + rename.

    The temporary file is created like any other file (the process umask applies),
    so the result is as readable as the optimizer's own files in the run directory.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def finalize_run_outputs(run_dir: Path, result: dict) -> None:
    """What every completed run gets before it is recorded as completed. Never raises.

    The result file (a stand-in optimizer may only return its result) and the
    baseline copy (:func:`persist_baseline_nema`).
    """
    run_dir = Path(run_dir)
    try:
        if not (run_dir / RESULT_FILENAME).is_file():
            run_dir.mkdir(parents=True, exist_ok=True)
            write_json_atomic(run_dir / RESULT_FILENAME, result)
    except Exception:  # noqa: BLE001 - the run itself succeeded
        logger.warning("could not write the result file of %s", run_dir, exc_info=True)
    persist_baseline_nema(run_dir, result)


# ---------------------------------------------------------------------------
# Baseline plan
# ---------------------------------------------------------------------------

def _locate_trial(run_dir: Path, recorded) -> Optional[Path]:
    """The trial directory as recorded, else the same trial inside ``run_dir``."""
    if not recorded:
        return None
    path = Path(str(recorded))
    if (path / PLAN_JSON_FILENAME).is_file():
        return path
    parts = path.parts
    for anchor in _TRIAL_ROOTS:
        if anchor in parts:
            last = len(parts) - 1 - parts[::-1].index(anchor)
            candidate = Path(run_dir).joinpath(*parts[last:])
            if (candidate / PLAN_JSON_FILENAME).is_file():
                return candidate
    return None


def _baseline_plan_json(run_dir: Path, result: dict) -> tuple[str, dict]:
    """(plan id, ``plan.json``) of the baseline: the trial of its first (coarse) record."""
    plan_id = (result.get("baseline") or {}).get("plan_id")
    if not plan_id:
        raise BaselineUnavailable("the run's result names no baseline plan")
    for record in result.get("trials") or []:
        if record.get("plan_id") == plan_id and "stage" not in record:
            trial_dir = _locate_trial(run_dir, record.get("trial_dir"))
            if trial_dir is not None:
                plan = read_json(trial_dir / PLAN_JSON_FILENAME)
                if plan is not None:
                    return str(plan_id), plan
            break
    raise BaselineUnavailable(
        f"the baseline plan {plan_id!r} has no trial with a {PLAN_JSON_FILENAME} in this run"
    )


def run_corridor_id(run_dir: Path, result: dict, fallback: Optional[str] = None) -> Optional[str]:
    """The corridor id the run's exports carry: its NEMA export's, else its TMC file's."""
    for document in (
        read_json(Path(run_dir) / NEMA_FILENAME),
        read_json(Path(str((result.get("config") or {}).get("tmc_json") or "")))
        if (result.get("config") or {}).get("tmc_json")
        else None,
    ):
        value = (document or {}).get("corridor_id")
        if isinstance(value, str) and value:
            return value
    return fallback


def baseline_source(result: dict, plan: Optional[dict] = None) -> str:
    """Where the baseline came from: the result's seed source, else the plan's provenance."""
    source = (result.get("seed") or {}).get("source")
    if not source and plan is not None:
        source = (plan.get("provenance") or {}).get("source")
    return str(source or "unknown")


def _export_static(plan, *, corridor_id: str, out_dir: Path) -> dict:
    """A static plan as NEMA timing: one coordinated sheet, or per signal without a common cycle."""
    from terasim_city.nema.export import NEMA_TIMING_SCHEMA, export_nema
    from terasim_city.plans.model import CorridorPlan

    try:
        plan.common_cycle_s()
    except ValueError:
        pass
    else:
        return export_nema(plan, corridor_id=corridor_id, out_dir=out_dir)

    intersections = []
    handoff_ready = True
    for signal_plan in plan.plans:
        single = CorridorPlan(plan.plan_id, (signal_plan,), plan.provenance)
        exported = export_nema(single, corridor_id=corridor_id, out_dir=out_dir)
        handoff_ready = handoff_ready and bool(exported["handoff_ready"])
        entry = dict(exported["intersections"][0])
        phases = entry.pop("phases")
        entry["cycle_s"] = float(signal_plan.cycle_s)
        entry["phases"] = phases
        intersections.append(entry)
    return {
        "schema": NEMA_TIMING_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corridor_id": corridor_id,
        "handoff_ready": handoff_ready,
        "coordination": {
            "cycle_length_s": max(float(p.cycle_s) for p in plan.plans),
            "reference_intersection": plan.plans[0].tls_id,
        },
        "intersections": intersections,
    }


def build_baseline_nema(
    run_dir: Path, result: dict, *, corridor_id: Optional[str] = None
) -> dict:
    """The run's baseline plan as a ``terasim.nema_timing.v1`` document (see module doc).

    ``corridor_id`` is used only when neither the run's NEMA export nor its TMC file
    names one. Raises :class:`BaselineUnavailable` with the reason.
    """
    from terasim_city.nema.export import export_nema_plan
    from terasim_city.plans.model import CorridorPlan
    from terasim_city.plans.nema_plan import NEMA_PLAN_KIND, NemaCorridorPlan

    run_dir = Path(run_dir)
    plan_id, payload = _baseline_plan_json(run_dir, result)
    corridor = run_corridor_id(run_dir, result, corridor_id) or ""
    try:
        with tempfile.TemporaryDirectory(prefix=".baseline_", dir=run_dir) as scratch:
            if payload.get("kind") == NEMA_PLAN_KIND:
                plan = NemaCorridorPlan.from_json_dict(payload)
                return export_nema_plan(plan, corridor_id=corridor, out_dir=Path(scratch))
            plan = CorridorPlan.from_json_dict(payload)
            return _export_static(plan, corridor_id=corridor, out_dir=Path(scratch))
    except (ValueError, KeyError, TypeError) as exc:
        raise BaselineUnavailable(
            f"the baseline plan {plan_id!r} cannot be written as NEMA timing: {exc}"
        ) from exc


def persist_baseline_nema(
    run_dir: Path, result: dict, *, corridor_id: Optional[str] = None
) -> Optional[dict]:
    """Write ``baseline_nema.json`` into the run directory; the document, or None. Never raises."""
    try:
        document = build_baseline_nema(run_dir, result, corridor_id=corridor_id)
        write_json_atomic(Path(run_dir) / BASELINE_NEMA_FILENAME, document)
        return document
    except BaselineUnavailable as exc:
        logger.info("no baseline NEMA copy for %s: %s", run_dir, exc)
    except Exception:  # noqa: BLE001 - the run itself succeeded
        logger.warning("could not write the baseline NEMA copy of %s", run_dir, exc_info=True)
    return None


def load_baseline_nema(
    run_dir: Path, result: dict, *, corridor_id: Optional[str] = None
) -> dict:
    """The copy written at completion, else a fresh build (cached for the next read)."""
    document = read_json(Path(run_dir) / BASELINE_NEMA_FILENAME)
    if document is not None:
        return document
    document = build_baseline_nema(run_dir, result, corridor_id=corridor_id)
    try:
        write_json_atomic(Path(run_dir) / BASELINE_NEMA_FILENAME, document)
    except OSError:
        logger.warning("could not cache the baseline NEMA copy of %s", run_dir, exc_info=True)
    return document


def baseline_plan_payload(run_dir: Path, result: dict) -> Optional[dict]:
    """The baseline trial's ``plan.json`` (None when it is gone)."""
    try:
        return _baseline_plan_json(Path(run_dir), result)[1]
    except BaselineUnavailable:
        return None


__all__ = [
    "BASELINE_NEMA_FILENAME",
    "BaselineUnavailable",
    "NEMA_FILENAME",
    "RESULT_FILENAME",
    "baseline_plan_payload",
    "baseline_source",
    "build_baseline_nema",
    "finalize_run_outputs",
    "load_baseline_nema",
    "persist_baseline_nema",
    "read_json",
    "read_result",
    "run_corridor_id",
    "write_json_atomic",
]
