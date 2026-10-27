"""Tests for the corridor optimization orchestration loop.

Two layers:

* FAST: ``TERASIM_CITY_INLINE_EVAL=1`` plus a monkeypatched
  ``evaluate_plan_task`` returning a synthetic MOE as a function of the plan's
  offsets — exercises seed strategy, scoring, the BO loop, the trials ledger,
  and the progress-callback contract without ever starting SUMO.
* REAL end-to-end: the demo corridor (3 TLS, baseline sheet, Bayesian
  offsets), a dual-TLS corridor on the demo net whose additional files program
  J1 only (Webster seed — J2 has no additional-file program — plus Bayesian
  offsets), and the scenario-file paths (the scenario's own routes and signal
  programs) through the actual runtime.

Test names deliberately avoid the repo-root auto-marker substrings; runtime
requirements self-skip via explicit markers.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import shutil
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

import terasim_city.orchestration.corridor_optimization as co
from terasim_city.optimize.base import Budget
from terasim_city.optimize.batch import INLINE_EVAL_ENV_VAR
from terasim_city.optimize.bayes_corridor import BayesianCorridorOptimizer
from terasim_city.optimize.bayes_offsets import BayesianOffsetOptimizer
from terasim_city.optimize.bayes_splits import BayesianSplitOptimizer
from terasim_city.orchestration.corridor_optimization import (
    OPTIMIZER_CHOICES,
    CorridorOptimizationConfig,
    _bo_mode,
    _cycle_bounds,
    _timing,
    run_corridor_optimization,
)
from terasim_city.plans.model import CorridorPlan, PhaseSpec, PlanProvenance, SignalPlan
from terasim_city.plans.nema_plan import NemaCorridorPlan, NemaSignalEntry
from terasim_city.plans.signal_groups import derive_link_movements
from tests.fixtures.demo_corridor import write_partial_program_sumocfg
from tests.fixtures.tmc_templates import (
    baseline_sheet,
    demo_tmc,
    two_signal_tmc,
    write_tmc,
)
from tests.test_city.conftest import (
    REPO_ROOT,
    requires_sumo_runtime,
    requires_sumolib,
)

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_CFG = DEMO_DIR / "demo.sumocfg"

RESULT_KEYS = {
    "schema",
    "seed",
    "resolved_corridor",
    "baseline",
    "best",
    "trials",
    "nema",
    "nema_export",
    "unserved_movements",
    "search",
    "config",
    "n_trials",
}

requires_demo_assets = pytest.mark.skipif(
    not DEMO_CFG.exists(),
    reason=f"corridor demo assets not present at {DEMO_DIR} "
    "(run examples/corridor_demo/build_demo_map.py)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _synthetic_moe(delay: float) -> dict:
    return {
        "control_delay_s_per_veh": delay,
        "stops_per_veh": delay / 10.0,
        "vehicles_total": 100,
        "vehicles_completed": 100,
        "vehicles_unfinished": 0,
        "vehicles_undeparted": 0,
        "total_delay_s": delay * 100.0,
        "total_stops": int(delay * 10.0),
        "mean_time_loss_s": delay,
        "mean_depart_delay_s": 0.0,
    }


def _install_fake_evaluator(monkeypatch, delay_fn):
    """Route BatchEvaluator inline through a synthetic per-plan objective.

    ``delay_fn(plan_id, offsets)`` -> control delay (s/veh); stops follow as
    delay/10. Inline mode calls the ``evaluate_plan_task`` name imported into
    ``terasim_city.optimize.batch``, so that is what gets patched.
    """
    monkeypatch.setenv(INLINE_EVAL_ENV_VAR, "1")

    def fake_evaluate_plan_task(task: dict) -> dict:
        plan = task["plan"]
        offsets = {p["tls_id"]: float(p["offset_s"]) for p in plan["plans"]}
        delay = float(delay_fn(plan["plan_id"], offsets))
        return {
            "plan_id": plan["plan_id"],
            "moe": _synthetic_moe(delay),
            "valid": True,
            "quality_errors": [],
            "trial_dir": task["trial_dir"],
            "kpi": {},
        }

    monkeypatch.setattr(
        "terasim_city.optimize.batch.evaluate_plan_task", fake_evaluate_plan_task
    )


def _one_tls_demo_copy(tmp_path: Path) -> Path:
    """Copy the demo map and give J2 a second program so exhaustive has >1 seed."""
    demo_copy = tmp_path / "demo_map"
    shutil.copytree(DEMO_DIR, demo_copy)
    tls_path = demo_copy / "demo.tls.xml"
    tree = ET.parse(tls_path)
    root = tree.getroot()
    j2 = next(node for node in root.findall("tlLogic") if node.attrib["id"] == "J2")
    alt = copy.deepcopy(j2)
    alt.set("programID", "alt_b")
    for phase, duration in zip(alt.findall("phase"), ("40", "3", "20", "3")):
        phase.set("duration", duration)
    root.append(alt)
    tree.write(tls_path)
    return demo_copy / "demo.sumocfg"


def _install_fake_plan_evaluator(monkeypatch, delay_fn):
    """Like ``_install_fake_evaluator`` but with the whole plan dict as input.

    ``delay_fn(plan_dict)`` -> control delay (s/veh); needed by the split-search
    tests whose objective depends on green durations, not offsets.
    """
    monkeypatch.setenv(INLINE_EVAL_ENV_VAR, "1")

    def fake_evaluate_plan_task(task: dict) -> dict:
        plan = task["plan"]
        delay = float(delay_fn(plan))
        return {
            "plan_id": plan["plan_id"],
            "moe": _synthetic_moe(delay),
            "valid": True,
            "quality_errors": [],
            "trial_dir": task["trial_dir"],
            "kpi": {},
        }

    monkeypatch.setattr(
        "terasim_city.optimize.batch.evaluate_plan_task", fake_evaluate_plan_task
    )




def _read_ledger(output_dir: Path) -> list[dict]:
    lines = (output_dir / "trials.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# FAST path (inline eval + synthetic objective)
# ---------------------------------------------------------------------------

@requires_demo_assets
@requires_sumolib
def test_exhaustive_static_path_seeds_webster_deterministically(tmp_path, monkeypatch):
    # No uploaded plan, no timing sheet: the ONLY static seed is Webster —
    # deterministic by construction (never dependent on additional-file order).
    base_sumocfg = _one_tls_demo_copy(tmp_path)
    _install_fake_evaluator(monkeypatch, lambda plan_id, _offsets: 20.0)

    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=base_sumocfg,
        output_dir=tmp_path / "out",
        tls_ids=("J2",),
        optimizer="exhaustive",
        sim_end=60.0,
        n_workers=2,
    )
    result = run_corridor_optimization(cfg)

    assert set(result) == RESULT_KEYS
    # The caller can SEE which seed the run used (uploaded plan vs generated).
    assert result["seed"]["source"] == "webster_init"
    assert result["seed"]["stored_plans_used"] is False
    assert result["schema"] == "terasim.corridor_optimization.v1"
    assert result["n_trials"] == 1
    # No Bayesian phase was asked for: reported as not run, with that reason.
    assert result["search"] == {
        "ran": False, "reason": "optimizer 'exhaustive' evaluates the seed plans only",
        "warm_starts": {"used": [], "skipped": []},
    }
    assert [t["plan_id"] for t in result["trials"]] == ["webster_init"]
    assert all(t["source"] == "webster_init" for t in result["trials"])
    assert result["baseline"]["plan_id"] == "webster_init"
    assert result["baseline"]["score"] == pytest.approx(1.0)
    assert len(result["resolved_corridor"]) == 1


@requires_demo_assets
@requires_sumolib
def test_bayes_loop_improves_over_baseline_and_reports_progress(tmp_path, monkeypatch):
    # Optimum at offset_J2 = 33 (baseline sheet has 20): BO must move toward it.
    _install_fake_evaluator(
        monkeypatch, lambda _plan_id, offsets: 5.0 + abs(offsets["J2"] - 33.0)
    )
    calls: list[tuple] = []

    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        from_ref="i1",
        to_ref="i3",
        baseline_sheet=write_tmc(tmp_path, baseline_sheet(), name="baseline_sheet.json"),
        optimizer="bayes_offsets",
        budget=Budget(max_trials=10, patience=0),
        sim_end=120.0,
        seed=42,
        n_workers=3,
    )
    result = run_corridor_optimization(
        cfg, progress_callback=lambda *args: calls.append(args)
    )

    total = 1 + 10  # one baseline seed + max_trials
    assert calls[0] == (0, total, None)
    assert [c[0] for c in calls[1:]] == list(range(1, total + 1))
    assert all(c[1] == total for c in calls)
    assert calls[1][2] == "baseline"
    assert all(c[2].startswith("bo_") for c in calls[2:])

    assert result["n_trials"] == total
    assert result["search"] == {
        "ran": True, "reason": None, "warm_starts": {"used": ["baseline"], "skipped": []},
    }
    ledger = _read_ledger(cfg.output_dir)
    assert len(ledger) == total
    assert ledger == result["trials"]
    assert [t["trial"] for t in ledger] == list(range(total))
    assert ledger[0]["plan_id"] == "baseline"
    assert ledger[0]["source"] == "baseline_sheet"
    assert all(t["source"] == "bo_iteration" for t in ledger[1:])

    assert result["baseline"]["plan_id"] == "baseline"
    assert result["baseline"]["score"] == pytest.approx(1.0)
    assert result["best"]["score"] < result["baseline"]["score"]
    best_j2 = result["best"]["offsets"]["J2"]
    assert abs(best_j2 - 33.0) < abs(20.0 - 33.0)
    # BO plans keep the baseline phase structure -> NEMA numbers survive.
    assert result["nema"]["handoff_ready"] is True

    tls_order = [item["tls_id"] for item in result["resolved_corridor"]]
    assert tls_order == ["J1", "J2", "J3"]


@requires_demo_assets
@requires_sumolib
def test_progress_callback_exception_stops_run_and_propagates(tmp_path, monkeypatch):
    _install_fake_evaluator(monkeypatch, lambda _plan_id, _offsets: 10.0)

    class SoftCancel(Exception):
        """Stand-in for the service worker's LeaseLost."""

    def cancelling_callback(done: int, _total: int, _plan_id) -> None:
        if done >= 2:
            raise SoftCancel("lease lost")

    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        from_ref="i1",
        to_ref="i3",
        baseline_sheet=write_tmc(tmp_path, baseline_sheet(), name="baseline_sheet.json"),
        optimizer="bayes_offsets",
        budget=Budget(max_trials=10, patience=0),
        sim_end=120.0,
        n_workers=3,
    )
    with pytest.raises(SoftCancel):
        run_corridor_optimization(cfg, progress_callback=cancelling_callback)

    # The run stopped mid-loop: the ledger holds exactly the trials recorded
    # up to (and including) the cancelled callback, and no result was written.
    assert len(_read_ledger(cfg.output_dir)) == 2
    assert not (cfg.output_dir / "corridor_optimization_result.json").exists()


