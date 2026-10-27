"""Spawn-safe single-trial evaluation task for corridor plan optimization.

``evaluate_plan_task`` is a module-level function taking/returning plain
JSON-compatible dicts so it can cross a ``multiprocessing`` spawn boundary.
Everything that (transitively) imports ``terasim`` — and thereby resolves
libsumo — is imported INSIDE the function: each pool child gets its own SUMO
instance and the parent process never loads libsumo through this module.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from terasim_city.maps.base import MANIFEST_NAME as _MAP_PACKAGE_MANIFEST

logger = logging.getLogger(__name__)

__all__ = ["evaluate_plan_task", "moe_validity_errors"]

QUALITY_KEYS = ("emergency_stops", "teleports", "collisions")

#: CUDA caching-allocator setting of every "cnde" trial, unless the deployment set
#: its own: a trial's GPU memory grows from ~1.5 GB to ~6 GB over a long run, and
#: expandable segments keep that growth from fragmenting into an out-of-memory error.
CUDA_ALLOC_CONF_ENV_VAR = "PYTORCH_CUDA_ALLOC_CONF"
CUDA_ALLOC_CONF = "expandable_segments:True"


def moe_validity_errors(moe: dict) -> list[str]:
    """Validity criteria on an extracted MOE (anti-gaming guard).

    A trial where NO vehicle ever departed produced no usable traffic: a plan
    that holds every vehicle at the insertion gate must be an invalid trial
    (score ``inf`` downstream), never a competitive one. Gating is
    deliberately ONLY on all-undeparted — ``completion_ratio == 0`` is
    legitimate for short runs where vehicles depart but nothing finishes, so
    it is NOT gated on.
    """
    errors: list[str] = []
    if moe["vehicles_undeparted"] == moe["vehicles_total"]:
        errors.append("no vehicle departed")
    return errors


def evaluate_plan_task(task: dict) -> dict:
    """Materialize, simulate, quality-gate and MOE-score one corridor plan.

    ``task`` keys (all JSON-compatible): ``plan`` (CorridorPlan.to_json_dict, or
    NemaCorridorPlan.to_json_dict carrying ``"kind": "nema"``),
    ``scenario_sumocfg``, ``net_file``, ``trial_dir`` (str paths), ``sim_end``
    (float), ``stalls`` (list of
    {latitude, longitude}), ``quality_thresholds`` ({emergency_stops,
    teleports, collisions}), ``vehicle_behavior`` (registry name, required — set by
    :class:`~terasim_city.optimize.batch.BatchEvaluator`; resolved in the
    child via :func:`terasim_city.factories.resolve_vehicle_factory`).
    Scoring is the parent's job; the evaluator returns the MOE only. Besides the
    all-demand MOE it carries two secondary keys that never enter the score:
    ``junction_delay_s`` (``{tls_id: s/veh | None}``) and
    ``corridor_junction_delay_s`` (their mean, ``None`` without data), the
    edge-based delay computed from SUMO's own ``--edgedata-output``
    (``sim_output/edgedata.xml``). When that file cannot be read the values are
    ``None`` and the trial stays valid.

    Returns ``{"plan_id", "moe", "valid", "quality_errors", "trial_dir",
    "kpi"}`` on a completed run (``valid`` False when the quality gate fails),
    or ``{"plan_id", "valid": False, "error", "trial_dir"}`` when ANY
    exception occurs — a worker must report failure as data, never crash the
    pool.
    """
    trial_dir = Path(task.get("trial_dir", "."))
    plan_payload = task.get("plan")
    plan_id = (
        str(plan_payload.get("plan_id", "<unknown>"))
        if isinstance(plan_payload, dict)
        else "<unknown>"
    )

    if str(task.get("vehicle_behavior", "")) != "cnde":
        try:
            return _evaluate(task, trial_dir)
        except Exception as exc:  # noqa: BLE001 — contract: exceptions become data
            return {
                "plan_id": plan_id,
                "valid": False,
                "error": str(exc),
                "trial_dir": str(trial_dir),
            }

    # cnde: the allocator setting must be in place before anything in this
    # process initializes CUDA (pool workers start without it unless the
    # deployment exported it).
    os.environ.setdefault(CUDA_ALLOC_CONF_ENV_VAR, CUDA_ALLOC_CONF)

    # cnde: per-trial log isolation. The ported CNDE/NDE aux logs open
    # cwd-relative "output"/"outputs" files with mode "w" (not modified here —
    # terasim_nde_nade.envs.nnde:148-152/396-399,
    # terasim_nde_nade.envs.nde:231-237/479-484); without an isolated cwd,
    # every concurrent trial in a shared cwd clobbers the same files. Pool
    # workers run with maxtasksperchild=1 (one trial per process), so a chdir
    # for the whole evaluation is safe; inline mode restores the previous cwd
    # in `finally`.
    #
    # Every filesystem path in `task` must be made absolute BEFORE the chdir
    # — a relative path in the original task dict would otherwise silently
    # re-resolve against the NEW cwd inside `_evaluate`.
    trial_dir = trial_dir.resolve()
    trial_dir.mkdir(parents=True, exist_ok=True)
    resolved_task = dict(task)
    for key in ("net_file", "scenario_sumocfg", "trial_dir", "map_dir"):
        value = resolved_task.get(key)
        if value:
            resolved_task[key] = str(Path(value).resolve())
    previous_cwd = os.getcwd()
    os.chdir(trial_dir)
    try:
        return _evaluate(resolved_task, trial_dir)
    except Exception as exc:  # noqa: BLE001 — contract: exceptions become data
        return {
            "plan_id": plan_id,
            "valid": False,
            "error": str(exc),
            "trial_dir": str(trial_dir),
        }
    finally:
        os.chdir(previous_cwd)


def _resolve_cnde_map_dir(task: dict) -> Path:
    """Resolve the MapStore package directory the "cnde" behavior needs.

    CNDE artifacts (``cnde_map.pkl`` / ``cnde_id_mapping.pkl``, see
    :func:`terasim_city.maps.cnde_artifacts.ensure_cnde_artifacts`) are
    generated/cached inside the map's own MapStore package directory, keyed
    on its ``net.net.xml``. Two ways to find it:

    1. Preferred: ``task["map_dir"]``, set by the orchestration layer (see
       ``orchestration.corridor_optimization.run_corridor_optimization``)
       from the ``MapPackage.root`` it already resolved — correct for both
       map sources (OSM-converted packages live entirely inside the store;
       prebuilt packages keep their net file wherever the submitted sumocfg
       pointed, which ``MapPackage.root`` also captures).
    2. Fallback: the trial's ``net_file`` happens to sit directly inside a
       MapStore package (its parent directory carries
       ``map_package.json``) — true for an OSM-sourced package, or a
       ``base_sumocfg`` pointed straight at a stored package's own net file.
       NOT true for a prebuilt package ingested from an external scenario
       directory: its manifest lives in the store, its net file does not.

    Raises:
        RuntimeError: Neither resolves to a usable directory.
    """
    map_dir = task.get("map_dir")
    if map_dir:
        return Path(map_dir)
    net_file = task.get("net_file")
    if net_file:
        candidate = Path(net_file).parent
        if (candidate / _MAP_PACKAGE_MANIFEST).exists():
            return candidate
    raise RuntimeError(
        "vehicle_behavior 'cnde' needs a MapStore package directory to build "
        "CNDE artifacts (cnde_map.pkl / cnde_id_mapping.pkl), but this task "
        "carries no usable map directory: task['map_dir'] was not set by the "
        "orchestration layer, and the trial's net_file "
        f"({net_file!r}) is not inside a MapStore package (no "
        f"{_MAP_PACKAGE_MANIFEST} next to it). Pass an explicit base_sumocfg "
        "that resolves to a stored map package, or run through "
        "terasim_city.orchestration.corridor_optimization, which derives "
        "map_dir automatically for an auto-selected map."
    )


DEFAULT_TRIAL_SEED = 42


def _sumocfg_seed(trial_sumocfg: Path) -> int:
    """The ``<random_number><seed value=.../>`` of the trial sumocfg, else 42."""
    try:
        import xml.etree.ElementTree as ET

        node = ET.parse(trial_sumocfg).getroot().find("./random_number/seed")
        if node is not None and node.get("value") is not None:
            return int(float(node.get("value")))
    except Exception:  # noqa: BLE001 -- a seed is a nicety, never a failure
        pass
    return DEFAULT_TRIAL_SEED


def _seed_trial_rngs(seed: int) -> None:
    """Seed python/numpy/torch (CPU + CUDA) for one trial; torch stays optional."""
    import random

    import numpy as np

    random.seed(seed)
    np.random.seed(seed)
    try:
        import torch

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except Exception:  # noqa: BLE001 -- no torch, or no CUDA: nothing to seed
        pass


def _evaluate(task: dict, trial_dir: Path) -> dict:
    # Spawn-safety: terasim (and with it libsumo) must only ever be imported
    # in the child process, so all heavy imports live here, not at module top.
    from terasim.logger.infoextractor import InfoExtractor
    from terasim.simulator import Simulator
    from terasim_nde_nade.adversity import StalledObjectAdversity

    from terasim_city.envs.corridor_env import (
        STALL_END_TIME,
        STALL_OBJECT_TYPE,
        CorridorSignalEnv,
    )
    from terasim_city.evaluation.diagnostics import (
        build_backlog,
        build_incident,
        count_warnings,
    )
    from terasim_city.evaluation.junction_delay import junction_delay_from_edgedata
    from terasim_city.evaluation.moe import extract_moe
    from terasim_city.factories import resolve_vehicle_factory
    from terasim_city.plans.materialize import materialize_plan
    from terasim_city.plans.model import CorridorPlan
    from terasim_city.plans.nema_plan import (
        NEMA_PLAN_KIND,
        NemaCorridorPlan,
        materialize_nema_plan,
    )

    # Two plan kinds share the whole evaluation machinery; only the plan
    # representation and its materializer differ.
    if task["plan"].get("kind") == NEMA_PLAN_KIND:
        plan = NemaCorridorPlan.from_json_dict(task["plan"])
        trial_sumocfg = materialize_nema_plan(
            plan, Path(task["scenario_sumocfg"]), trial_dir
        )
        programs = plan.programs()
    else:
        plan = CorridorPlan.from_json_dict(task["plan"])
        trial_sumocfg = materialize_plan(
            plan, Path(task["scenario_sumocfg"]), trial_dir
        )
        programs = {signal_plan.tls_id: plan.plan_id for signal_plan in plan.plans}

    # Stall adversities are built inside the child (spawn safety). The
    # coordinates arrive ALREADY snapped onto real vehicle lanes — the
    # orchestration resolves incident geometry once, before any trial.
    stalls = list(task.get("stalls") or [])
    adversities = [
        StalledObjectAdversity(
            placement_mode="latlon_degree",
            lat=stall["latitude"],
            lon=stall["longitude"],
            degree=0.0,
            start_time=0.0,
            end_time=STALL_END_TIME,
            object_type=STALL_OBJECT_TYPE,
        )
        for stall in stalls
    ]

    # Resolved in the child: behavior factories may carry heavy runtime
    # dependencies (e.g. CNDE inference) that must never load in the parent.
    vehicle_behavior = str(task["vehicle_behavior"])
    vehicle_factory = resolve_vehicle_factory(vehicle_behavior)

    sim_output_dir = trial_dir / "sim_output"
    # Simulator's "tripinfo" handling already passes
    # --tripinfo-output.write-unfinished; repeating it here would abort SUMO
    # with "A value ... was already set", so only write-undeparted is added.
    additional_sumo_args = ["--tripinfo-output.write-undeparted", "true"]
    # SUMO's own edge data (one aggregated interval over the run) feeds the
    # secondary per-junction delay metric below.
    edgedata_path = sim_output_dir / "edgedata.xml"
    additional_sumo_args = additional_sumo_args + ["--edgedata-output", str(edgedata_path)]

    if vehicle_behavior == "cnde":
        # Heavy/GPU imports live here, only on the cnde path.
        from terasim_city.envs.cnde_corridor_env import CndeCorridorSignalEnv
        from terasim_city.factories.cnde_behavior import build_trial_nnde_simulator

        # Every CNDE trial starts from the same RNG state, whatever process it
        # runs in: with csim's id ordering made hash-independent this makes a
        # plan's evaluation reproducible across processes, which is what lets
        # the refine stage compare candidates evaluated in parallel workers.
        _seed_trial_rngs(_sumocfg_seed(trial_sumocfg))

        map_dir = _resolve_cnde_map_dir(task)
        nnde_simulator, id_mapping_pkl = build_trial_nnde_simulator(
            map_dir, "terasim-cnde"
        )
        env = CndeCorridorSignalEnv(
            programs=programs,
            adversities=adversities,
            sim_end=float(task["sim_end"]),
            nnde_simulator=nnde_simulator,
            map_id_mapping=str(id_mapping_pkl),
            vehicle_factory=vehicle_factory,
            info_extractor=InfoExtractor,
        )
        # CLI overrides the sumocfg's own step-length; CNDE's trajectory
        # cadence (5 TeraSim steps = 0.5s) assumes a 0.1s SUMO step.
        additional_sumo_args = additional_sumo_args + ["--step-length", "0.1"]
    else:
        env = CorridorSignalEnv(
            programs=programs,
            adversities=adversities,
            sim_end=float(task["sim_end"]),
            vehicle_factory=vehicle_factory,
            info_extractor=InfoExtractor,
        )

    sim = Simulator(
        sumo_net_file_path=Path(task["net_file"]),
        sumo_config_file_path=trial_sumocfg,
        num_tries=10,
        gui_flag=False,
        output_path=sim_output_dir,
        sumo_output_file_types=["tripinfo"],
        additional_sumo_args=additional_sumo_args,
    )
    sim.bind_env(env)
    sim.run()

    errors: list[str] = []
    try:
        counts = count_warnings(sim_output_dir / "run.log")
    except FileNotFoundError as exc:
        errors.append(str(exc))
        counts = {key: 0 for key in QUALITY_KEYS}
    thresholds = dict(task["quality_thresholds"])
    for name in QUALITY_KEYS:
        threshold = thresholds.get(name, 0)
        if threshold is None:
            continue  # no gate: the condition shows up in the score, not validity
        if counts.get(name, 0) > int(threshold):
            errors.append(f"{name}={counts[name]} exceeds threshold {threshold}")

    # extract_moe also gates the tripinfo file itself: missing / unparseable /
    # entry-less raises ValueError, which the caller turns into an error result.
    moe = extract_moe(sim_output_dir / "tripinfo.xml")
    errors.extend(moe_validity_errors(moe))

    # Secondary, edge-based per-junction delay (mean timeLoss/left over each
    # TLS's incoming edges). Never part of the score, and it must never fail
    # a trial: an unreadable edge-data file degrades to None values.
    tls_ids = list(programs)
    try:
        junction = junction_delay_from_edgedata(
            edgedata_path, Path(task["net_file"]), tls_ids
        )
    except Exception as exc:  # noqa: BLE001 — secondary metric must never fail a trial
        logger.warning("junction delay unavailable for %s: %s", trial_dir, exc)
        junction = {tls_id: None for tls_id in tls_ids}
    moe = dict(moe)
    moe["junction_delay_s"] = junction
    finite = [v for v in junction.values() if v is not None]
    moe["corridor_junction_delay_s"] = (sum(finite) / len(finite)) if finite else None

    valid = not errors
    kpi = {
        "valid": valid,
        "signal_plan": {"plan_id": plan.plan_id, "programs": programs},
        "moe": moe,
        "quality_gate": {
            "passed": valid,
            "counts": dict(counts),
            "errors": list(errors),
        },
        "diagnostics": {
            "backlog": build_backlog(env),
            # Same incident record the old signal-evaluation kpi carries:
            # placed stall edges/lanes zipped with the SNAPPED geometries
            # (fails loud on a placement-count mismatch -> invalid trial).
            "incident": build_incident(env, stalls),
        },
        "run": {
            "sim_end": float(task["sim_end"]),
            "trial_sumocfg": str(trial_sumocfg),
        },
    }
    (trial_dir / "kpi.json").write_text(
        json.dumps(kpi, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "plan_id": plan.plan_id,
        "moe": moe,
        "valid": valid,
        "quality_errors": errors,
        "trial_dir": str(trial_dir),
        "kpi": kpi,
    }
