"""Simulated signal-performance measures of a finished run (corridor_ui.sim_metrics.v1).

The trial selection and the document shape are checked without SUMO; one test re-runs a
real optimization of the bundled demo scenario and computes the document from it.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from terasim_city.evaluation.sim_metrics import (
    SIM_METRICS_SCHEMA,
    SimMetricsUnavailable,
    _corridor_tls_ids,
    _plan_trials,
    compute_sim_metrics,
    write_sim_metrics,
)
from tests.test_city.conftest import REPO_ROOT, requires_sumo_runtime, requires_sumolib

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"


def _trial(run_dir: Path, name: str) -> Path:
    trial = run_dir / "trials" / name
    trial.mkdir(parents=True)
    (trial / "simulation.sumocfg").write_text("<configuration/>", encoding="utf-8")
    return trial


def _result(run_dir: Path, **extra) -> dict:
    base = _trial(run_dir, "trial_0000")
    best = _trial(run_dir, "trial_0002")
    result = {
        "baseline": {"plan_id": "scenario_programs"},
        "best": {"plan_id": "bo_corr_0001", "trial_dir": str(best)},
        "trials": [
            {"plan_id": "scenario_programs", "trial_dir": str(base)},
            {"plan_id": "webster_init", "trial_dir": str(run_dir / "trials" / "trial_0001")},
            {"plan_id": "bo_corr_0001", "trial_dir": str(best)},
        ],
        "config": {"tls_ids": ["J1", "J2"]},
    }
    result.update(extra)
    return result


def test_the_best_and_the_baseline_trial_are_re_simulated(tmp_path: Path) -> None:
    result = _result(tmp_path)

    plans = _plan_trials(result, tmp_path, ("optimized", "baseline"))

    assert list(plans) == ["optimized", "baseline"]
    assert plans["optimized"] == ("bo_corr_0001", tmp_path / "trials" / "trial_0002")
    assert plans["baseline"] == ("scenario_programs", tmp_path / "trials" / "trial_0000")
    assert list(_plan_trials(result, tmp_path, ("baseline",))) == ["baseline"]
    # The tool's order (optimized first) whatever order the caller lists them in.
    assert list(_plan_trials(result, tmp_path, ("baseline", "optimized"))) == [
        "optimized", "baseline",
    ]


def test_the_baseline_trial_is_the_coarse_one_even_when_refine_re_ran_it(tmp_path: Path) -> None:
    result = _result(tmp_path)
    refine_dir = tmp_path / "trials_refine" / "trial_0000"
    result["trials"].append(
        {"plan_id": "scenario_programs", "trial_dir": str(refine_dir), "stage": "refine"}
    )

    plans = _plan_trials(result, tmp_path, ("baseline",))

    assert plans["baseline"][1] == tmp_path / "trials" / "trial_0000"


def test_a_refine_winner_is_the_optimized_plan(tmp_path: Path) -> None:
    winner_dir = tmp_path / "trials_refine" / "trial_0001"
    winner_dir.mkdir(parents=True)
    (winner_dir / "simulation.sumocfg").write_text("<configuration/>", encoding="utf-8")
    result = _result(
        tmp_path,
        export_source="refine",
        refine={
            "baseline": {"plan_id": "scenario_programs", "trial_dir": "unused"},
            "evaluations": [{"plan_id": "webster_init", "trial_dir": str(winner_dir)}],
            "winner_plan_id": "webster_init",
        },
    )

    plans = _plan_trials(result, tmp_path, ("optimized",))

    assert plans["optimized"] == ("webster_init", winner_dir)


def test_trial_dirs_recorded_under_another_root_are_found_in_the_run(tmp_path: Path) -> None:
    result = _result(tmp_path)
    moved = json.loads(
        json.dumps(result).replace(str(tmp_path), "/somewhere/else/7f3a")
    )

    plans = _plan_trials(moved, tmp_path, ("optimized", "baseline"))

    assert plans["optimized"][1] == tmp_path / "trials" / "trial_0002"
    assert plans["baseline"][1] == tmp_path / "trials" / "trial_0000"


def test_a_missing_trial_is_unavailable_with_a_reason(tmp_path: Path) -> None:
    result = _result(tmp_path)
    result["trials"] = [t for t in result["trials"] if t["plan_id"] != "scenario_programs"]

    with pytest.raises(SimMetricsUnavailable, match="scenario_programs"):
        _plan_trials(result, tmp_path, ("baseline",))


def test_a_run_without_its_result_file_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(SimMetricsUnavailable, match="corridor_optimization_result.json"):
        compute_sim_metrics(tmp_path)


def test_signals_come_from_the_resolved_corridor_when_none_were_configured() -> None:
    result = {
        "config": {"tls_ids": []},
        "resolved_corridor": [{"tls_id": "J2"}, {"tls_id": "J1"}, {"tls_id": "J2"}],
    }
    assert _corridor_tls_ids(result) == ["J2", "J1"]
    assert _corridor_tls_ids({"config": {"tls_ids": ["J1", "J3"]}}) == ["J1", "J3"]


def test_the_document_is_written_like_the_tool_writes_it(tmp_path: Path) -> None:
    doc = {"schema": SIM_METRICS_SCHEMA, "plans": {}}
    path = tmp_path / "sim_metrics.json"

    write_sim_metrics(doc, path)

    assert path.read_text(encoding="utf-8") == json.dumps(doc, indent=1) + "\n"


METHOD_KEYS = {
    "description", "warmup_s", "end_s", "sample_s", "stop_bar_zone_m", "approach_region_m",
    "arrival", "saturation_flow_veh_h_lane", "gap_out_s", "min_green_s", "red_window_s",
    "split_failure_occupancy",
}
APPROACH_KEYS = {
    "lanes", "volume_veh_h", "arrivals", "aog_pct", "aor_pct", "gor_pct", "ror5_pct",
    "split_failures", "split_failures_per_hr", "cycles", "queue_veh", "queue_censored",
    "phase_util_pct", "dos", "demand_veh_h", "sat_flow_veh_h_lane", "sat_flow_source",
    "control_delay_s", "stops_per_veh", "vehicles", "green_s", "cycle_s",
}
INTERSECTION_KEYS = {
    "control_delay_s", "stops_per_veh", "volume_veh_h", "dos", "aog_pct", "aor_pct",
    "gor_pct", "ror5_pct", "split_failures_per_hr", "queue_veh", "queue_censored",
    "phase_util_pct", "vehicles", "approaches", "intersection_id",
}


@pytest.mark.integration
@pytest.mark.requires_sumo
@requires_sumolib
@requires_sumo_runtime
def test_the_demo_scenario_run_yields_a_full_document(tmp_path: Path) -> None:
    """A real (small) optimization of the bundled demo scenario, then both re-runs."""
    from terasim_city.optimize.base import Budget
    from terasim_city.orchestration.corridor_optimization import (
        CorridorOptimizationConfig,
        run_corridor_optimization,
    )

    run_dir = tmp_path / "0123abcd"
    cfg = CorridorOptimizationConfig(
        tmc_json=DEMO_DIR / "placeholder_tmc.json",
        base_sumocfg=DEMO_DIR / "demo_scenario.sumocfg",
        output_dir=run_dir,
        tls_ids=("J1", "J2", "J3"),
        optimizer="exhaustive",
        stage_mode="per_approach",
        flow_source="scenario_routes",
        counts_source="scenario_routes",
        seed_from_scenario_programs=True,
        budget=Budget(max_trials=1, patience=0),
        sim_end=600.0,
        n_workers=2,
    )
    result = run_corridor_optimization(cfg)

    doc = compute_sim_metrics(run_dir, corridor_id="demo_scenario")

    assert doc["schema"] == SIM_METRICS_SCHEMA == "corridor_ui.sim_metrics.v1"
    assert (doc["corridor_id"], doc["run_id"]) == ("demo_scenario", "0123abcd")
    assert set(doc["method"]) == METHOD_KEYS
    assert (doc["method"]["warmup_s"], doc["method"]["end_s"]) == (300.0, 600.0)
    assert doc["corridor_order"] == ["J1", "J2", "J3"]
    assert doc["mainline"] == ["EB", "WB"]
    assert list(doc["plans"]) == ["optimized", "baseline"]
    assert doc["plans"]["optimized"]["plan_id"] == result["best"]["plan_id"]
    assert doc["plans"]["baseline"]["plan_id"] == result["baseline"]["plan_id"]
    for plan in doc["plans"].values():
        corridor = plan["corridor"]
        assert 0.0 <= corridor["aog_pct"] <= 100.0
        assert math.isclose(corridor["aog_pct"] + corridor["aor_pct"], 100.0, abs_tol=0.11)
        assert corridor["dos_max_tls"] in {"J1", "J2", "J3"}
        assert set(corridor["travel_time_s"]) == {"EB", "WB"}
        assert set(plan["intersections"]) == {"J1", "J2", "J3"}
        for tls_id, intersection in plan["intersections"].items():
            assert set(intersection) == INTERSECTION_KEYS
            assert intersection["intersection_id"] == tls_id  # the demo's TMC-less ids
            assert intersection["vehicles"] > 0
            for approach in intersection["approaches"].values():
                assert set(approach) == APPROACH_KEYS
        assert set(plan["phase0_starts_s"]) == {"J1", "J2", "J3"}
    # The re-runs leave nothing behind in the run directory but what was there.
    assert not list(run_dir.glob(".sim_metrics*"))