@requires_demo_assets
@requires_sumolib
def test_auto_single_tls_runs_split_search_after_seeds(tmp_path, monkeypatch):
    # Optimum at EW green = 40 (the alt_fixed seed has 30): the split search
    # must run after the seeds and every trial must keep the seed's cycle.
    def delay_fn(plan: dict) -> float:
        greens = [
            p["duration_s"] for p in plan["plans"][0]["phases"] if p["kind"] == "green"
        ]
        return 5.0 + abs(greens[0] - 40.0)

    _install_fake_plan_evaluator(monkeypatch, delay_fn)
    calls: list[tuple] = []

    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J2",),
        optimizer="auto",
        budget=Budget(max_trials=6, patience=0),
        sim_end=60.0,
        seed=42,
        n_workers=2,
    )
    result = run_corridor_optimization(
        cfg, progress_callback=lambda *args: calls.append(args)
    )

    # One Webster seed + the full split budget.
    n_seeds = 1
    total = n_seeds + 6
    assert calls[0] == (0, total, None)
    assert all(c[1] == total for c in calls)
    assert result["n_trials"] == total
    assert result["search"] == {
        "ran": True, "reason": None, "warm_starts": {"used": ["webster_init"], "skipped": []},
    }

    ledger = _read_ledger(cfg.output_dir)
    assert ledger == result["trials"]
    assert ledger[0]["plan_id"] == "webster_init"
    assert ledger[0]["source"] == "webster_init"
    # bo_split_* trials appear after the seeds.
    assert all(t["plan_id"].startswith("bo_split_") for t in ledger[n_seeds:])
    assert all(t["source"] == "bo_iteration" for t in ledger[n_seeds:])
    assert result["baseline"]["plan_id"] == "webster_init"
    assert result["baseline"]["score"] == pytest.approx(1.0)
    assert result["best"]["score"] <= result["baseline"]["score"]


@requires_demo_assets
@requires_sumolib
def test_bayes_offsets_rejected_for_single_tls_corridor(tmp_path):
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J2",),
        optimizer="bayes_offsets",
    )
    with pytest.raises(ValueError, match="bayes_offsets"):
        run_corridor_optimization(cfg)


@requires_demo_assets
@requires_sumolib
def test_bayes_splits_rejected_for_multi_tls_corridor(tmp_path):
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        from_ref="i1",
        to_ref="i3",
        optimizer="bayes_splits",
    )
    with pytest.raises(ValueError, match="bayes_splits"):
        run_corridor_optimization(cfg)


def test_unknown_optimizer_rejected(tmp_path):
    with pytest.raises(ValueError, match="optimizer"):
        CorridorOptimizationConfig(
            tmc_json=write_tmc(tmp_path, demo_tmc()),
            base_sumocfg=DEMO_CFG,
            output_dir=tmp_path / "out",
            optimizer="grid_search",
        )


# ---------------------------------------------------------------------------
# Webster seed decomposition (stage_mode) and seed cycle bounds
# ---------------------------------------------------------------------------

def test_config_stage_and_cycle_defaults(tmp_path):
    cfg = CorridorOptimizationConfig(
        tmc_json=tmp_path / "t.json", base_sumocfg=None, output_dir=tmp_path,
    )
    assert cfg.stage_mode == "two_stage"
    assert cfg.search_cycle is False
    assert (cfg.cycle_min_s, cfg.cycle_max_s) == (45.0, 150.0)


def test_config_rejects_unknown_stage_mode(tmp_path):
    with pytest.raises(ValueError, match="stage_mode"):
        CorridorOptimizationConfig(
            tmc_json=tmp_path / "t.json", base_sumocfg=None, output_dir=tmp_path,
            stage_mode="five",
        )


def test_config_rejects_non_bool_search_cycle(tmp_path):
    with pytest.raises(ValueError, match="search_cycle"):
        CorridorOptimizationConfig(
            tmc_json=tmp_path / "t.json", base_sumocfg=None, output_dir=tmp_path,
            search_cycle="yes",
        )


@pytest.mark.parametrize("value", ["false", 1])
def test_config_rejects_non_bool_seed_from_scenario_programs(tmp_path, value):
    with pytest.raises(ValueError, match="seed_from_scenario_programs"):
        CorridorOptimizationConfig(
            tmc_json=tmp_path / "t.json", base_sumocfg=None, output_dir=tmp_path,
            seed_from_scenario_programs=value,
        )


@pytest.mark.parametrize(
    ("low", "high"), [(0.0, 100.0), (-5.0, 100.0), (100.0, 100.0), (150.0, 100.0)]
)
def test_config_rejects_bad_cycle_bounds(tmp_path, low, high):
    with pytest.raises(ValueError, match="cycle_min_s"):
        CorridorOptimizationConfig(
            tmc_json=tmp_path / "t.json", base_sumocfg=None, output_dir=tmp_path,
            cycle_min_s=low, cycle_max_s=high,
        )


def test_config_coerces_cycle_bounds_to_float(tmp_path):
    cfg = CorridorOptimizationConfig(
        tmc_json=tmp_path / "t.json", base_sumocfg=None, output_dir=tmp_path,
        cycle_min_s=60, cycle_max_s=120,
    )
    assert (cfg.cycle_min_s, cfg.cycle_max_s) == (60.0, 120.0)
    assert isinstance(cfg.cycle_min_s, float) and isinstance(cfg.cycle_max_s, float)


def _one_demanded_approach_tmc(tmp_path):
    payload = demo_tmc()
    payload["intersections"] = payload["intersections"][:1]  # i1 resolves to J1
    for direction, turns in payload["intersections"][0]["counts"].items():
        if direction != "East":
            for turn in turns:
                turns[turn] = 0
    return write_tmc(tmp_path, payload, name="one.json")


@requires_demo_assets
@requires_sumolib
def test_per_approach_single_demanded_approach_still_serves_every_approach(
    monkeypatch, tmp_path
):
    """One demanded approach -> the three empty approaches still get their stage, at
    the minimum green (spec G2), so the split search has stages to work with."""
    seen: list[dict] = []

    def delay_fn(plan: dict) -> float:
        seen.append(plan)
        return 100.0

    _install_fake_plan_evaluator(monkeypatch, delay_fn)
    cfg = CorridorOptimizationConfig(
        tmc_json=_one_demanded_approach_tmc(tmp_path),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J1",),
        optimizer="auto",
        budget=Budget(max_trials=4, patience=2),
        stage_mode="per_approach",
        sim_end=60.0,
        n_workers=1,
    )
    result = run_corridor_optimization(cfg)

    greens = [p for p in seen[0]["plans"][0]["phases"] if p["kind"] == "green"]
    assert [g["movements"][0].split("-")[0] for g in greens] == ["SB", "WB", "NB", "EB"]
    assert [g["duration_s"] for g in greens[:3]] == [5.0, 5.0, 5.0]
    assert greens[3]["duration_s"] > 5.0
    assert result["search"]["ran"] is True
    assert result["n_trials"] > 1
    assert result["unserved_movements"] == {}


@requires_demo_assets
@requires_sumolib
def test_auto_with_only_single_green_stage_seeds_degrades_to_seeds_only(
    monkeypatch, tmp_path
):
    """A seed with one green stage (odd production programs) -> the split search is
    skipped (seeds only) instead of raising."""
    real_build = co.build_webster_plan

    def first_stage_only(*args, **kwargs):
        plan = real_build(*args, **kwargs)
        return CorridorPlan(
            plan_id=plan.plan_id,
            plans=tuple(
                SignalPlan(tls_id=sp.tls_id, program_id=sp.program_id, offset_s=0.0,
                           phases=sp.phases[:3])
                for sp in plan.plans
            ),
            provenance=plan.provenance,
        )

    monkeypatch.setattr(co, "build_webster_plan", first_stage_only)
    seen: list[dict] = []

    def delay_fn(plan: dict) -> float:
        seen.append(plan)
        return 100.0

    _install_fake_plan_evaluator(monkeypatch, delay_fn)
    cfg = CorridorOptimizationConfig(
        tmc_json=_one_demanded_approach_tmc(tmp_path),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J1",),
        optimizer="auto",
        budget=Budget(max_trials=4, patience=2),
        stage_mode="per_approach",
        sim_end=60.0,
        n_workers=1,
    )
    result = run_corridor_optimization(cfg)

    # The seed really is a single-green-stage plan (the precondition)...
    assert len(seen) == 1
    greens = [p for p in seen[0]["plans"][0]["phases"] if p["kind"] == "green"]
    assert len(greens) == 1
    # ... so no Bayesian trial ran and the seed is the answer.
    assert result["n_trials"] == 1 and result["best"]["plan_id"] == "webster_init"
    assert result["search"]["ran"] is False
    assert "no seed plan for TLS J1 has >= 2 green stages" in result["search"]["reason"]
    # Only the SB stage survived: the other approaches' movements are never green.
    assert set(result["unserved_movements"]) == {"J1"}
    assert {m.split("-")[0] for m in result["unserved_movements"]["J1"]} == {"WB", "NB", "EB"}


@requires_demo_assets
@requires_sumolib
def test_per_approach_seed_gets_stage_mode_and_cycle_bounds_from_config(
    monkeypatch, tmp_path
):
    seen: list[dict] = []

    def delay_fn(plan: dict) -> float:
        seen.append(plan)
        return 10.0

    _install_fake_plan_evaluator(monkeypatch, delay_fn)
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J2",),
        optimizer="exhaustive",
        stage_mode="per_approach",
        cycle_min_s=100.0,
        cycle_max_s=120.0,
        sim_end=60.0,
        n_workers=1,
    )
    result = run_corridor_optimization(cfg)

    assert [plan["plan_id"] for plan in seen] == ["webster_init"]
    phases = seen[0]["plans"][0]["phases"]
    greens = [p for p in phases if p["kind"] == "green"]
    # Demand on every approach -> one protected stage each, SB, WB, NB, EB.
    assert [p["movements"][0].split("-")[0] for p in greens] == ["SB", "WB", "NB", "EB"]
    # The unbounded Webster cycle here is ~81 s, so 100 s proves the floor applied.
    assert sum(p["duration_s"] for p in phases) == pytest.approx(100.0)
    echo = result["config"]
    assert (echo["stage_mode"], echo["search_cycle"]) == ("per_approach", False)
    assert (echo["cycle_min_s"], echo["cycle_max_s"]) == (100.0, 120.0)


# ---------------------------------------------------------------------------
# bayes_corridor: joint search over a common cycle, per-TLS splits and offsets
# ---------------------------------------------------------------------------

def _corridor_cfg(tmp_path: Path, **overrides) -> CorridorOptimizationConfig:
    """3-TLS demo corridor seeded from the baseline sheet (cycle 90, offsets 0/20/40)."""
    fields = dict(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J1", "J2", "J3"),
        baseline_sheet=write_tmc(tmp_path, baseline_sheet(), name="baseline_sheet.json"),
        optimizer="bayes_corridor",
        sim_end=120.0,
        n_workers=4,
        seed=11,
    )
    fields.update(overrides)
    return CorridorOptimizationConfig(**fields)


def _phase_cycle(signal_plan: dict) -> float:
    return sum(p["duration_s"] for p in signal_plan["phases"])


