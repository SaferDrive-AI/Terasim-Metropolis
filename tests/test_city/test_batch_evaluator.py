"""Integration tests for BatchEvaluator / evaluate_plan_task on the demo map.

Runs the real SUMO runtime through a 2-worker spawn pool: builds one shared
scenario via ``run_tmc_pipeline`` on ``examples/corridor_demo``, evaluates the
``alt_fixed`` seed plan plus a derived offset variant, and re-evaluates the
seed to lock determinism. Test names deliberately avoid the repo-root
``requires_sumo`` auto-marker substrings; runtime absence self-skips via
explicit markers instead.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import replace
from pathlib import Path

import pytest

from terasim_city.optimize.batch import INLINE_EVAL_ENV_VAR, BatchEvaluator
from terasim_city.optimize.evaluator import evaluate_plan_task
from terasim_city.plans.model import CorridorPlan, PlanProvenance
from tests.fixtures.tmc_templates import demo_tmc, write_tmc
from tests.test_city.conftest import (
    REPO_ROOT,
    requires_sumo_runtime,
    requires_sumolib,
)

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_CFG = DEMO_DIR / "demo.sumocfg"
DEMO_TLS = DEMO_DIR / "demo.tls.xml"

QUALITY_THRESHOLDS = {"emergency_stops": 0, "teleports": 0, "collisions": 0}

requires_demo_assets = pytest.mark.skipif(
    not DEMO_CFG.exists(),
    reason=f"corridor demo assets not present at {DEMO_DIR} "
    "(run examples/corridor_demo/build_demo_map.py)",
)

pytestmark = [requires_demo_assets, requires_sumolib, requires_sumo_runtime]


@pytest.fixture(scope="module")
def demo_scenario(tmp_path_factory):
    """One TMC-generated scenario shared by every test in this module."""
    from terasim_city.api import run_tmc_pipeline

    out_dir = tmp_path_factory.mktemp("bev_scenario")
    demo_tmc_path = write_tmc(out_dir, demo_tmc(), name="demo_tmc.json")
    return run_tmc_pipeline(demo_tmc_path, DEMO_CFG, out_dir, sim_end=60.0, seed=42)


@pytest.fixture(scope="module")
def seed_plan() -> CorridorPlan:
    from terasim_city.plans.model import CorridorPlan, PlanProvenance

    from tests.fixtures.signal_plans import demo_alt_fixed_plan

    return CorridorPlan(
        plan_id="seed_alt_fixed",
        plans=tuple(demo_alt_fixed_plan(t) for t in ("J1", "J2", "J3")),
        provenance=PlanProvenance(source="explicit"),
    )


@pytest.fixture(scope="module")
def offset_variant(seed_plan: CorridorPlan) -> CorridorPlan:
    """Same phases as the seed, J2/J3 offsets shifted (cycle is 66 s)."""
    by_tls = seed_plan.by_tls()
    return CorridorPlan(
        plan_id="alt_fixed_shifted",
        plans=(
            by_tls["J1"],
            replace(by_tls["J2"], offset_s=15.0),
            replace(by_tls["J3"], offset_s=30.0),
        ),
        provenance=PlanProvenance(
            source="explicit", parent_plan_id=seed_plan.plan_id
        ),
    )


def _make_evaluator(demo_scenario, trials_root: Path, *, n_workers: int = 2) -> BatchEvaluator:
    return BatchEvaluator(
        scenario_sumocfg=Path(demo_scenario.sumocfg_file),
        net_file=Path(demo_scenario.net_file),
        trials_root=trials_root,
        sim_end=60.0,
        stalls=[],
        quality_thresholds=dict(QUALITY_THRESHOLDS),
        n_workers=n_workers,
    )


def test_pool_evaluates_two_plans_and_repeats_deterministically(
    demo_scenario, seed_plan, offset_variant, tmp_path
):
    evaluator = _make_evaluator(demo_scenario, tmp_path / "trials", n_workers=2)
    try:
        results = evaluator.evaluate([seed_plan, offset_variant])
        assert [r["plan_id"] for r in results] == [
            seed_plan.plan_id,
            offset_variant.plan_id,
        ]
        for result in results:
            assert result["valid"] is True, result.get(
                "error", result.get("quality_errors")
            )
            assert result["quality_errors"] == []
            moe = result["moe"]
            assert moe["vehicles_total"] > 0
            assert math.isfinite(moe["control_delay_s_per_veh"])
            trial_dir = Path(result["trial_dir"])
            kpi_path = trial_dir / "kpi.json"
            assert kpi_path.exists()
            kpi = json.loads(kpi_path.read_text(encoding="utf-8"))
            assert kpi["valid"] is True
            assert kpi["signal_plan"]["plan_id"] == result["plan_id"]
            assert set(kpi["signal_plan"]["programs"]) == {"J1", "J2", "J3"}
            programs = kpi["signal_plan"]["programs"]
            assert "junction_delay_s" in moe and set(moe["junction_delay_s"]) == set(programs)
            assert isinstance(moe["corridor_junction_delay_s"], float)
            assert (trial_dir / "sim_output" / "edgedata.xml").exists()
            assert kpi["moe"] == moe
            assert result["kpi"] == kpi

        dirs = [Path(r["trial_dir"]) for r in results]
        assert dirs[0] != dirs[1]
        assert dirs[0].name == "trial_0000"
        assert dirs[1].name == "trial_0001"

        # Same plan, same scenario, fresh SUMO child: identical MOE.
        repeat = evaluator.evaluate([seed_plan])
        assert Path(repeat[0]["trial_dir"]).name == "trial_0002"
        assert (
            repeat[0]["moe"]["control_delay_s_per_veh"]
            == results[0]["moe"]["control_delay_s_per_veh"]
        )
        assert repeat[0]["moe"] == results[0]["moe"]
    finally:
        evaluator.close()


def test_junction_delay_failure_leaves_the_trial_valid(
    demo_scenario, seed_plan, tmp_path, monkeypatch, caplog
):
    """The edge-based junction delay is secondary: when it cannot be computed
    (missing / unparseable edge data) the trial still succeeds, with ``None``
    values and a warning, and the all-demand MOE is untouched."""

    def unreadable(*_args, **_kwargs):
        raise FileNotFoundError("edge data file not found")

    # Inline evaluation runs the real SUMO trial in this process, so the
    # patched function is the one ``_evaluate`` resolves.
    monkeypatch.setenv(INLINE_EVAL_ENV_VAR, "1")
    monkeypatch.setattr(
        "terasim_city.evaluation.junction_delay.junction_delay_from_edgedata", unreadable
    )
    evaluator = _make_evaluator(demo_scenario, tmp_path / "trials", n_workers=1)
    try:
        with caplog.at_level(logging.WARNING, logger="terasim_city.optimize.evaluator"):
            (result,) = evaluator.evaluate([seed_plan])
    finally:
        evaluator.close()

    assert result["valid"] is True, result.get("error", result.get("quality_errors"))
    moe = result["moe"]
    assert moe["junction_delay_s"] == {"J1": None, "J2": None, "J3": None}
    assert moe["corridor_junction_delay_s"] is None
    assert moe["vehicles_total"] > 0 and math.isfinite(moe["control_delay_s_per_veh"])
    assert any(
        "junction delay unavailable" in record.getMessage()
        and str(result["trial_dir"]) in record.getMessage()
        for record in caplog.records
    )


def test_task_exception_becomes_invalid_result(seed_plan, tmp_path):
    task = {
        "plan": seed_plan.to_json_dict(),
        "scenario_sumocfg": str(tmp_path / "missing.sumocfg"),
        "net_file": str(tmp_path / "missing.net.xml"),
        "trial_dir": str(tmp_path / "trial_err"),
        "sim_end": 60.0,
        "stalls": [],
        "quality_thresholds": dict(QUALITY_THRESHOLDS),
        "kpi_weights": None,
    }
    result = evaluate_plan_task(task)
    assert result["valid"] is False
    assert result["plan_id"] == seed_plan.plan_id
    assert "error" in result and result["error"]
    assert result["trial_dir"] == str(tmp_path / "trial_err")


def test_inline_env_hook_skips_pool(demo_scenario, seed_plan, tmp_path, monkeypatch):
    monkeypatch.setenv(INLINE_EVAL_ENV_VAR, "1")
    evaluator = _make_evaluator(demo_scenario, tmp_path / "trials", n_workers=2)
    try:
        # Broken plan payload: fails fast inside evaluate_plan_task without
        # ever starting SUMO, proving the inline path runs in-process.
        broken = dict(seed_plan.to_json_dict())
        broken.pop("plans")

        class _FakePlan:
            plan_id = "broken"

            @staticmethod
            def to_json_dict() -> dict:
                return broken

        results = evaluator.evaluate([_FakePlan()])
        assert len(results) == 1
        assert results[0]["valid"] is False
        assert "plans" in results[0]["error"]
        assert evaluator._pool is None  # inline mode never builds a pool
        assert Path(results[0]["trial_dir"]).name == "trial_0000"
    finally:
        evaluator.close()


def test_worker_count_validation(demo_scenario, tmp_path):
    with pytest.raises(ValueError, match="n_workers"):
        _make_evaluator(demo_scenario, tmp_path / "trials", n_workers=0)
