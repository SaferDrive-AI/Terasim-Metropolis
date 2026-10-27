"""Unit tests for BayesianOffsetOptimizer.

No SUMO involved: the Bayesian optimizer is exercised against an analytic
objective of the sampled offsets (``|offset_J2 - 33| + 1``, optimum at 33 on a
1-second grid), which also locks determinism for a fixed ``rng_seed``.
"""

from __future__ import annotations

import math

import pytest

from terasim_city.optimize.base import Budget, PlanEvaluation
from terasim_city.optimize.bayes_offsets import BayesianOffsetOptimizer
from terasim_city.plans.model import (
    CorridorPlan,
    PhaseSpec,
    PlanProvenance,
    SignalPlan,
)

CYCLE_S = 90.0


def _signal_plan(tls_id: str, offset_s: float = 0.0) -> SignalPlan:
    return SignalPlan(
        tls_id=tls_id,
        program_id="p0",
        offset_s=offset_s,
        phases=(
            PhaseSpec(state="GGrr", duration_s=45.0, kind="green"),
            PhaseSpec(state="rrGG", duration_s=45.0, kind="green"),
        ),
    )


def _corridor(plan_id: str, offsets: dict[str, float]) -> CorridorPlan:
    return CorridorPlan(
        plan_id=plan_id,
        plans=tuple(_signal_plan(t, o) for t, o in offsets.items()),
        provenance=PlanProvenance(source="explicit"),
    )


def _evaluation(plan: CorridorPlan, score: float, *, valid: bool = True) -> PlanEvaluation:
    return PlanEvaluation(plan=plan, moe={}, score=score, valid=valid, trial_dir="")


# ---------------------------------------------------------------------------
# BayesianOffsetOptimizer — analytic objective, no SUMO
# ---------------------------------------------------------------------------

TARGET_OFFSET = 33.0


def _seed_plan() -> CorridorPlan:
    return _corridor("seed_default", {"J1": 0.0, "J2": 10.0})


def _objective(plan: CorridorPlan) -> float:
    return abs(plan.by_tls()["J2"].offset_s - TARGET_OFFSET) + 1.0


def _run_fake_objective(rounds: int, *, rng_seed: int = 42) -> tuple[list[float], BayesianOffsetOptimizer]:
    opt = BayesianOffsetOptimizer(
        _seed_plan(),
        budget=Budget(max_trials=rounds, patience=0),
        rng_seed=rng_seed,
        step_s=1.0,
    )
    asked: list[float] = []
    while not opt.is_finished():
        plans = opt.ask(1)
        if not plans:
            break
        for plan in plans:
            asked.append(plan.by_tls()["J2"].offset_s)
            opt.tell([_evaluation(plan, _objective(plan))])
    return asked, opt


def test_bayes_finds_optimum_within_step_tolerance():
    asked, opt = _run_fake_objective(15)
    assert len(asked) == 15
    assert opt.is_finished()
    best = opt.best()
    assert best is not None
    best_offset = best.plan.by_tls()["J2"].offset_s
    assert abs(best_offset - TARGET_OFFSET) <= 1.0
    assert best.score == pytest.approx(_objective(best.plan))


def test_bayes_deterministic_for_same_seed():
    asked_a, opt_a = _run_fake_objective(15, rng_seed=42)
    asked_b, opt_b = _run_fake_objective(15, rng_seed=42)
    assert asked_a == asked_b
    assert opt_a.best().plan.plan_id == opt_b.best().plan.plan_id


def test_bayes_offsets_on_grid_and_first_tls_pinned():
    asked, opt = _run_fake_objective(8)
    seed = _seed_plan()
    for offset in asked:
        assert 0.0 <= offset <= CYCLE_S - 1.0
        assert offset == pytest.approx(round(offset))  # 1 s grid
    plans = BayesianOffsetOptimizer(
        seed, budget=Budget(max_trials=3, patience=0), rng_seed=1
    ).ask(3)
    for plan in plans:
        assert plan.by_tls()["J1"].offset_s == seed.by_tls()["J1"].offset_s
        assert plan.plan_id.startswith("bo_")
        assert plan.provenance.source == "bo_iteration"
        assert plan.provenance.parent_plan_id == "seed_default"
        assert plan.provenance.iteration is not None
        assert dict(plan.provenance.params).keys() == {"offset_J2"}