@requires_demo_assets
@requires_sumolib
def test_bayes_corridor_searches_cycle_and_offsets(monkeypatch, tmp_path):
    seen: list[dict] = []

    # Cycle-only objective: seed-independent pass; offsets are pinned by test_bayes_corridor.py.
    def objective(plan: dict) -> float:
        seen.append(plan)
        cycle = sum(p["duration_s"] for p in plan["plans"][0]["phases"])
        return abs(cycle - 110.0) + 10.0

    _install_fake_plan_evaluator(monkeypatch, objective)
    cfg = _corridor_cfg(
        tmp_path,
        search_cycle=True,
        cycle_min_s=60.0,
        cycle_max_s=150.0,
        budget=Budget(max_trials=40, patience=0),
    )
    result = run_corridor_optimization(cfg)
    assert result["best"]["score"] < result["baseline"]["score"]
    timing = result["best"]["timing"]
    cycles = set(round(c) for c in timing["cycle_s"].values())
    assert len(cycles) == 1 and 60 <= cycles.pop() <= 150
    assert timing["offsets_s"]["J1"] == 0.0 and set(timing["greens_s"]) == {"J1", "J2", "J3"}
    assert result["baseline"]["timing"]["cycle_s"]["J1"] == pytest.approx(90.0)
    assert result["config"]["optimizer"] == "bayes_corridor"

    # The joint optimizer really ran: one seed, then the full budget of bo_corr_* trials whose
    # cycles vary across the whole range and are shared by every TLS of a plan.
    assert result["n_trials"] == 1 + 40
    assert all(t["plan_id"].startswith("bo_corr_") for t in result["trials"][1:])
    bo_plans = seen[1:]
    assert len(bo_plans) == 40
    assert len({round(_phase_cycle(plan["plans"][0])) for plan in bo_plans}) > 5
    for plan in bo_plans:
        cycle = _phase_cycle(plan["plans"][0])
        assert 60.0 - 1e-9 <= cycle <= 150.0 + 1e-9
        assert all(_phase_cycle(p) == pytest.approx(cycle) for p in plan["plans"])
    # Offsets are searched end to end too, not just the cycle.
    assert len({plan["plans"][1]["offset_s"] for plan in bo_plans}) > 1
    assert timing["offsets_s"] == result["best"]["offsets"]


@requires_demo_assets
@requires_sumolib
def test_bayes_corridor_without_search_cycle_keeps_the_seed_cycle(monkeypatch, tmp_path):
    seen: list[dict] = []

    def objective(plan: dict) -> float:
        seen.append(plan)
        offsets = {p["tls_id"]: float(p["offset_s"]) for p in plan["plans"]}
        greens = [p["duration_s"] for p in plan["plans"][0]["phases"] if p["kind"] == "green"]
        return 10.0 + abs(offsets["J2"] - 20.0) + abs(offsets["J3"] - 40.0) + abs(greens[0] - 40.0)

    _install_fake_plan_evaluator(monkeypatch, objective)
    result = run_corridor_optimization(
        _corridor_cfg(tmp_path, budget=Budget(max_trials=12, patience=0))
    )

    assert result["n_trials"] == 1 + 12
    # search_cycle is off: every plan of every TLS keeps the baseline's 90 s cycle ...
    for plan in seen:
        assert all(_phase_cycle(p) == pytest.approx(90.0) for p in plan["plans"])
    assert all(c == pytest.approx(90.0) for c in result["best"]["timing"]["cycle_s"].values())
    # ... while, unlike bayes_offsets, the greens are searched together with the offsets.
    j1_splits = {tuple(p["duration_s"] for p in plan["plans"][0]["phases"]) for plan in seen[1:]}
    assert len(j1_splits) > 1


@requires_demo_assets
@requires_sumolib
def test_bayes_corridor_rejects_single_tls(tmp_path):
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J1",),
        optimizer="bayes_corridor",
    )
    with pytest.raises(ValueError, match="bayes_corridor"):
        run_corridor_optimization(cfg)


@requires_demo_assets
@requires_sumolib
@pytest.mark.parametrize("optimizer", ["bayes_offsets", "bayes_splits", "bayes_corridor"])
def test_static_search_optimizers_are_rejected_on_a_stored_nema_seed(
    monkeypatch, tmp_path, optimizer
):
    entry = NemaSignalEntry(
        tls_id="J1", intersection={}, pattern={"pattern_id": "p1", "cycle_s": 90.0}
    )
    nema_seed = NemaCorridorPlan(
        plan_id="stored", entries=(entry,), provenance=PlanProvenance(source="stored_nema")
    )
    monkeypatch.setattr(co, "_load_stored_nema_seed", lambda *_args, **_kwargs: (nema_seed, None))
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J1", "J2", "J3"),
        optimizer=optimizer,
    )
    with pytest.raises(ValueError, match=rf"'{optimizer}'.*stored NEMA seed"):
        run_corridor_optimization(cfg)


@requires_demo_assets
@requires_sumolib
def test_timing_block_is_reported_for_offset_search_too(monkeypatch, tmp_path):
    _install_fake_evaluator(
        monkeypatch, lambda _plan_id, offsets: 5.0 + abs(offsets["J2"] - 33.0)
    )
    result = run_corridor_optimization(
        _corridor_cfg(
            tmp_path,
            optimizer="bayes_offsets",
            budget=Budget(max_trials=6, patience=0),
            n_workers=3,
        )
    )
    baseline, best = result["baseline"]["timing"], result["best"]["timing"]
    assert set(baseline) == set(best) == {"cycle_s", "offsets_s", "greens_s"}
    assert baseline["offsets_s"] == {"J1": 0.0, "J2": 20.0, "J3": 40.0}
    assert baseline["greens_s"] == {"J1": [48.0, 34.0], "J2": [48.0, 34.0], "J3": [48.0, 34.0]}
    assert baseline["cycle_s"] == {"J1": 90.0, "J2": 90.0, "J3": 90.0}
    # Offset search moves nothing but the offsets, and `timing` agrees with `offsets`.
    assert best["offsets_s"] == result["best"]["offsets"]
    assert best["cycle_s"] == baseline["cycle_s"] and best["greens_s"] == baseline["greens_s"]


@requires_demo_assets
@requires_sumolib
def test_auto_single_tls_split_search_varies_the_cycle_under_search_cycle(monkeypatch, tmp_path):
    seen: list[dict] = []

    def delay_fn(plan: dict) -> float:
        seen.append(plan)
        return 10.0

    _install_fake_plan_evaluator(monkeypatch, delay_fn)
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J2",),
        optimizer="auto",
        search_cycle=True,
        cycle_min_s=60.0,
        cycle_max_s=120.0,
        budget=Budget(max_trials=10, patience=0),
        sim_end=60.0,
        n_workers=2,
    )
    run_corridor_optimization(cfg)

    bo_cycles = [
        _phase_cycle(plan["plans"][0]) for plan in seen if plan["plan_id"].startswith("bo_split_")
    ]
    assert len(bo_cycles) == 10
    assert all(60.0 - 1e-9 <= c <= 120.0 + 1e-9 for c in bo_cycles)
    assert len({round(c) for c in bo_cycles}) > 3  # the cycle is searched, not frozen


def _regreen(signal_plan, shift: float):
    """The same program with ``shift`` s moved from its second green to its first (same cycle)."""
    phases = list(signal_plan.phases)
    first, second = [i for i, ph in enumerate(phases) if ph.kind == "green"][:2]
    phases[first] = replace(phases[first], duration_s=phases[first].duration_s + shift)
    phases[second] = replace(phases[second], duration_s=phases[second].duration_s - shift)
    return replace(signal_plan, phases=tuple(phases))


def _lengthen(signal_plan, extra: float):
    """The same program with ``extra`` more seconds on its first green (a longer cycle)."""
    phases = list(signal_plan.phases)
    first = next(i for i, ph in enumerate(phases) if ph.kind == "green")
    phases[first] = replace(phases[first], duration_s=phases[first].duration_s + extra)
    return replace(signal_plan, phases=tuple(phases))


def _install_seed_pool(monkeypatch, make_extras) -> None:
    """Seed every run with ``[<the usual seed plan>, *make_extras(that plan)]``."""
    original = co._build_seed_plans

    def with_extra_seeds(cfg, corridor, net_file, **kwargs):
        base = original(cfg, corridor, net_file, **kwargs)[0]
        return [base, *make_extras(base)]

    monkeypatch.setattr(co, "_build_seed_plans", with_extra_seeds)


def _install_pool_evaluator(monkeypatch, delays: dict, *, invalid: tuple = ()) -> None:
    """Inline fake evaluator: ``delays[plan_id]`` (25 s for any other), ``invalid`` ids fail."""
    monkeypatch.setenv(INLINE_EVAL_ENV_VAR, "1")

    def fake_evaluate_plan_task(task: dict) -> dict:
        plan_id = task["plan"]["plan_id"]
        if plan_id in invalid:
            return {
                "plan_id": plan_id, "moe": {}, "valid": False, "quality_errors": ["boom"],
                "error": "boom", "trial_dir": task["trial_dir"], "kpi": {},
            }
        return {
            "plan_id": plan_id, "moe": _synthetic_moe(delays.get(plan_id, 25.0)), "valid": True,
            "quality_errors": [], "trial_dir": task["trial_dir"], "kpi": {},
        }

    monkeypatch.setattr("terasim_city.optimize.batch.evaluate_plan_task", fake_evaluate_plan_task)


def _spy_warm_starts(monkeypatch, optimizer_class) -> list:
    """Record every ``optimizer_class.warm_start`` call as ``(plan_id, accepted)``."""
    calls: list[tuple[str, bool]] = []
    real_warm_start = optimizer_class.warm_start

    def spy(self, evaluations):
        plan_id = evaluations[0].plan.plan_id
        try:
            real_warm_start(self, evaluations)
        except ValueError:
            calls.append((plan_id, False))
            raise
        calls.append((plan_id, True))

    monkeypatch.setattr(optimizer_class, "warm_start", spy)
    return calls


@requires_demo_assets
@requires_sumolib
def test_extra_valid_seeds_warm_start_the_search_and_incompatible_ones_are_skipped(
    monkeypatch, tmp_path, caplog
):
    def extras(base):
        shifted = replace(
            base, plan_id="seed_shifted", plans=tuple(_regreen(sp, 4.0) for sp in base.plans)
        )
        three_stage = replace(
            base,
            plan_id="seed_three_stage",
            plans=tuple(
                replace(sp, phases=sp.phases + sp.phases[:3]) if sp.tls_id == "J2" else sp
                for sp in base.plans
            ),
        )
        assert sum(1 for ph in three_stage.by_tls()["J2"].phases if ph.kind == "green") == 3
        other_cycle = replace(
            base, plan_id="seed_other_cycle", plans=tuple(_lengthen(sp, 5.0) for sp in base.plans)
        )
        return [shifted, three_stage, other_cycle, replace(base, plan_id="seed_broken")]

    _install_seed_pool(monkeypatch, extras)
    _install_pool_evaluator(
        monkeypatch,
        {
            "baseline": 30.0,
            "seed_shifted": 20.0,
            "seed_three_stage": 40.0,
            "seed_other_cycle": 35.0,
        },
        invalid=("seed_broken",),
    )
    warm_starts = _spy_warm_starts(monkeypatch, BayesianCorridorOptimizer)

    cfg = _corridor_cfg(tmp_path, budget=Budget(max_trials=6, patience=0), n_workers=3)
    with caplog.at_level(logging.INFO, logger=co.logger.name):
        result = run_corridor_optimization(cfg)

    # The best seed first, then the other valid seeds in order: the baseline is accepted; the
    # three-stage plan and the 95 s plan (the search keeps the 90 s cycle) are refused and logged;
    # the invalid seed is never offered; the best seed is not offered twice.
    assert warm_starts == [
        ("seed_shifted", True),
        ("baseline", True),
        ("seed_three_stage", False),
        ("seed_other_cycle", False),
    ]
    skipped = [
        r.getMessage() for r in caplog.records if "not used as a warm start" in r.getMessage()
    ]
    assert len(skipped) == 2
    assert "seed_three_stage" in skipped[0] and "3 green stage" in skipped[0]
    assert "seed_other_cycle" in skipped[1] and "95s cycle" in skipped[1]
    assert result["n_trials"] == 5 + 6
    assert all(t["plan_id"].startswith("bo_corr_") for t in result["trials"][5:])
    assert result["best"]["plan_id"] == "seed_shifted"
    # The result says the same: which seeds informed the search, and why the others did not.
    warm = result["search"]["warm_starts"]
    assert warm["used"] == ["seed_shifted", "baseline"]
    assert [item["plan_id"] for item in warm["skipped"]] == [
        "seed_three_stage", "seed_other_cycle", "seed_broken",
    ]
    assert "3 green stage" in warm["skipped"][0]["reason"]
    assert "95s cycle" in warm["skipped"][1]["reason"]
    assert warm["skipped"][2]["reason"] == "its trial was not valid"


