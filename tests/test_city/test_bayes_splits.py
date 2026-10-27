"""Unit tests for BayesianSplitOptimizer.

Mostly SUMO-free: the mapping math and error cases run on hand-built plans.
The convergence test seeds from the demo baseline sheet's J1 plan (loaded via
``load_timing_sheet``, which needs sumolib + the demo net) and drives the
optimizer against an analytic objective rewarding an EW green near a target
value — also locking determinism for a fixed ``rng_seed``.
"""

from __future__ import annotations

import itertools
import math
import warnings

import pytest

from terasim_city.optimize.base import Budget, PlanEvaluation
from terasim_city.optimize.bayes_splits import (
    CYCLE_STEP_S,
    BayesianSplitOptimizer,
    cycle_grid_bounds,
    factors_from_greens,
    greens_from_factors,
)
from terasim_city.plans.model import (
    CorridorPlan,
    PhaseSpec,
    PlanProvenance,
    SignalPlan,
)
from tests.fixtures.tmc_templates import baseline_sheet, write_tmc
from tests.test_city.conftest import REPO_ROOT, requires_sumolib

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_NET = DEMO_DIR / "demo.net.xml"

requires_demo_assets = pytest.mark.skipif(
    not DEMO_NET.exists(),
    reason=f"corridor demo assets not present at {DEMO_DIR} "
    "(run examples/corridor_demo/build_demo_map.py)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STAGE_STATES = (("GGrr", "yyrr"), ("rrGG", "rryy"))


def _single_tls_plan(
    greens: tuple[float, ...] = (48.0, 34.0),
    *,
    tls_id: str = "J1",
    offset_s: float = 0.0,
    plan_id: str = "seed",
    yellow_s: float = 3.0,
    all_red_s: float = 1.0,
) -> CorridorPlan:
    """One-TLS plan: per green stage a green + yellow(3s) + all_red(1s) by default."""
    phases: list[PhaseSpec] = []
    for index, green in enumerate(greens):
        green_state, yellow_state = _STAGE_STATES[index % 2]
        phases.append(PhaseSpec(state=green_state, duration_s=float(green), kind="green"))
        phases.append(PhaseSpec(state=yellow_state, duration_s=yellow_s, kind="yellow"))
        phases.append(PhaseSpec(state="rrrr", duration_s=all_red_s, kind="all_red"))
    return CorridorPlan(
        plan_id=plan_id,
        plans=(
            SignalPlan(
                tls_id=tls_id,
                program_id="p0",
                offset_s=offset_s,
                phases=tuple(phases),
            ),
        ),
        provenance=PlanProvenance(source="explicit"),
    )


def _greens(plan: CorridorPlan) -> list[float]:
    return [p.duration_s for p in plan.plans[0].phases if p.kind == "green"]


def _evaluation(plan: CorridorPlan, score: float, *, valid: bool = True) -> PlanEvaluation:
    return PlanEvaluation(plan=plan, moe={}, score=score, valid=valid, trial_dir="")


# ---------------------------------------------------------------------------
# Mapping math
# ---------------------------------------------------------------------------

def test_asked_plans_preserve_cycle_exactly_and_structure():
    seed = _single_tls_plan()
    cycle = seed.plans[0].cycle_s
    opt = BayesianSplitOptimizer(
        seed, budget=Budget(max_trials=10, patience=0), rng_seed=7
    )
    plans = opt.ask(10)
    assert len(plans) == 10
    for plan in plans:
        signal_plan = plan.plans[0]
        # Cycle exactly preserved (incl. the rounding remainder).
        assert signal_plan.cycle_s == pytest.approx(cycle, abs=1e-9)
        greens = _greens(plan)
        assert all(g >= 5.0 - 1e-9 for g in greens)
        # Every stage except the remainder-absorbing largest is on a 0.1 grid.
        largest = greens.index(max(greens))
        for index, green in enumerate(greens):
            if index != largest:
                assert abs(green * 10.0 - round(green * 10.0)) < 1e-6
        # Only green durations move: states, kinds, order, offset unchanged.
        assert signal_plan.offset_s == seed.plans[0].offset_s
        assert [p.state for p in signal_plan.phases] == [
            p.state for p in seed.plans[0].phases
        ]
        assert [p.kind for p in signal_plan.phases] == [
            p.kind for p in seed.plans[0].phases
        ]
        assert plan.plan_id.startswith("bo_split_")
        assert plan.provenance.source == "bo_iteration"
        assert plan.provenance.parent_plan_id == "seed"
        assert plan.provenance.iteration is not None
        params = dict(plan.provenance.params)
        assert set(params) == {"green_0", "green_1"}
        assert params["green_0"] == pytest.approx(greens[0])
        assert params["green_1"] == pytest.approx(greens[1])


def test_mapping_math_direct_and_extreme_factors():
    # The seed of _single_tls_plan(): cycle 90, fixed 8, total green 82, min green 5,
    # headroom 72.
    total_green, min_green = 82.0, 5.0

    # Equal factors -> equal split of the headroom.
    greens = greens_from_factors([0.5, 0.5], total_green, min_green)
    assert greens == [pytest.approx(41.0), pytest.approx(41.0)]

    # Extreme asymmetry: the small stage still gets min_green + its share.
    greens = greens_from_factors([0.05, 1.0], total_green, min_green)
    assert greens[0] >= 5.0
    assert greens[0] == pytest.approx(5.0 + 72.0 * 0.05 / 1.05, abs=0.1)
    assert sum(greens) == pytest.approx(82.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Construction errors
# ---------------------------------------------------------------------------

def test_multi_tls_seed_rejected_pointing_at_offsets():
    multi = CorridorPlan(
        plan_id="corridor",
        plans=(
            _single_tls_plan(tls_id="J1").plans[0],
            _single_tls_plan(tls_id="J2").plans[0],
        ),
        provenance=PlanProvenance(source="explicit"),
    )
    with pytest.raises(ValueError, match="offsets"):
        BayesianSplitOptimizer(multi, budget=Budget())


def test_single_green_stage_rejected():
    with pytest.raises(ValueError, match="green stages"):
        BayesianSplitOptimizer(_single_tls_plan((60.0,)), budget=Budget())


def test_insufficient_green_headroom_rejected():
    # Cycle 16 with 8s fixed -> 8s of green < 2 * min_green_s (5s).
    with pytest.raises(ValueError, match="min_green"):
        BayesianSplitOptimizer(_single_tls_plan((4.0, 4.0)), budget=Budget())


def test_zero_headroom_is_allowed_and_pins_greens_to_min():
    # total_green == n * min_green exactly: legal, every stage gets min_green.
    seed = _single_tls_plan((6.0, 4.0))  # total green 10 = 2 * 5
    opt = BayesianSplitOptimizer(seed, budget=Budget(max_trials=2, patience=0))
    (plan,) = opt.ask(1)
    assert _greens(plan) == [pytest.approx(5.0), pytest.approx(5.0)]
    assert plan.plans[0].cycle_s == pytest.approx(seed.plans[0].cycle_s, abs=1e-9)


# ---------------------------------------------------------------------------
# Ask/tell mechanics (mirroring the offset optimizer)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Warm start
# ---------------------------------------------------------------------------


def test_warm_start_all_min_green_seed_accepted():
    # Zero-headroom plan: factors floor at 0.05 rather than dividing by zero.
    seed = _single_tls_plan((6.0, 4.0))
    opt = BayesianSplitOptimizer(seed, budget=Budget(max_trials=3, patience=0))
    opt.warm_start([_evaluation(seed, 1.5)])
    assert opt.best().score == 1.5


def test_warm_start_mismatched_plan_rejected():
    opt = BayesianSplitOptimizer(
        _single_tls_plan(tls_id="J1"), budget=Budget(max_trials=3, patience=0)
    )
    stranger = _single_tls_plan(tls_id="J9", plan_id="stranger")
    with pytest.raises(ValueError, match="lacks TLS"):
        opt.warm_start([_evaluation(stranger, 1.0)])
    three_stage = _single_tls_plan((30.0, 30.0, 18.0), plan_id="threes")
    with pytest.raises(ValueError, match="green stage"):
        opt.warm_start([_evaluation(three_stage, 1.0)])


# ---------------------------------------------------------------------------
# Convergence on the demo baseline-sheet seed (analytic objective)
# ---------------------------------------------------------------------------

TARGET_EW_GREEN = 55.0


def _demo_j1_seed(sheet_path) -> CorridorPlan:
    from terasim_city.plans.baseline_sheet import load_timing_sheet

    corridor_plan = load_timing_sheet(sheet_path, net_file=DEMO_NET)
    return CorridorPlan(
        plan_id="baseline_J1",
        plans=(corridor_plan.by_tls()["J1"],),
        provenance=PlanProvenance(source="baseline_sheet"),
    )


def _objective(plan: CorridorPlan) -> float:
    # EW is the sheet's first green stage; reward greens near the target.
    return abs(_greens(plan)[0] - TARGET_EW_GREEN) + 1.0


def _run_fake_objective(
    seed: CorridorPlan, rounds: int, *, rng_seed: int = 42
) -> tuple[list[list[float]], BayesianSplitOptimizer]:
    opt = BayesianSplitOptimizer(
        seed, budget=Budget(max_trials=rounds, patience=0), rng_seed=rng_seed
    )
    opt.warm_start([_evaluation(seed, _objective(seed))])
    asked: list[list[float]] = []
    while not opt.is_finished():
        plans = opt.ask(1)
        if not plans:
            break
        for plan in plans:
            asked.append(_greens(plan))
            opt.tell([_evaluation(plan, _objective(plan))])
    return asked, opt


@requires_demo_assets
@requires_sumolib
def test_split_search_finds_target_ew_green_on_demo_seed(tmp_path):
    seed = _demo_j1_seed(write_tmc(tmp_path, baseline_sheet()))
    assert _greens(seed) == [pytest.approx(48.0), pytest.approx(34.0)]
    asked, opt = _run_fake_objective(seed, 15)
    assert len(asked) == 15
    assert opt.is_finished()
    best = opt.best()
    assert best is not None
    best_ew = _greens(best.plan)[0]
    assert abs(best_ew - TARGET_EW_GREEN) <= 2.0
    assert best.score == pytest.approx(_objective(best.plan))
    # Every trial keeps the seed's total green time (hence its 90s cycle).
    for greens in asked:
        assert sum(greens) == pytest.approx(sum(_greens(seed)), abs=1e-9)


@requires_demo_assets
@requires_sumolib
def test_split_search_deterministic_for_same_seed(tmp_path):
    seed = _demo_j1_seed(write_tmc(tmp_path, baseline_sheet()))
    asked_a, opt_a = _run_fake_objective(seed, 15, rng_seed=42)
    asked_b, opt_b = _run_fake_objective(seed, 15, rng_seed=42)
    assert asked_a == asked_b
    assert opt_a.best().plan.plan_id == opt_b.best().plan.plan_id


# ---------------------------------------------------------------------------
# Cycle search (optional ``cycle_bounds`` dimension)
# ---------------------------------------------------------------------------

def test_cycle_bounds_add_a_cycle_dimension_and_keep_fixed_time():
    seed = _single_tls_plan()
    fixed = sum(p.duration_s for p in seed.plans[0].phases if p.kind != "green")
    opt = BayesianSplitOptimizer(
        seed, budget=Budget(max_trials=12, patience=0), rng_seed=1, cycle_bounds=(60.0, 120.0)
    )
    plans = opt.ask(12)
    assert len(plans) == 12
    for plan in plans:
        sp = plan.plans[0]
        assert 60.0 - 1e-9 <= sp.cycle_s <= 120.0 + 1e-9
        green_total = sum(p.duration_s for p in sp.phases if p.kind == "green")
        assert math.isclose(sp.cycle_s - fixed, green_total, abs_tol=1e-6)
        assert dict(plan.provenance.params)["cycle_s"] == pytest.approx(sp.cycle_s)
    assert len({round(p.plans[0].cycle_s) for p in plans}) > 1


def test_cycle_bounds_floor_is_fixed_time_plus_min_greens():
    seed = _single_tls_plan()
    fixed = sum(p.duration_s for p in seed.plans[0].phases if p.kind != "green")
    with pytest.raises(ValueError, match="cycle_bounds"):
        BayesianSplitOptimizer(seed, budget=Budget(max_trials=1), cycle_bounds=(fixed, fixed + 1.0))


def test_warm_start_with_cycle_bounds_snaps_seed_cycle():
    seed = _single_tls_plan()
    opt = BayesianSplitOptimizer(seed, budget=Budget(max_trials=2), cycle_bounds=(60.0, 120.0))
    opt.warm_start([PlanEvaluation(plan=seed, moe={}, score=1.0, valid=True, trial_dir="x")])
    assert opt.best() is not None and opt.best().plan is seed


def test_no_cycle_bounds_is_unchanged():
    seed = _single_tls_plan()
    opt = BayesianSplitOptimizer(seed, budget=Budget(max_trials=3), rng_seed=7)
    for plan in opt.ask(3):
        assert plan.plans[0].cycle_s == pytest.approx(seed.plans[0].cycle_s)
        assert "cycle_s" not in dict(plan.provenance.params)


def test_split_math_helpers_round_trip():
    greens = greens_from_factors([0.5, 1.0, 0.25], total_green=60.0, min_green=5.0)
    assert sum(greens) == pytest.approx(60.0)
    assert factors_from_greens(greens, 5.0)[1] == pytest.approx(1.0)
    assert CYCLE_STEP_S == 1.0


# Behaviour the tests above cannot detect: the seed in
# test_warm_start_with_cycle_bounds_snaps_seed_cycle has a 90 s cycle, already on
# the 1 s grid and inside the bounds, so that test passes even without the snap.

def test_warm_start_snaps_off_grid_and_out_of_range_cycles_into_the_domain():
    opt = BayesianSplitOptimizer(
        _single_tls_plan(), budget=Budget(max_trials=2), cycle_bounds=(60.0, 120.0)
    )
    for greens, snapped in (
        ((48.4, 34.0), 90.0),  # 90.4 s is off the 1 s grid -> nearest grid point
        ((70.0, 52.0), 120.0),  # 130 s is above the range -> upper bound
        ((20.0, 12.0), 60.0),  # 40 s is below the range -> lower bound
    ):
        plan = _single_tls_plan(greens, plan_id="warm")
        assert opt._warm_start_params(plan)["cycle_s"] == pytest.approx(snapped)
        opt.warm_start([_evaluation(plan, 1.0)])  # optuna rejects off-domain values
    assert opt.best() is not None


def test_cycle_bounds_below_the_floor_are_raised_to_it():
    # Floor = 8 s fixed time + 2 x 5 s min green = 18 s; the requested low of
    # 10 s would leave negative green headroom if it were used as-is.
    seed = _single_tls_plan()
    opt = BayesianSplitOptimizer(
        seed, budget=Budget(max_trials=10, patience=0), rng_seed=3, cycle_bounds=(10.0, 30.0)
    )
    plans = opt.ask(10)
    assert len(plans) == 10
    for plan in plans:
        assert 18.0 - 1e-9 <= plan.plans[0].cycle_s <= 30.0 + 1e-9
        assert all(g >= 5.0 - 1e-9 for g in _greens(plan))


def test_cycle_search_keeps_a_large_seed_offset_inside_the_sampled_cycle():
    # An 80 s offset is valid at the seed's 90 s cycle, but SignalPlan rejects an
    # offset >= its cycle, so sampled cycles below 80 s must wrap it into range.
    seed = _single_tls_plan(offset_s=80.0)
    opt = BayesianSplitOptimizer(
        seed, budget=Budget(max_trials=12, patience=0), rng_seed=1, cycle_bounds=(60.0, 120.0)
    )
    plans = opt.ask(12)
    assert any(p.plans[0].cycle_s < 80.0 for p in plans)  # the scenario really occurs
    for plan in plans:
        sp = plan.plans[0]
        assert 0.0 <= sp.offset_s < sp.cycle_s
        if sp.cycle_s > 80.0:
            assert sp.offset_s == 80.0  # unchanged whenever it still fits


def test_cycle_search_ask_tell_loop_runs_and_tracks_best():
    seed = _single_tls_plan()
    opt = BayesianSplitOptimizer(
        seed, budget=Budget(max_trials=14, patience=0), rng_seed=5, cycle_bounds=(60.0, 120.0)
    )
    opt.warm_start([_evaluation(seed, 50.0)])
    while not opt.is_finished():
        plans = opt.ask(2)
        assert plans
        # Lower is better: reward cycles close to 100 s.
        opt.tell([_evaluation(p, abs(p.plans[0].cycle_s - 100.0)) for p in plans])
    best = opt.best()
    assert best is not None and best.score < 50.0
    assert best.score == pytest.approx(abs(best.plan.plans[0].cycle_s - 100.0))


def test_split_math_helpers_reject_impossible_inputs():
    with pytest.raises(ValueError, match="at least one"):
        greens_from_factors([], total_green=10.0, min_green=5.0)
    with pytest.raises(ValueError, match="min_green"):
        greens_from_factors([0.5, 0.5], total_green=9.0, min_green=5.0)
    assert factors_from_greens([], 5.0) == []


# ---------------------------------------------------------------------------
# Cycle grid: bounds are snapped onto the step grid before optuna sees them
# ---------------------------------------------------------------------------

def test_messy_float_floor_is_snapped_onto_the_grid_and_matches_the_optuna_domain():
    # Four stages of 30 s green + 3.3 s yellow + 1.8 s all-red with min_green_s=7 make
    # the cycle floor 48.400000000000006 s. Unsnapped, the domain would be [48.4, 149.4]:
    # optuna warns and quietly lowers its top to 148.4 while the optimizer would still
    # snap warm starts to 149.4. Snapped onto the grid it is [49, 150] and nothing differs.
    seed = _single_tls_plan((30.0,) * 4, yellow_s=3.3, all_red_s=1.8)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # optuna's "not divisible by step" is a UserWarning
        opt = BayesianSplitOptimizer(
            seed,
            budget=Budget(max_trials=4, patience=0),
            rng_seed=1,
            min_green_s=7.0,
            cycle_bounds=(45.0, 150.0),
        )
    assert opt._cycle_low == 49.0 and opt._cycle_high == 150.0
    domain = opt._distributions["cycle_s"]
    assert (opt._cycle_low, opt._cycle_high) == (domain.low, domain.high)
    # Warm starts at the seed's own cycle, just above the range and far above it.
    for greens, cycle, snapped in (
        ((30.0,) * 4, 140.4, 140.0),
        ((32.6,) * 4, 150.8, 150.0),
        ((40.0,) * 4, 180.4, 150.0),
    ):
        plan = _single_tls_plan(greens, yellow_s=3.3, all_red_s=1.8, plan_id="warm")
        assert plan.plans[0].cycle_s == pytest.approx(cycle)
        assert opt._warm_start_params(plan)["cycle_s"] == snapped
        opt.warm_start([_evaluation(plan, 1.0)])
    plans = opt.ask(3)
    assert len(plans) == 3
    for plan in plans:
        assert 49.0 - 1e-9 <= plan.plans[0].cycle_s <= 150.0 + 1e-9


def test_cycle_bounds_off_the_grid_snap_low_up_and_high_down():
    # Default seed: the floor is 18 s, so the requested bounds decide.
    opt = BayesianSplitOptimizer(
        _single_tls_plan(), budget=Budget(max_trials=2), cycle_bounds=(60.1, 120.1)
    )
    assert (opt._cycle_low, opt._cycle_high) == (61.0, 120.0)
    domain = opt._distributions["cycle_s"]
    assert (domain.low, domain.high) == (61.0, 120.0)


@pytest.mark.parametrize(
    "bounds",
    [(float("inf"), 150.0), (45.0, float("nan"))],
    ids=["inf-low", "nan-high"],
)
def test_non_finite_cycle_bounds_are_rejected(bounds):
    with pytest.raises(ValueError, match=r"cycle_bounds.*(inf|nan)"):
        BayesianSplitOptimizer(
            _single_tls_plan(), budget=Budget(max_trials=1), cycle_bounds=bounds
        )


def test_cycle_domain_matches_stored_bounds_across_messy_float_programs():
    # Realistic four-stage programs whose yellow / all-red / min-green sums are not
    # exact in binary floating point (the cycle floor is e.g. 48.400000000000006 s).
    # The domain optuna ends up with must be the one we hold, with no optuna warning,
    # and warm starts from far outside the range must be accepted.
    yellows = (3.0, 3.1, 3.3, 3.5, 3.6, 3.7, 3.9, 4.0, 4.2, 4.5)
    all_reds = (1.0, 1.2, 1.5, 1.8, 2.0, 2.2, 2.5)
    min_greens = (5.0, 6.0, 7.0, 8.0)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for yellow, all_red, min_green in itertools.product(yellows, all_reds, min_greens):
            where = f"yellow={yellow} all_red={all_red} min_green={min_green}"
            seed = _single_tls_plan((30.0,) * 4, yellow_s=yellow, all_red_s=all_red)
            opt = BayesianSplitOptimizer(
                seed, budget=Budget(max_trials=2), min_green_s=min_green, cycle_bounds=(45.0, 150.0)
            )
            domain = opt._distributions["cycle_s"]
            assert (opt._cycle_low, opt._cycle_high) == (domain.low, domain.high), where
            for greens in ((60.0,) * 4, (8.0,) * 4):  # far above / around the bottom of the range
                plan = _single_tls_plan(greens, yellow_s=yellow, all_red_s=all_red, plan_id="warm")
                opt.warm_start([_evaluation(plan, 1.0)])


def test_cycle_grid_bounds_snap_low_up_and_high_down_with_float_noise_tolerance():
    assert cycle_grid_bounds((60.1, 120.1), 18.0) == (61.0, 120.0)
    # The floor wins over a lower requested bound and is itself snapped up.
    assert cycle_grid_bounds((45.0, 150.0), 48.400000000000006) == (49.0, 150.0)
    # Float noise around a grid point must not move it by a whole step: bounds a hair
    # off 60 and 120 still snap to 60 and 120 (the previous arithmetic turned the
    # (60.1, 120.1) case above into a top of 119.1 because 120.1 - 60.1 is 59.99999999999999).
    assert cycle_grid_bounds((60.0 + 1e-12, 120.0 - 1e-12), 18.0) == (60.0, 120.0)
    # Nothing left above the floor.
    with pytest.raises(ValueError, match="no cycle range"):
        cycle_grid_bounds((45.0, 50.0), 50.0)


@pytest.mark.parametrize("bounds", [(float("-inf"), 150.0), (45.0, float("inf"))])
def test_cycle_grid_bounds_rejects_non_finite_bounds(bounds):
    with pytest.raises(ValueError, match=r"cycle_bounds.*inf"):
        cycle_grid_bounds(bounds, 18.0)


def test_stored_cycle_bounds_follow_the_optuna_domain_when_the_grid_is_inexact(monkeypatch):
    # A 0.1 s step is not exactly representable, so even snapped bounds can be
    # normalised by optuna. The optimizer must keep following optuna's domain, or a
    # warm start snapped to a stale top bound would be rejected as outside it.
    monkeypatch.setattr("terasim_city.optimize.bayes_splits.CYCLE_STEP_S", 0.1)
    seed = _single_tls_plan((30.0,) * 4, yellow_s=3.3, all_red_s=1.8)
    floor = sum(p.duration_s for p in seed.plans[0].phases if p.kind != "green") + 4 * 7.0
    computed = cycle_grid_bounds((45.0, 150.0), floor)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # optuna warns while it normalises the domain
        opt = BayesianSplitOptimizer(
            seed, budget=Budget(max_trials=2), min_green_s=7.0, cycle_bounds=(45.0, 150.0)
        )
    domain = opt._distributions["cycle_s"]
    assert computed != (domain.low, domain.high)  # premise: optuna really changed the domain
    assert (opt._cycle_low, opt._cycle_high) == (domain.low, domain.high)
    plan = _single_tls_plan((60.0,) * 4, yellow_s=3.3, all_red_s=1.8, plan_id="warm")
    assert opt._warm_start_params(plan)["cycle_s"] == domain.high
    opt.warm_start([_evaluation(plan, 1.0)])
