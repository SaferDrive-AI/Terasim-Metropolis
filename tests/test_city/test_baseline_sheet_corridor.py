"""A corridor's timing sheet seeds runs over part of that corridor.

The scheduled entry of a corridor may name a ``baseline_sheet`` covering all of its
signals while a request covers only some of them: one intersection (the synchronous
on-demand path) or a from/to pair inside the corridor. Such a run takes the sheet's
entries for its own signals. A signal the sheet has no entry for is a
:class:`TimingSheetCoverageError` naming it. Evaluation is synthetic (no SUMO).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from terasim_city.optimize.base import Budget
from terasim_city.optimize.batch import INLINE_EVAL_ENV_VAR
from terasim_city.orchestration.corridor_optimization import (
    CorridorOptimizationConfig,
    run_corridor_optimization,
)
from terasim_city.plans.baseline_sheet import TimingSheetCoverageError
from tests.fixtures.tmc_templates import baseline_sheet, demo_tmc, write_tmc
from tests.test_city.conftest import REPO_ROOT, requires_sumolib

DEMO_CFG = REPO_ROOT / "examples" / "corridor_demo" / "demo.sumocfg"
#: The demo sheet: cycle 90, offsets J1 0 / J2 20 / J3 40, NEMA 2+6 then 4+8.
SHEET_OFFSETS = {"J1": 0.0, "J2": 20.0, "J3": 40.0}


@pytest.fixture
def synthetic_eval(monkeypatch):
    monkeypatch.setenv(INLINE_EVAL_ENV_VAR, "1")

    def evaluate(task: dict) -> dict:
        delay = 20.0
        return {
            "plan_id": task["plan"]["plan_id"],
            "moe": {
                "control_delay_s_per_veh": delay, "stops_per_veh": delay / 10.0,
                "vehicles_total": 100, "vehicles_completed": 100, "completion_ratio": 1.0,
                "departed_ratio": 1.0, "total_delay_s": delay * 100.0,
                "total_stops": delay * 10.0, "mean_time_loss_s": delay,
                "mean_depart_delay_s": 0.0,
            },
            "valid": True,
            "quality_errors": [],
            "trial_dir": task["trial_dir"],
            "kpi": {},
        }

    monkeypatch.setattr("terasim_city.optimize.batch.evaluate_plan_task", evaluate)


def _run(tmp_path: Path, sheet: dict, **corridor) -> dict:
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        baseline_sheet=write_tmc(tmp_path, sheet, name="baseline_sheet.json"),
        optimizer="exhaustive",
        sim_end=60.0,
        n_workers=1,
        **corridor,
    )
    return run_corridor_optimization(cfg)


def _nema_signals(tmp_path: Path) -> list[str]:
    nema = json.loads((tmp_path / "out" / "nema_plan.json").read_text(encoding="utf-8"))
    return [entry["tls_id"] for entry in nema["intersections"]]


@requires_sumolib
def test_a_single_intersection_takes_its_signal_from_the_corridor_sheet(
    tmp_path, synthetic_eval
):
    # The synchronous on-demand request for one intersection: from == to.
    result = _run(tmp_path, baseline_sheet(), from_ref="i2", to_ref="i2")

    assert result["seed"]["source"] == "baseline_sheet"
    assert result["baseline"]["plan_id"] == "baseline"
    assert result["baseline"]["timing"]["offsets_s"] == {"J2": SHEET_OFFSETS["J2"]}
    assert result["baseline"]["timing"]["cycle_s"] == {"J2": 90.0}
    assert _nema_signals(tmp_path) == ["J2"]


@requires_sumolib
def test_a_from_to_pair_inside_the_corridor_takes_the_signals_on_its_path(
    tmp_path, synthetic_eval
):
    result = _run(tmp_path, baseline_sheet(), from_ref="i1", to_ref="i2")

    assert result["baseline"]["timing"]["offsets_s"] == {
        "J1": SHEET_OFFSETS["J1"], "J2": SHEET_OFFSETS["J2"],
    }
    assert _nema_signals(tmp_path) == ["J1", "J2"]


@requires_sumolib
def test_a_sheet_without_tls_ids_is_matched_by_intersection_id(tmp_path, synthetic_eval):
    sheet = baseline_sheet()
    for entry in sheet["intersections"]:
        entry["tls_id"] = None

    result = _run(tmp_path, sheet, from_ref="i2", to_ref="i3")

    assert result["baseline"]["timing"]["offsets_s"] == {
        "J2": SHEET_OFFSETS["J2"], "J3": SHEET_OFFSETS["J3"],
    }


@requires_sumolib
def test_a_signal_the_sheet_does_not_describe_is_a_coverage_error(tmp_path, synthetic_eval):
    sheet = baseline_sheet()
    sheet["intersections"] = [e for e in sheet["intersections"] if e["tls_id"] != "J2"]

    # The path from i1 to i3 crosses J2, which the sheet has no entry for.
    with pytest.raises(TimingSheetCoverageError, match="J2") as caught:
        _run(tmp_path, sheet, from_ref="i1", to_ref="i3")

    assert isinstance(caught.value, ValueError)  # callers catching ValueError still do
    assert "J1" in str(caught.value) and "J3" in str(caught.value)  # what it does cover


@requires_sumolib
def test_the_whole_corridor_reads_the_sheet_as_before(tmp_path, synthetic_eval):
    result = _run(tmp_path, baseline_sheet(), tls_ids=("J1", "J2", "J3"))

    assert result["baseline"]["timing"]["offsets_s"] == SHEET_OFFSETS
    assert _nema_signals(tmp_path) == ["J1", "J2", "J3"]