@requires_demo_assets
@requires_sumolib
@pytest.mark.parametrize(
    ("optimizer", "single_tls", "optimizer_class"),
    [
        ("auto", False, BayesianOffsetOptimizer),  # >= 2 TLS: offset search
        ("bayes_offsets", False, BayesianOffsetOptimizer),
        ("auto", True, BayesianSplitOptimizer),  # one TLS: split search
    ],
    ids=["auto-corridor", "bayes_offsets", "auto-single-tls"],
)
def test_extra_seeds_are_never_offered_to_the_offset_and_split_optimizers(
    monkeypatch, tmp_path, optimizer, single_tls, optimizer_class
):
    # These optimizers encode only part of a plan (its offsets, or its splits), so a second seed
    # would be scored at a plan that was never evaluated: only the best seed may warm-start them.
    _install_seed_pool(
        monkeypatch,
        lambda base: [
            replace(
                base, plan_id="seed_shifted", plans=tuple(_regreen(sp, 4.0) for sp in base.plans)
            )
        ],
    )
    _install_pool_evaluator(
        monkeypatch, {"baseline": 30.0, "webster_init": 30.0, "seed_shifted": 20.0}
    )
    warm_starts = _spy_warm_starts(monkeypatch, optimizer_class)

    overrides = {"tls_ids": ("J2",), "baseline_sheet": None} if single_tls else {}
    cfg = _corridor_cfg(
        tmp_path,
        optimizer=optimizer,
        budget=Budget(max_trials=6, patience=0),
        n_workers=3,
        **overrides,
    )
    result = run_corridor_optimization(cfg)

    assert warm_starts == [("seed_shifted", True)]  # the best seed, once; never the other seed
    assert result["n_trials"] == 2 + 6
    assert result["best"]["plan_id"] == "seed_shifted"
    assert result["search"]["warm_starts"] == {"used": ["seed_shifted"], "skipped": []}


# ---------------------------------------------------------------------------
# Result `timing` block and small orchestration helpers
# ---------------------------------------------------------------------------

def _static_plan() -> CorridorPlan:
    def signal_plan(tls_id: str, offset: float, green_a: float, green_b: float) -> SignalPlan:
        return SignalPlan(
            tls_id=tls_id,
            program_id="p",
            offset_s=offset,
            phases=(
                PhaseSpec(state="GGrr", duration_s=green_a, kind="green"),
                PhaseSpec(state="yyrr", duration_s=3.0, kind="yellow"),
                PhaseSpec(state="rrrr", duration_s=1.0, kind="all_red"),
                PhaseSpec(state="rrGG", duration_s=green_b, kind="green"),
                PhaseSpec(state="rryy", duration_s=3.0, kind="yellow"),
                PhaseSpec(state="rrrr", duration_s=1.0, kind="all_red"),
            ),
        )

    return CorridorPlan(
        plan_id="p",
        plans=(signal_plan("A", 0.0, 40.0, 30.0), signal_plan("B", 12.5, 35.0, 35.0)),
        provenance=PlanProvenance(source="explicit"),
    )


def test_timing_of_a_static_plan_lists_cycle_offset_and_greens_per_tls():
    assert _timing(_static_plan()) == {
        "cycle_s": {"A": 78.0, "B": 78.0},
        "offsets_s": {"A": 0.0, "B": 12.5},
        "greens_s": {"A": [40.0, 30.0], "B": [35.0, 35.0]},
    }


def test_timing_of_a_nema_plan_has_cycle_and_offsets_but_no_greens():
    entries = tuple(
        NemaSignalEntry(
            tls_id=tls_id,
            intersection={},
            pattern={"pattern_id": "p1", "cycle_s": cycle, "offset_s": offset},
        )
        for tls_id, cycle, offset in (("A", 100, 10), ("B", 120, 35.5))
    )
    timing = _timing(NemaCorridorPlan(plan_id="n", entries=entries))
    assert timing == {"cycle_s": {"A": 100.0, "B": 120.0}, "offsets_s": {"A": 10.0, "B": 35.5}}
    assert all(isinstance(v, float) for key in timing for v in timing[key].values())


def test_optimizer_choices_and_bo_mode_for_bayes_corridor():
    assert OPTIMIZER_CHOICES == (
        "auto", "exhaustive", "bayes_offsets", "bayes_splits", "bayes_corridor",
    )
    assert _bo_mode("bayes_corridor", 3) == "corridor"
    assert _bo_mode("bayes_corridor", 2) == "corridor"
    with pytest.raises(ValueError, match="bayes_corridor.*>= 2 TLS"):
        _bo_mode("bayes_corridor", 1)
    # "auto" still picks offsets for corridors and splits for a single intersection.
    assert (_bo_mode("auto", 3), _bo_mode("auto", 1)) == ("offsets", "splits")
    assert _bo_mode("exhaustive", 3) is None


def test_cycle_bounds_helper_follows_search_cycle(tmp_path):
    base = dict(tmc_json=tmp_path / "t.json", base_sumocfg=None, output_dir=tmp_path)
    assert _cycle_bounds(CorridorOptimizationConfig(**base)) is None
    cfg = CorridorOptimizationConfig(**base, search_cycle=True, cycle_min_s=60, cycle_max_s=120)
    assert _cycle_bounds(cfg) == (60.0, 120.0)


# ---------------------------------------------------------------------------
# cnde coarse-path inline/single-worker constraint: vehicle_behavior="cnde"
# with n_workers>1 under the spawn pool would start concurrent GPU workers,
# each loading the 972MB checkpoint per trial. __post_init__ rejects that
# combination unless TERASIM_CITY_INLINE_EVAL forces single-process
# evaluation (matching what BatchEvaluator's own inline=None default actually
# does at the coarse construction site in run_corridor_optimization).
# ---------------------------------------------------------------------------

def test_cnde_coarse_behavior_with_multiple_workers_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv(INLINE_EVAL_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match="n_workers"):
        CorridorOptimizationConfig(
            tmc_json=write_tmc(tmp_path, demo_tmc()),
            base_sumocfg=DEMO_CFG,
            output_dir=tmp_path / "out",
            vehicle_behavior="cnde",
            n_workers=2,
        )
    # Never got far enough to touch the filesystem/output_dir: the failure is
    # at config construction, not mid-run.
    assert not (tmp_path / "out").exists()


def test_cnde_coarse_behavior_single_worker_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv(INLINE_EVAL_ENV_VAR, raising=False)
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        vehicle_behavior="cnde",
        n_workers=1,
    )
    assert cfg.vehicle_behavior == "cnde"
    assert cfg.n_workers == 1


def test_cnde_coarse_behavior_multiple_workers_allowed_with_inline_eval_env(
    tmp_path, monkeypatch
):
    # BatchEvaluator.evaluate never builds the spawn pool when the env var
    # forces inline mode, so n_workers > 1 is unused, not unsafe, in that
    # case -- the guard deliberately lets this combination through.
    monkeypatch.setenv(INLINE_EVAL_ENV_VAR, "1")
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        vehicle_behavior="cnde",
        n_workers=4,
    )
    assert cfg.vehicle_behavior == "cnde"
    assert cfg.n_workers == 4


def test_cnde_coarse_behavior_runs_up_to_cnde_max_workers(tmp_path, monkeypatch):
    # A CNDE request raises the limit to the deployment's GPU budget
    # (TERASIM_CITY_CNDE_MAX_WORKERS in the service): up to that many workers
    # are a real pool, one more is still rejected.
    monkeypatch.delenv(INLINE_EVAL_ENV_VAR, raising=False)
    fields = dict(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        vehicle_behavior="cnde",
        cnde_max_workers=3,
    )
    assert CorridorOptimizationConfig(**fields, n_workers=3).n_workers == 3
    with pytest.raises(ValueError, match="cnde_max_workers=3"):
        CorridorOptimizationConfig(**fields, n_workers=4)
    with pytest.raises(ValueError, match="cnde_max_workers must be >= 1"):
        CorridorOptimizationConfig(**dict(fields, cnde_max_workers=0), n_workers=1)


def test_cnde_refine_with_multi_worker_sumo_passive_coarse_remains_legal(tmp_path):
    """The refine path builds its own BatchEvaluator sized by
    refine_n_workers (_run_refine_stage; inline single process by default),
    never by cfg.n_workers, so a multi-worker coarse search under
    sumo_passive with a cnde REFINE pass -- the demo config's exact shape --
    must stay legal: the coarse-path guard only checks cfg.vehicle_behavior
    (the coarse behavior), never cfg.refine_vehicle_behavior."""
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        n_workers=3,
        vehicle_behavior="sumo_passive",
        refine_top_k=2,
        refine_vehicle_behavior="cnde",
    )
    assert cfg.n_workers == 3
    assert cfg.vehicle_behavior == "sumo_passive"
    assert cfg.refine_vehicle_behavior == "cnde"
    assert cfg.refine_n_workers == 1


def test_cnde_refine_n_workers_above_one_is_legal_without_inline_env(
    tmp_path, monkeypatch
):
    """refine_n_workers > 1 is the refine stage's own, explicitly validated
    opt-in to concurrent cnde processes (identical results per process since
    the 2026-09-23 hash-seed fix -- see _run_refine_stage): the coarse guard
    must not fire for it, and TERASIM_CITY_INLINE_EVAL -- which the refine
    stage ignores and which would collapse the coarse pool -- must not be
    required."""
    monkeypatch.delenv(INLINE_EVAL_ENV_VAR, raising=False)
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        n_workers=3,
        vehicle_behavior="sumo_passive",
        refine_top_k=3,
        refine_vehicle_behavior="cnde",
        refine_n_workers=4,
    )
    assert cfg.refine_n_workers == 4
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# counts_source: turning counts derived from the scenario's own routes
# ---------------------------------------------------------------------------

# A customer-style demand on the demo net: its three signals sit on one east-west street
# (W1 -> J1 -> J2 -> J3 -> E1), the *_J<n>_* edges are each signal's side street.
SCENARIO_ROUTES = """<routes>
  <vType id="car" length="4.5" maxSpeed="20"/>
  <route id="eastbound" edges="EB_1 EB_2 EB_3 EB_4"/>
  <route id="westbound" edges="WB_1 WB_2 WB_3 WB_4"/>
  <route id="northbound" edges="NB_J2_0 NB_J2_1 NB_J2_2 NB_J2_3"/>
  <flow id="f_eb" route="eastbound" type="car" begin="0" end="3600" vehsPerHour="600"/>
  <flow id="f_wb" route="westbound" type="car" begin="0" end="3600" vehsPerHour="400"/>
  <flow id="f_nb" route="northbound" type="car" begin="0" end="3600" vehsPerHour="100"/>
</routes>
"""

