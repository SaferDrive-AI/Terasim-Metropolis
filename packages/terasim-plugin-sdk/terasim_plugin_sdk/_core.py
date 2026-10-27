"""The SDK's single bridge to terasim-city internals.

Everything the SDK takes from terasim-city that is not in that package's public
exports is reached through this module, so the coupling sits in one place and the
SDK's test suite exercises all of it. The SDK depends on a matching terasim-city
release (see its ``pyproject.toml``). Plugins never import this module.

The functions below call the core's own implementations, so a plugin built with the
SDK times, scores and exports plans exactly like ``run_corridor_optimization``.
Imports are local to each function: the optimization module loads the search
libraries, which ``terasim-plugin list`` does not need.
"""

from __future__ import annotations

from pathlib import Path


def kpi_metrics() -> "dict[str, str]":
    """KPI weight key -> the MOE metric it weights (``"stops"`` -> ``"stops_per_veh"``)."""
    from terasim_city.evaluation.moe import _WEIGHT_METRICS

    return dict(_WEIGHT_METRICS)


def quality_thresholds_off() -> dict:
    """The validity gates every optimization run uses: none (congestion is scored instead)."""
    from terasim_city.optimize.evaluator import QUALITY_KEYS

    return {key: None for key in QUALITY_KEYS}


def plan_timing(plan) -> dict:
    """Per-TLS ``cycle_s`` / ``offsets_s`` (and ``greens_s`` for static plans) of a plan."""
    from terasim_city.orchestration.corridor_optimization import _timing

    return _timing(plan)


def export_nema_or_reason(
    plan, *, corridor_id: str, out_dir: Path
) -> "tuple[dict | None, str | None]":
    """Write ``nema_plan.json`` like the core does: ``(payload, None)`` or ``(None, reason)``."""
    from terasim_city.orchestration.corridor_optimization import _export_nema

    return _export_nema(plan, corridor_id=corridor_id, out_dir=Path(out_dir))


def unserved_movements(plan, net_file: Path) -> "dict[str, list[str]]":
    """Per TLS, the vehicle movements ``plan`` never lets move (``{}`` when none)."""
    from terasim_city.orchestration.corridor_optimization import _unserved_movements

    return _unserved_movements(plan, Path(net_file), None)


def _config(*, tmc_json: Path, base_sumocfg: Path, output_dir: Path, **fields):
    from terasim_city.orchestration.corridor_optimization import CorridorOptimizationConfig

    return CorridorOptimizationConfig(
        tmc_json=Path(tmc_json),
        base_sumocfg=Path(base_sumocfg),
        output_dir=Path(output_dir),
        **fields,
    )


def timing_sheet_plan(
    sheet: Path, *, corridor, net_file: Path, tmc_json: Path, base_sumocfg: Path, output_dir: Path
):
    """The timing sheet's plan for the corridor's signals, mapped exactly as the core maps it."""
    from terasim_city.orchestration.corridor_optimization import _load_baseline_plan

    cfg = _config(
        tmc_json=tmc_json,
        base_sumocfg=base_sumocfg,
        output_dir=output_dir,
        baseline_sheet=Path(sheet),
    )
    return _load_baseline_plan(cfg, corridor, Path(net_file))


def stored_nema_plan(
    *,
    plan_data_root: Path,
    plan_time: "str | None",
    corridor,
    tmc,
    tmc_json: Path,
    base_sumocfg: Path,
    output_dir: Path,
):
    """The corridor's stored NEMA plans at ``plan_time``: ``(plan, None)`` or ``(None, reason)``.

    The core's seed assembly: intersections scheduled "free" at that time keep their
    official structure with demand-computed splits.
    """
    from terasim_city.orchestration.corridor_optimization import _load_stored_nema_seed

    cfg = _config(
        tmc_json=tmc_json,
        base_sumocfg=base_sumocfg,
        output_dir=output_dir,
        plan_data_root=Path(plan_data_root),
        plan_time=plan_time,
    )
    return _load_stored_nema_seed(cfg, corridor, tmc.corridor_id, tmc)