def test_bayes_ask_capped_by_budget():
    opt = BayesianOffsetOptimizer(
        _seed_plan(), budget=Budget(max_trials=2, patience=0), rng_seed=42
    )
    plans = opt.ask(5)
    assert len(plans) == 2
    assert opt.ask(1) == []  # budget fully pending
    opt.tell([_evaluation(p, _objective(p)) for p in plans])
    assert opt.is_finished()


def test_bayes_patience_stops_early():
    opt = BayesianOffsetOptimizer(
        _seed_plan(), budget=Budget(max_trials=100, patience=3), rng_seed=42
    )
    # First tell improves (best: None -> set); then 3 non-improving tells.
    (plan,) = opt.ask(1)
    opt.tell([_evaluation(plan, 1.0)])
    assert not opt.is_finished()
    for i in range(3):
        (plan,) = opt.ask(1)
        opt.tell([_evaluation(plan, 10.0 + i)])
    assert opt.is_finished()
    assert opt.best().score == 1.0


def test_bayes_single_tls_seed_rejected():
    single = _corridor("solo", {"J1": 0.0})
    with pytest.raises(ValueError, match=">= 2 TLS"):
        BayesianOffsetOptimizer(single, budget=Budget())


def test_bayes_uncommon_cycle_rejected():
    long_j2 = SignalPlan(
        tls_id="J2",
        program_id="p0",
        offset_s=0.0,
        phases=(PhaseSpec(state="GGrr", duration_s=100.0, kind="green"),),
    )
    plan = CorridorPlan(
        plan_id="mixed",
        plans=(_signal_plan("J1"), long_j2),
        provenance=PlanProvenance(source="explicit"),
    )
    with pytest.raises(ValueError, match="common cycle"):
        BayesianOffsetOptimizer(plan, budget=Budget())


def test_bayes_warm_start_accepted_and_sets_best():
    seed = _seed_plan()
    opt = BayesianOffsetOptimizer(
        seed, budget=Budget(max_trials=5, patience=0), rng_seed=42
    )
    # Off-grid seed offset (10.0 is on-grid; use a shifted variant off-grid).
    from dataclasses import replace

    off_grid = CorridorPlan(
        plan_id="seed_offgrid",
        plans=(seed.plans[0], replace(seed.plans[1], offset_s=10.4)),
        provenance=PlanProvenance(source="explicit"),
    )
    opt.warm_start([_evaluation(off_grid, 2.5)])
    best = opt.best()
    assert best is not None
    assert best.plan.plan_id == "seed_offgrid"
    # Warm-start consumes no budget: all 5 trials still available.
    plans = opt.ask(5)
    assert len(plans) == 5


def test_bayes_tell_unknown_plan_rejected():
    opt = BayesianOffsetOptimizer(
        _seed_plan(), budget=Budget(max_trials=5, patience=0), rng_seed=42
    )
    with pytest.raises(ValueError, match="not a pending"):
        opt.tell([_evaluation(_corridor("ghost", {"J1": 0.0, "J2": 1.0}), 1.0)])


def test_bayes_inf_score_kept_in_evaluation():
    opt = BayesianOffsetOptimizer(
        _seed_plan(), budget=Budget(max_trials=2, patience=0), rng_seed=42
    )
    (plan,) = opt.ask(1)
    evaluation = _evaluation(plan, math.inf, valid=False)
    opt.tell([evaluation])  # optuna receives the finite penalty internally
    assert math.isinf(evaluation.score)
    assert opt.best() is None