# The same street, but J2's side street carries nine times the through traffic of the main street.
NORTH_SOUTH_HEAVY_ROUTES = """<routes>
  <route id="eastbound" edges="EB_1 EB_2 EB_3 EB_4"/>
  <route id="northbound" edges="NB_J2_0 NB_J2_1 NB_J2_2 NB_J2_3"/>
  <route id="southbound" edges="SB_J2_0 SB_J2_1 SB_J2_2 SB_J2_3"/>
  <flow id="f_eb" route="eastbound" begin="0" end="3600" vehsPerHour="100"/>
  <flow id="f_nb" route="northbound" begin="0" end="3600" vehsPerHour="900"/>
  <flow id="f_sb" route="southbound" begin="0" end="3600" vehsPerHour="900"/>
</routes>
"""


def _scenario_with_routes(
    tmp_path: Path, routes_xml: str = SCENARIO_ROUTES, end: int = 3600
) -> Path:
    """A base scenario with its own demand: the demo net and signals plus a route file."""
    routes = tmp_path / "customer.rou.xml"
    routes.write_text(routes_xml, encoding="utf-8")
    cfg = tmp_path / "customer.sumocfg"
    cfg.write_text(
        f"""<configuration><input>
<net-file value="{DEMO_DIR / 'demo.net.xml'}"/>
<route-files value="{routes}"/>
<additional-files value="{DEMO_DIR / 'demo.tls.xml'}"/>
</input><time><begin value="0"/><end value="{end}"/></time></configuration>""",
        encoding="utf-8",
    )
    return cfg


def _routes_cfg(tmp_path: Path, base: Path, **overrides) -> CorridorOptimizationConfig:
    """Seeds from the scenario's own routes: placeholder TMC, derived counts and flows."""
    fields = dict(
        tmc_json=write_tmc(tmp_path, demo_tmc(), name="placeholder.json"),
        base_sumocfg=base,
        output_dir=tmp_path / "out",
        tls_ids=("J1", "J2", "J3"),
        optimizer="exhaustive",
        counts_source="scenario_routes",
        flow_source="scenario_routes",
        sim_end=60.0,
        n_workers=1,
    )
    fields.update(overrides)
    return CorridorOptimizationConfig(**fields)


@requires_demo_assets
@requires_sumolib
def test_counts_source_scenario_routes_writes_derived_tmc(monkeypatch, tmp_path):
    base = _scenario_with_routes(tmp_path)
    placeholder = write_tmc(tmp_path, demo_tmc(), name="placeholder.json")
    _install_fake_evaluator(monkeypatch, lambda plan_id, offsets: 100.0)
    cfg = CorridorOptimizationConfig(
        tmc_json=placeholder, base_sumocfg=base, output_dir=tmp_path / "out",
        tls_ids=("J1", "J2", "J3"), optimizer="exhaustive", counts_source="scenario_routes",
        flow_source="scenario_routes",
    )
    result = run_corridor_optimization(cfg)
    derived = tmp_path / "out" / "derived_tmc.json"
    assert derived.exists() and result["config"]["tmc_json"] == str(derived)
    assert result["config"]["counts_source"] == "scenario_routes"
    assert [
        i["intersection_id"] for i in json.loads(derived.read_text())["intersections"]
    ] == ["J1", "J2", "J3"]


@requires_demo_assets
@requires_sumolib
def test_counts_source_tmc_is_the_default_and_reads_the_given_file(monkeypatch, tmp_path):
    _install_fake_evaluator(monkeypatch, lambda plan_id, offsets: 100.0)
    tmc_json = write_tmc(tmp_path, demo_tmc())
    cfg = CorridorOptimizationConfig(
        tmc_json=tmc_json, base_sumocfg=DEMO_CFG, output_dir=tmp_path / "out",
        tls_ids=("J2",), optimizer="exhaustive", sim_end=60.0, n_workers=1,
    )
    assert cfg.counts_source == "tmc"
    result = run_corridor_optimization(cfg)
    assert result["config"]["counts_source"] == "tmc"
    assert result["config"]["tmc_json"] == str(tmc_json)
    assert not (tmp_path / "out" / "derived_tmc.json").exists()


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (
            {"counts_source": "loop_detectors"},
            r"counts_source must be one of \('tmc', 'scenario_routes'\)",
        ),
        ({"counts_source": "scenario_routes", "base_sumocfg": None}, "needs base_sumocfg$"),
        ({"counts_source": "scenario_routes", "tls_ids": ()}, "needs tls_ids$"),
        (
            {"counts_source": "scenario_routes", "base_sumocfg": None, "tls_ids": ()},
            "needs base_sumocfg and tls_ids",
        ),
    ],
)
def test_counts_source_is_validated_at_config_time(tmp_path, fields, message):
    given = dict(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J1",),
    )
    given.update(fields)
    with pytest.raises(ValueError, match=message):
        CorridorOptimizationConfig(**given)
    assert not (tmp_path / "out").exists()


@requires_demo_assets
@requires_sumolib
def test_derived_counts_replace_the_placeholder_downstream(monkeypatch, tmp_path):
    base = _scenario_with_routes(tmp_path, NORTH_SOUTH_HEAVY_ROUTES)
    _install_fake_evaluator(monkeypatch, lambda plan_id, offsets: 100.0)

    # The placeholder is the demo TMC, which is east-west heavy at J2 (740 vs 270 veh/h).
    from_tmc = run_corridor_optimization(
        _routes_cfg(tmp_path, base, counts_source="tmc", output_dir=tmp_path / "out_tmc")
    )
    east_west, north_south = from_tmc["baseline"]["timing"]["greens_s"]["J2"]
    assert east_west > north_south

    from_routes = run_corridor_optimization(_routes_cfg(tmp_path, base))
    east_west, north_south = from_routes["baseline"]["timing"]["greens_s"]["J2"]
    assert north_south > east_west  # the seed follows the scenario's routes (100 vs 900 veh/h)

    derived = tmp_path / "out" / "derived_tmc.json"
    assert json.loads(derived.read_text())["corridor_id"] == "demo_arterial"  # the placeholder's
    assert json.loads(derived.read_text())["timestamp_start"] == "2026-07-01T08:00:00"  # its too
    assert [(i["intersection_id"], i["tls_id"]) for i in from_routes["resolved_corridor"]] == [
        ("J1", "J1"), ("J2", "J2"), ("J3", "J3"),
    ]
    metadata = json.loads((tmp_path / "out" / "scenario" / "metadata.json").read_text())
    assert metadata["source"]["tmc_json"] == str(derived)
    assert from_routes["config"]["tmc_json"] == str(derived)
    assert from_tmc["config"]["tmc_json"] == str(tmp_path / "placeholder.json")


@requires_demo_assets
@requires_sumolib
def test_derived_counts_cover_the_simulated_window_only(monkeypatch, tmp_path):
    routes = """<routes>
  <route id="eastbound" edges="EB_1 EB_2"/>
  <route id="westbound" edges="WB_3 WB_4"/>
  <flow id="am" route="eastbound" begin="0" end="1800" vehsPerHour="600"/>
  <flow id="later" route="westbound" begin="1800" end="5400" vehsPerHour="300"/>
</routes>"""
    base = _scenario_with_routes(tmp_path, routes, end=7200)  # the sumocfg's own window is 7200 s
    _install_fake_evaluator(monkeypatch, lambda plan_id, offsets: 100.0)

    def j1_east_west(sim_end: float) -> tuple[int, int]:
        out = tmp_path / f"out_{int(sim_end)}"
        run_corridor_optimization(
            _routes_cfg(tmp_path, base, tls_ids=("J1",), sim_end=sim_end, output_dir=out)
        )
        counts = json.loads((out / "derived_tmc.json").read_text())["intersections"][0]["counts"]
        return counts["East"]["Straight"], counts["West"]["Straight"]

    # The trials simulate [0, sim_end): a flow that starts after it is not demand they will see.
    assert j1_east_west(1800.0) == (600, 0)
    assert j1_east_west(7200.0) == (600, 300)


@requires_demo_assets
@requires_sumolib
def test_counts_source_scenario_routes_needs_no_placeholder_file(monkeypatch, tmp_path):
    _install_fake_evaluator(monkeypatch, lambda plan_id, offsets: 100.0)
    cfg = _routes_cfg(
        tmp_path, _scenario_with_routes(tmp_path), tmc_json=tmp_path / "does_not_exist.json"
    )
    result = run_corridor_optimization(cfg)
    derived = tmp_path / "out" / "derived_tmc.json"
    assert json.loads(derived.read_text())["corridor_id"] == "scenario"
    assert json.loads(derived.read_text())["timestamp_start"] == "1970-01-01T00:00:00"
    assert result["config"]["tmc_json"] == str(derived)


@requires_demo_assets
@requires_sumolib
def test_signal_group_overrides_reach_the_derived_counts(monkeypatch, tmp_path):
    through_links = [
        lm.link_index
        for lm in derive_link_movements(DEMO_DIR / "demo.net.xml", "J2")
        if lm.movement == "EB-through"
    ]
    overrides = tmp_path / "signal_groups.json"
    overrides.write_text(
        json.dumps({"J2": {str(link): "EB-left" for link in through_links}}), encoding="utf-8"
    )
    _install_fake_evaluator(monkeypatch, lambda plan_id, offsets: 100.0)
    cfg = _routes_cfg(
        tmp_path, _scenario_with_routes(tmp_path), signal_groups_overrides=overrides
    )
    run_corridor_optimization(cfg)
    counts = json.loads((tmp_path / "out" / "derived_tmc.json").read_text())["intersections"][1][
        "counts"
    ]
    # J2's eastbound flow (600 veh/h) is now counted as left turns.
    assert counts["East"]["Straight"] == 0 and counts["East"]["Left"] == 600


# ---------------------------------------------------------------------------
# seed_from_scenario_programs: the customer's own signal timing is the baseline
# ---------------------------------------------------------------------------

def _programs_cfg(tmp_path: Path, **overrides) -> CorridorOptimizationConfig:
    """3-TLS demo corridor whose first seed is the scenario's own <tlLogic> programs."""
    fields = dict(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J1", "J2", "J3"),
        optimizer="exhaustive",
        seed_from_scenario_programs=True,
        sim_end=60.0,
        n_workers=1,
    )
    fields.update(overrides)
    return CorridorOptimizationConfig(**fields)


@requires_demo_assets
@requires_sumolib
def test_seed_from_scenario_programs_is_the_baseline(monkeypatch, tmp_path):
    tmc_path = write_tmc(tmp_path, demo_tmc())
    _install_fake_plan_evaluator(
        monkeypatch, lambda plan: 100.0 if plan["plan_id"] == "scenario_programs" else 90.0
    )
    cfg = CorridorOptimizationConfig(
        tmc_json=tmc_path, base_sumocfg=DEMO_CFG, output_dir=tmp_path / "out",
        tls_ids=("J1", "J2", "J3"), optimizer="exhaustive", seed_from_scenario_programs=True,
    )
    result = run_corridor_optimization(cfg)
    assert result["baseline"]["plan_id"] == "scenario_programs"
    assert result["seed"]["source"] == "sumo_tls"
    assert result["best"]["plan_id"] == "webster_init"
    assert result["n_trials"] == 2


@requires_demo_assets
@requires_sumolib
def test_scenario_programs_are_not_a_seed_unless_asked_for(monkeypatch, tmp_path):
    _install_pool_evaluator(monkeypatch, {})
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()), base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out", tls_ids=("J1", "J2", "J3"), optimizer="exhaustive",
        sim_end=60.0, n_workers=1,
    )
    assert cfg.seed_from_scenario_programs is False
    result = run_corridor_optimization(cfg)
    assert [t["plan_id"] for t in result["trials"]] == ["webster_init"]
    assert result["seed"]["source"] == "webster_init"
    assert result["config"]["seed_from_scenario_programs"] is False


@requires_demo_assets
@requires_sumolib
def test_unbeaten_scenario_program_stays_the_best_and_is_exported(monkeypatch, tmp_path):
    _install_fake_plan_evaluator(
        monkeypatch, lambda plan: 80.0 if plan["plan_id"] == "scenario_programs" else 90.0
    )
    result = run_corridor_optimization(_programs_cfg(tmp_path))

    assert result["config"]["seed_from_scenario_programs"] is True
    assert result["best"]["plan_id"] == result["baseline"]["plan_id"] == "scenario_programs"
    assert result["best"]["source"] == "sumo_tls"
    assert result["nema_export"] == {"exported": True}
    # The baseline is the demo's own program, the additional file's 30/3/30/3 (66 s) rather
    # than the net's 42/3/42/3, at every signal.
    assert result["baseline"]["timing"] == {
        "cycle_s": {"J1": 66.0, "J2": 66.0, "J3": 66.0},
        "offsets_s": {"J1": 0.0, "J2": 0.0, "J3": 0.0},
        "greens_s": {"J1": [30.0, 30.0], "J2": [30.0, 30.0], "J3": [30.0, 30.0]},
    }
    exported = json.loads((tmp_path / "out" / "nema_plan.json").read_text(encoding="utf-8"))
    assert [item["tls_id"] for item in exported["intersections"]] == ["J1", "J2", "J3"]


@requires_demo_assets
@requires_sumolib
def test_signal_group_overrides_reach_the_scenario_program_seed(monkeypatch, tmp_path):
    through_links = [
        lm.link_index
        for lm in derive_link_movements(DEMO_DIR / "demo.net.xml", "J2")
        if lm.movement == "EB-through"
    ]
    overrides = tmp_path / "signal_groups.json"
    overrides.write_text(
        json.dumps({"J2": {str(link): "EB-left" for link in through_links}}), encoding="utf-8"
    )
    _install_fake_plan_evaluator(
        monkeypatch, lambda plan: 80.0 if plan["plan_id"] == "scenario_programs" else 90.0
    )
    run_corridor_optimization(_programs_cfg(tmp_path, signal_groups_overrides=overrides))

    # The customer's plan wins, so its movements are what the NEMA export lists.
    exported = json.loads((tmp_path / "out" / "nema_plan.json").read_text(encoding="utf-8"))
    movements = {
        item["tls_id"]: {m for row in item["phases"] for m in row["movements"]}
        for item in exported["intersections"]
    }
    assert "EB-through" in movements["J1"]
    assert "EB-through" not in movements["J2"] and "EB-left" in movements["J2"]


@requires_demo_assets
@requires_sumolib
def test_invalid_scenario_program_hands_the_baseline_to_the_next_valid_seed(monkeypatch, tmp_path):
    _install_pool_evaluator(monkeypatch, {}, invalid=("scenario_programs",))
    result = run_corridor_optimization(_programs_cfg(tmp_path))

    assert [(t["plan_id"], t["valid"]) for t in result["trials"]] == [
        ("scenario_programs", False), ("webster_init", True),
    ]
    assert result["baseline"]["plan_id"] == "webster_init"
    assert result["seed"]["source"] == "webster_init"


@requires_demo_assets
@requires_sumolib
@pytest.mark.parametrize("scenario_valid", [True, False])
def test_scenario_program_leads_a_baseline_sheet_unless_it_is_invalid(
    monkeypatch, tmp_path, scenario_valid
):
    _install_pool_evaluator(
        monkeypatch, {}, invalid=() if scenario_valid else ("scenario_programs",)
    )
    result = run_corridor_optimization(
        _corridor_cfg(tmp_path, optimizer="exhaustive", seed_from_scenario_programs=True)
    )
    assert [(t["plan_id"], t["source"]) for t in result["trials"]] == [
        ("scenario_programs", "sumo_tls"), ("baseline", "baseline_sheet"),
    ]
    assert result["baseline"]["plan_id"] == ("scenario_programs" if scenario_valid else "baseline")
    assert result["seed"]["source"] == ("sumo_tls" if scenario_valid else "baseline_sheet")


@requires_demo_assets
@requires_sumolib
@pytest.mark.parametrize(
    ("optimizer", "tls_ids", "prefix"),
    [
        ("auto", ("J1", "J2", "J3"), "bo_0"),  # offset search
        ("auto", ("J2",), "bo_split_"),
        ("bayes_corridor", ("J1", "J2", "J3"), "bo_corr_"),
    ],
)
def test_every_search_accepts_the_scenario_program_as_its_starting_plan(
    monkeypatch, tmp_path, optimizer, tls_ids, prefix
):
    _install_fake_plan_evaluator(
        monkeypatch, lambda plan: 50.0 if plan["plan_id"] == "scenario_programs" else 70.0
    )
    result = run_corridor_optimization(
        _programs_cfg(
            tmp_path, optimizer=optimizer, tls_ids=tls_ids, budget=Budget(max_trials=3, patience=0)
        )
    )
    assert result["baseline"]["plan_id"] == result["best"]["plan_id"] == "scenario_programs"
    assert result["n_trials"] == 2 + 3
    assert all(t["plan_id"].startswith(prefix) for t in result["trials"][2:])


@requires_demo_assets
@requires_sumolib
def test_scenario_programs_are_read_from_the_auto_selected_map(monkeypatch, tmp_path):
    """No explicit base_sumocfg: the programs come from the map the run resolved."""
    from terasim_city.maps.store import MapStore

    root = tmp_path / "store"
    MapStore(root).ingest(DEMO_CFG)
    _install_fake_plan_evaluator(monkeypatch, lambda plan: 100.0)
    result = run_corridor_optimization(
        _programs_cfg(tmp_path, base_sumocfg=None, plan_data_root=root)
    )
    assert result["config"]["map_id"] is not None
    assert result["baseline"]["plan_id"] == "scenario_programs"
    assert result["seed"]["source"] == "sumo_tls"
    # The store held no signal plan for the corridor; the scenario's programs were asked
    # for, so the run did not "fall back" to them.
    note = result["seed"]["note"]
    assert note.startswith("a plan store was configured but was not used (")
    assert "fell back" not in note
    assert note.endswith(
        "; the run used the scenario's own signal programs, as requested "
        "(seed_from_scenario_programs)"
    )


# ---------------------------------------------------------------------------
# customer programs that run a different cycle at every signal: the run survives
# ---------------------------------------------------------------------------

def _edit_j2_program(tmp_path: Path, edit) -> Path:
    """The demo map with J2's <tlLogic> edited by ``edit(element)``; returns the sumocfg."""
    customer_map = tmp_path / "customer_map"
    shutil.copytree(DEMO_DIR, customer_map)
    tls_path = customer_map / "demo.tls.xml"
    tree = ET.parse(tls_path)
    edit(next(node for node in tree.getroot().findall("tlLogic") if node.attrib["id"] == "J2"))
    tree.write(tls_path)
    return customer_map / "demo.sumocfg"


def _shorten_greens(tls) -> None:
    """J2 runs a 46 s cycle (20 s greens) while J1 and J3 keep the demo's 66 s."""
    for phase in tls.findall("phase"):
        if phase.attrib["duration"] == "30":
            phase.set("duration", "20")


def _single_green_stage(tls) -> None:
    """J2 serves the east-west movements only: one green stage, nothing to split."""
    for phase in list(tls):
        tls.remove(phase)
    for duration, state in (
        ("40", "GGGgrrrGGGgrrr"), ("3", "yyyyrrryyyyrrr"), ("3", "rrrrrrrrrrrrrr"),
    ):
        ET.SubElement(tls, "phase", {"duration": duration, "state": state})


def _customer_plan_wins(plan: dict) -> float:
    return 50.0 if plan["plan_id"] == "scenario_programs" else 90.0


@requires_demo_assets
@requires_sumolib
def test_result_lists_the_movements_the_winning_plan_never_serves(monkeypatch, tmp_path):
    # The customer's J2 program serves east-west only; it wins, so its never-green
    # north-south movements are reported (spec G2), in the result and on disk.
    _install_fake_plan_evaluator(monkeypatch, _customer_plan_wins)
    cfg = _programs_cfg(tmp_path, base_sumocfg=_edit_j2_program(tmp_path, _single_green_stage))

    result = run_corridor_optimization(cfg)

    assert result["best"]["plan_id"] == "scenario_programs"
    from terasim_city.plans.model import MOVEMENTS

    north_south = {
        lm.movement
        for lm in derive_link_movements(DEMO_DIR / "demo.net.xml", "J2")
        if lm.movement.startswith(("NB-", "SB-"))
    }
    assert result["unserved_movements"] == {"J2": sorted(north_south, key=MOVEMENTS.index)}
    on_disk = json.loads((cfg.output_dir / "corridor_optimization_result.json").read_text())
    assert on_disk["unserved_movements"] == result["unserved_movements"]


@requires_demo_assets
@requires_sumolib
def test_result_reports_no_unserved_movements_for_a_plan_that_serves_all(monkeypatch, tmp_path):
    _install_fake_plan_evaluator(monkeypatch, _customer_plan_wins)
    result = run_corridor_optimization(_programs_cfg(tmp_path))
    assert result["best"]["plan_id"] == "scenario_programs"
    assert result["unserved_movements"] == {}


def _warnings(caplog, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


@requires_demo_assets
@requires_sumolib
def test_unequal_cycles_skip_the_nema_export_but_not_the_run(monkeypatch, tmp_path, caplog):
    _install_fake_plan_evaluator(monkeypatch, _customer_plan_wins)
    cfg = _programs_cfg(tmp_path, base_sumocfg=_edit_j2_program(tmp_path, _shorten_greens))
    with caplog.at_level(logging.WARNING, logger=co.logger.name):
        result = run_corridor_optimization(cfg)

    # The run completes as it always did: result JSON, ledger, the customer's plan the winner.
    result_file = cfg.output_dir / "corridor_optimization_result.json"
    on_disk = json.loads(result_file.read_text(encoding="utf-8"))
    assert on_disk["nema_export"] == result["nema_export"]
    assert result["best"]["plan_id"] == result["baseline"]["plan_id"] == "scenario_programs"
    assert result["n_trials"] == 2 and _read_ledger(cfg.output_dir) == result["trials"]

    # ... but there is no NEMA plan, and the result and the log both say why.
    assert not (cfg.output_dir / "nema_plan.json").exists()
    assert result["nema"]["handoff_ready"] is False
    export = result["nema_export"]
    assert export["exported"] is False
    cycles = ("scenario_programs", "J1=66s", "J2=46s", "J3=66s")
    assert all(text in export["reason"] for text in cycles)
    (warning,) = _warnings(caplog, "NEMA export")
    assert all(text in warning for text in cycles)


@requires_demo_assets
@requires_sumolib
def test_corridor_search_that_cannot_start_from_the_customers_plan_runs_seeds_only(
    monkeypatch, tmp_path, caplog
):
    _install_fake_plan_evaluator(monkeypatch, _customer_plan_wins)
    cfg = _programs_cfg(
        tmp_path, base_sumocfg=_edit_j2_program(tmp_path, _shorten_greens),
        optimizer="bayes_corridor", budget=Budget(max_trials=3, patience=0),
    )
    calls: list[tuple] = []
    with caplog.at_level(logging.WARNING, logger=co.logger.name):
        result = run_corridor_optimization(cfg, progress_callback=lambda *args: calls.append(args))

    assert [t["plan_id"] for t in result["trials"]] == ["scenario_programs", "webster_init"]
    assert result["best"]["plan_id"] == "scenario_programs"
    (warning,) = _warnings(caplog, "not applicable")
    assert "scenario_programs" in warning and "no common cycle" in warning
    assert warning.endswith("seeds only")
    assert result["search"] == {"ran": False, "reason": warning, "warm_starts": {"used": [], "skipped": []}}
    assert calls[-1] == (2, 2, None)  # the unused search budget leaves the progress total


@requires_demo_assets
@requires_sumolib
def test_corridor_search_with_a_searched_cycle_starts_from_the_customers_plan(
    monkeypatch, tmp_path, caplog
):
    _install_fake_plan_evaluator(
        monkeypatch, lambda plan: 50.0 if plan["plan_id"] == "scenario_programs" else 70.0
    )
    cfg = _programs_cfg(
        tmp_path, base_sumocfg=_edit_j2_program(tmp_path, _shorten_greens),
        optimizer="bayes_corridor", search_cycle=True, budget=Budget(max_trials=3, patience=0),
    )
    with caplog.at_level(logging.WARNING, logger=co.logger.name):
        result = run_corridor_optimization(cfg)

    # The searched cycle makes the seed's mixed cycles representable, so the search runs ...
    assert result["n_trials"] == 2 + 3
    assert all(t["plan_id"].startswith("bo_corr_") for t in result["trials"][2:])
    assert not _warnings(caplog, "not applicable")
    assert result["search"] == {
        "ran": True,
        "reason": None,
        # The customer's plan informed the search; the Webster seed could not.
        "warm_starts": {
            "used": ["scenario_programs"],
            "skipped": [{
                "plan_id": "webster_init",
                "reason": "warm_start: plan 'webster_init' has 8s of non-green time at 'J1' "
                "but the seed plan has 6s",
            }],
        },
    }
    # ... and when nothing beats the customer's plan it is still the winner, exported or not.
    assert result["best"]["plan_id"] == "scenario_programs"
    assert result["nema_export"]["exported"] is False


@requires_demo_assets
@requires_sumolib
def test_split_search_that_cannot_start_from_the_customers_plan_runs_seeds_only(
    monkeypatch, tmp_path, caplog
):
    _install_fake_plan_evaluator(monkeypatch, _customer_plan_wins)
    cfg = _programs_cfg(
        tmp_path, base_sumocfg=_edit_j2_program(tmp_path, _single_green_stage),
        tls_ids=("J2",), optimizer="bayes_splits", budget=Budget(max_trials=3, patience=0),
    )
    with caplog.at_level(logging.WARNING, logger=co.logger.name):
        result = run_corridor_optimization(cfg)

    assert [t["plan_id"] for t in result["trials"]] == ["scenario_programs", "webster_init"]
    (warning,) = _warnings(caplog, "not applicable")
    assert "scenario_programs" in warning and ">= 2 green stages" in warning
    assert warning.endswith("seeds only")
    assert result["search"] == {"ran": False, "reason": warning, "warm_starts": {"used": [], "skipped": []}}


@requires_demo_assets
@requires_sumolib
@pytest.mark.parametrize(
    ("optimizer", "prefix"), [("bayes_offsets", ""), ("auto", "auto optimizer: ")]
)
def test_offset_search_that_cannot_start_from_the_customers_plan_runs_seeds_only(
    monkeypatch, tmp_path, caplog, optimizer, prefix
):
    _install_fake_plan_evaluator(monkeypatch, _customer_plan_wins)
    cfg = _programs_cfg(
        tmp_path, base_sumocfg=_edit_j2_program(tmp_path, _shorten_greens),
        optimizer=optimizer, budget=Budget(max_trials=3, patience=0),
    )
    with caplog.at_level(logging.WARNING, logger=co.logger.name):
        result = run_corridor_optimization(cfg)

    # The run completes: the result JSON is written and only the seed trials ran ...
    assert (cfg.output_dir / "corridor_optimization_result.json").exists()
    assert [t["plan_id"] for t in result["trials"]] == ["scenario_programs", "webster_init"]
    assert result["n_trials"] == 2 and result["best"]["plan_id"] == "scenario_programs"
    # ... and the result says the search did not run, for the reason that was logged.
    (warning,) = _warnings(caplog, "not applicable")
    assert warning.startswith(f"{prefix}offset search not applicable")
    assert "scenario_programs" in warning and "no common cycle" in warning
    assert warning.endswith("seeds only")
    assert result["search"] == {"ran": False, "reason": warning, "warm_starts": {"used": [], "skipped": []}}


@requires_demo_assets
@requires_sumolib
def test_auto_split_search_is_skipped_when_the_best_seed_has_a_single_green_stage(
    monkeypatch, tmp_path, caplog
):
    # Webster has two stages, so auto still plans the split search; the customer's one-stage
    # program then turns out to be the best seed.
    _install_fake_plan_evaluator(monkeypatch, _customer_plan_wins)
    cfg = _programs_cfg(
        tmp_path, base_sumocfg=_edit_j2_program(tmp_path, _single_green_stage),
        tls_ids=("J2",), optimizer="auto", budget=Budget(max_trials=3, patience=0),
    )
    with caplog.at_level(logging.WARNING, logger=co.logger.name):
        result = run_corridor_optimization(cfg)

    assert [t["plan_id"] for t in result["trials"]] == ["scenario_programs", "webster_init"]
    (warning,) = _warnings(caplog, "fewer than 2 green stages")
    assert "scenario_programs" in warning
    assert result["search"] == {"ran": False, "reason": warning, "warm_starts": {"used": [], "skipped": []}}


def _stored_nema_seed(monkeypatch, entries) -> None:
    seed = NemaCorridorPlan(
        plan_id="stored", entries=tuple(entries), provenance=PlanProvenance(source="stored_nema")
    )
    monkeypatch.setattr(co, "_load_stored_nema_seed", lambda *_args, **_kwargs: (seed, None))


@requires_demo_assets
@requires_sumolib
@pytest.mark.parametrize(
    ("tls_ids", "entries", "needle"),
    [
        (  # cycles that are neither equal nor multiples of each other cannot be coordinated
            ("J1", "J2", "J3"),
            [("J1", {}, 90.0), ("J2", {}, 90.0), ("J3", {}, 100.0)],
            "cannot be offset-optimized",
        ),
        (  # a cycle shorter than the minimum greens plus clearances leaves nothing to split
            ("J1",),
            [("J1", {"rings": {"ring1": [2, 4], "ring2": [6, 8]},
                     "phases": [{"phase": p} for p in (2, 4, 6, 8)]}, 15.0)],
            "cannot be split-optimized",
        ),
    ],
    ids=["nema-offsets", "nema-splits"],
)
def test_stored_plan_search_that_cannot_start_evaluates_the_baseline_only(
    monkeypatch, tmp_path, caplog, tls_ids, entries, needle
):
    _stored_nema_seed(
        monkeypatch,
        [
            NemaSignalEntry(
                tls_id=tls_id, intersection=intersection,
                pattern={"pattern_id": "p1", "cycle_s": cycle},
            )
            for tls_id, intersection, cycle in entries
        ],
    )
    _install_fake_plan_evaluator(monkeypatch, lambda plan: 50.0)
    cfg = _programs_cfg(
        tmp_path, seed_from_scenario_programs=False, tls_ids=tls_ids, optimizer="auto",
        budget=Budget(max_trials=2, patience=0),
    )
    with caplog.at_level(logging.WARNING, logger=co.logger.name):
        result = run_corridor_optimization(cfg)

    assert result["n_trials"] == 1 and result["seed"]["source"] == "stored_nema"
    (warning,) = _warnings(caplog, needle)
    assert warning.endswith("baseline evaluation only")
    assert result["search"] == {"ran": False, "reason": warning, "warm_starts": {"used": [], "skipped": []}}


# ---------------------------------------------------------------------------
# REAL end-to-end (actual runtime; a few minutes)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.requires_sumo
@requires_demo_assets
@requires_sumolib
@requires_sumo_runtime
def test_demo_corridor_bayes_end_to_end(tmp_path):
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        from_ref="i1",
        to_ref="i3",
        baseline_sheet=write_tmc(tmp_path, baseline_sheet(), name="baseline_sheet.json"),
        optimizer="bayes_offsets",
        budget=Budget(max_trials=4, patience=8),
        sim_end=120.0,
        seed=42,
        n_workers=2,
    )
    result = run_corridor_optimization(cfg)

    assert set(result) == RESULT_KEYS
    assert [item["tls_id"] for item in result["resolved_corridor"]] == [
        "J1",
        "J2",
        "J3",
    ]
    assert result["best"]["score"] is not None
    assert math.isfinite(result["best"]["score"])
    assert result["best"]["moe"]["vehicles_total"] > 0

    ledger = _read_ledger(cfg.output_dir)
    assert len(ledger) == 5  # 1 baseline + 4 BO trials
    assert ledger[0]["plan_id"] == "baseline"
    assert [t["source"] for t in ledger[1:]] == ["bo_iteration"] * 4

    nema_json = cfg.output_dir / "nema_plan.json"
    assert nema_json.exists()
    payload = json.loads(nema_json.read_text(encoding="utf-8"))
    assert payload["handoff_ready"] is True
    assert result["nema"]["handoff_ready"] is True
    assert payload["corridor_id"] == "demo_arterial"


@pytest.mark.integration
@pytest.mark.requires_sumo
@requires_demo_assets
@requires_sumolib
@requires_sumo_runtime
def test_dual_tls_webster_offsets_end_to_end(tmp_path):
    """i1 -> i2 on a map whose additional files program J1 only, through the actual runtime.

    J2 is a signal of the net but carries NO additional-file program, so the
    seed ladder falls to rung 2: a single all-Webster seed (source
    'webster_init'), then the auto optimizer (2 TLS -> offsets) refines it
    with bo_* trials.
    """
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, two_signal_tmc()),
        base_sumocfg=write_partial_program_sumocfg(tmp_path / "map", ("J1",)),
        output_dir=tmp_path / "out",
        from_ref="i1",
        to_ref="i2",
        optimizer="auto",
        budget=Budget(max_trials=4, patience=8),
        sim_end=120.0,
        seed=42,
        n_workers=2,
    )
    from terasim_city.signals import discover_tls_ids

    # Premise: only J1 has a candidate program in the additional files.
    assert discover_tls_ids(cfg.base_sumocfg) == ("J1",)
    result = run_corridor_optimization(cfg)

    assert set(result) == RESULT_KEYS
    assert [item["tls_id"] for item in result["resolved_corridor"]] == ["J1", "J2"]

    ledger = _read_ledger(cfg.output_dir)
    assert ledger == result["trials"]
    assert len(ledger) == 5  # 1 Webster seed + 4 BO offset trials
    # Seed rows come from the Webster initialiser, never from additional-file
    # programs (J2 has none -> the common program set is empty).
    assert ledger[0]["plan_id"] == "webster_init"
    assert ledger[0]["source"] == "webster_init"
    assert all(t["plan_id"].startswith("bo_") for t in ledger[1:])
    assert all(t["source"] == "bo_iteration" for t in ledger[1:])
    # Offset search: BO trials carry offsets for both corridor TLS.
    for trial in ledger[1:]:
        assert set(trial["offsets"]) == {"J1", "J2"}

    assert result["baseline"]["plan_id"] == "webster_init"
    assert result["best"]["score"] is not None
    assert math.isfinite(result["best"]["score"])
    assert result["best"]["moe"]["vehicles_total"] > 0

    nema_json = cfg.output_dir / "nema_plan.json"
    assert nema_json.exists()


@pytest.mark.integration
@pytest.mark.requires_sumo
@requires_demo_assets
@requires_sumolib
@requires_sumo_runtime
def test_customer_routes_give_the_counts_and_the_demand_end_to_end(tmp_path):
    """The scenario's own route file seeds the Webster plan and is what the trials simulate."""
    cfg = _routes_cfg(
        tmp_path,
        _scenario_with_routes(tmp_path),
        optimizer="bayes_offsets",
        budget=Budget(max_trials=2, patience=0),
        sim_end=300.0,
        n_workers=2,
    )
    result = run_corridor_optimization(cfg)

    derived = tmp_path / "out" / "derived_tmc.json"
    assert result["config"]["tmc_json"] == str(derived)
    j2_counts = json.loads(derived.read_text(encoding="utf-8"))["intersections"][1]["counts"]
    assert (j2_counts["East"]["Straight"], j2_counts["West"]["Straight"]) == (600, 400)
    assert j2_counts["North"]["Straight"] == 100  # the side street's flow, as in the route file
    assert result["seed"]["source"] == "webster_init" and result["n_trials"] == 3
    assert result["best"]["moe"]["vehicles_total"] > 0
    assert math.isfinite(result["best"]["score"])


@pytest.mark.integration
@pytest.mark.requires_sumo
@requires_demo_assets
@requires_sumolib
@requires_sumo_runtime
def test_scenario_programs_seed_runs_in_sumo_end_to_end(tmp_path):
    """The customer's own program is a runnable SUMO program: it is simulated as the baseline."""
    result = run_corridor_optimization(_programs_cfg(tmp_path, sim_end=300.0, n_workers=2))

    assert result["seed"]["source"] == "sumo_tls"
    assert [(t["plan_id"], t["valid"]) for t in result["trials"]] == [
        ("scenario_programs", True), ("webster_init", True),
    ]
    assert result["baseline"]["plan_id"] == "scenario_programs"
    assert result["baseline"]["score"] == pytest.approx(1.0)
    assert result["baseline"]["moe"]["vehicles_total"] > 0
    assert math.isfinite(result["best"]["score"])


@pytest.mark.integration
@pytest.mark.requires_sumo
@requires_demo_assets
@requires_sumolib
@requires_sumo_runtime
def test_bundled_demo_scenario_runs_every_customer_switch_together(tmp_path):
    """The in-image scenario self-test's switches, in process and in real SUMO.

    The bundled demo scenario (the five files the image bakes) with the joint corridor
    search over a searched cycle, per-approach seeds, the scenario's own routes for the
    demand and the counts, and its own programs as the baseline.
    """
    cfg = CorridorOptimizationConfig(
        tmc_json=DEMO_DIR / "placeholder_tmc.json",
        base_sumocfg=DEMO_DIR / "demo_scenario.sumocfg",
        output_dir=tmp_path / "out",
        tls_ids=("J1", "J2", "J3"),
        optimizer="bayes_corridor",
        search_cycle=True,
        cycle_min_s=45.0,
        cycle_max_s=180.0,
        stage_mode="per_approach",
        flow_source="scenario_routes",
        counts_source="scenario_routes",
        seed_from_scenario_programs=True,
        budget=Budget(max_trials=4, patience=0),
        sim_end=300.0,
        n_workers=2,
    )
    result = run_corridor_optimization(cfg)

    signals = {"J1", "J2", "J3"}
    assert set(result) == RESULT_KEYS
    assert result["seed"]["source"] == "sumo_tls"
    assert [t["plan_id"] for t in result["trials"][:2]] == ["scenario_programs", "webster_init"]
    assert all(t["valid"] for t in result["trials"])
    assert result["n_trials"] == 2 + 4
    assert all(t["plan_id"].startswith("bo_corr_") for t in result["trials"][2:])
    # timing: per-signal cycles (and greens, static plans) of both sides.
    for side in ("baseline", "best"):
        timing = result[side]["timing"]
        assert set(timing["cycle_s"]) == set(timing["offsets_s"]) == signals
        assert set(timing["greens_s"]) == signals
    # search: it ran, from the best seed.
    search = result["search"]
    assert search["ran"] is True and search["reason"] is None
    assert search["warm_starts"]["used"][:1] in (["scenario_programs"], ["webster_init"])
    # nema_export: one coordinated plan whose splits fill each signal's cycle.
    assert result["nema_export"] == {"exported": True}
    nema = json.loads((cfg.output_dir / "nema_plan.json").read_text(encoding="utf-8"))
    cycle = nema["coordination"]["cycle_length_s"]
    for intersection in nema["intersections"]:
        assert sum(row["split_s"] for row in intersection["phases"]) == pytest.approx(cycle)
    # junction_delay_s: the edge-based delay of every signal, next to the all-demand MOE.
    for side in ("baseline", "best"):
        moe = result[side]["moe"]
        assert moe["vehicles_total"] > 0  # really simulated
        assert set(moe["junction_delay_s"]) == signals
        assert math.isfinite(moe["corridor_junction_delay_s"])
        assert math.isfinite(moe["control_delay_s_per_veh"])
    # unserved_movements: every plan here serves every movement.
    assert result["unserved_movements"] == {}
    config = result["config"]
    assert (config["flow_source"], config["counts_source"]) == ("scenario_routes", "scenario_routes")
    assert config["tmc_json"] == str(cfg.output_dir / "derived_tmc.json")



def _demo_with_a_fractional_net_program(tmp_path: Path) -> Path:
    """The demo scenario whose net carries a 42.5 s phase, which sumolib < 1.27 rejects."""
    demo = tmp_path / "fractional_demo"
    shutil.copytree(DEMO_DIR, demo)
    net = demo / "demo.net.xml"
    text = net.read_text(encoding="utf-8")
    assert '<phase duration="42"' in text
    net.write_text(text.replace('<phase duration="42"', '<phase duration="42.5"', 1), encoding="utf-8")
    return demo / "demo.sumocfg"


@requires_demo_assets
@requires_sumolib
def test_the_run_warns_once_about_unreadable_programs_and_quiets_its_trials(
    monkeypatch, tmp_path, caplog
):
    import sumolib

    from terasim.simulator import QUIET_PROGRAM_FALLBACK_ENV

    base = _demo_with_a_fractional_net_program(tmp_path)
    try:
        sumolib.net.readNet(str(base.parent / "demo.net.xml"), withInternal=True, withPrograms=True)
    except ValueError:
        pass
    else:
        pytest.skip("this sumolib reads fractional phase durations")
    monkeypatch.delenv(QUIET_PROGRAM_FALLBACK_ENV, raising=False)
    seen: list = []

    def delay_fn(plan: dict) -> float:
        seen.append(os.environ.get(QUIET_PROGRAM_FALLBACK_ENV))
        return 50.0

    _install_fake_plan_evaluator(monkeypatch, delay_fn)
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=base,
        output_dir=tmp_path / "out",
        tls_ids=("J1", "J2", "J3"),
        optimizer="bayes_offsets",
        budget=Budget(max_trials=3, patience=0),
        sim_end=60.0,
        n_workers=1,
    )
    with caplog.at_level(logging.DEBUG, logger=co.logger.name):
        result = run_corridor_optimization(cfg)

    assert result["n_trials"] == 1 + 3
    warnings = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "could not parse the TLS programs" in r.getMessage()
    ]
    assert len(warnings) == 1 and "demo.net.xml" in warnings[0]
    # Every trial ran with the variable set; the run restores the environment.
    assert seen == ["1"] * 4
    assert QUIET_PROGRAM_FALLBACK_ENV not in os.environ


@requires_demo_assets
@requires_sumolib
def test_a_readable_net_neither_warns_nor_quiets_the_trials(monkeypatch, tmp_path, caplog):
    from terasim.simulator import QUIET_PROGRAM_FALLBACK_ENV

    monkeypatch.setenv(QUIET_PROGRAM_FALLBACK_ENV, "previous")
    seen: list = []
    _install_fake_plan_evaluator(
        monkeypatch, lambda plan: seen.append(os.environ.get(QUIET_PROGRAM_FALLBACK_ENV)) or 50.0
    )
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J2",),
        optimizer="exhaustive",
        sim_end=60.0,
        n_workers=1,
    )
    with caplog.at_level(logging.WARNING, logger=co.logger.name):
        run_corridor_optimization(cfg)

    assert not [r for r in caplog.records if "could not parse the TLS programs" in r.getMessage()]
    assert seen == ["previous"]  # untouched
    assert os.environ[QUIET_PROGRAM_FALLBACK_ENV] == "previous"



def _j2_program(*states: str) -> CorridorPlan:
    kinds = {"G": "green", "g": "green", "y": "yellow"}
    phases = []
    for state in states:
        kind = next((kinds[c] for c in state if c in "Gg"), None) or (
            "yellow" if "y" in state else "all_red"
        )
        phases.append(PhaseSpec(state=state, duration_s=10.0, kind=kind))
    return CorridorPlan(
        plan_id="customer",
        plans=(SignalPlan(tls_id="J2", program_id="p", offset_s=0.0, phases=tuple(phases)),),
        provenance=PlanProvenance(source="sumo_tls"),
    )


@requires_demo_assets
@requires_sumolib
@pytest.mark.parametrize("right_turn", ["s", "o", "O"])
def test_permissive_or_uncontrolled_links_count_as_served(right_turn):
    # J2's right turns (links 0, 4, 7, 11) show `right_turn` in every phase: a green arrow
    # that requires stopping ("s") or an uncontrolled link ("o"/"O") still moves traffic.
    r = right_turn
    plan = _j2_program(
        f"{r}GGg{r}rr{r}GGg{r}rr",
        f"{r}yyy{r}rr{r}yyy{r}rr",
        f"{r}rrr{r}Gg{r}rrr{r}Gg",
        f"{r}rrr{r}yy{r}rrr{r}yy",
    )
    assert co._unserved_movements(plan, DEMO_DIR / "demo.net.xml", None) == {}


@requires_demo_assets
@requires_sumolib
def test_a_movement_shown_only_red_or_yellow_is_unserved():
    # The right turns never move: red ("r"/"u") or yellow ("y"/"Y") in every phase.
    plan = _j2_program(
        "rGGgurrYGGgrrr",
        "yyyyrrryyyyurr",
        "rrrruGgrrrrrGg",
        "urrryyyrrrrYyy",
    )
    assert co._unserved_movements(plan, DEMO_DIR / "demo.net.xml", None) == {
        "J2": ["EB-right", "WB-right", "NB-right", "SB-right"]
    }
