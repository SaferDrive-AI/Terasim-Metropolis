"""Unit tests for BayesianCorridorOptimizer (SUMO-free, hand-built plans)."""

from __future__ import annotations

import math
import warnings
from dataclasses import replace

import optuna
import pytest

from terasim_city.optimize.base import Budget, PlanEvaluation
from terasim_city.optimize.bayes_corridor import BayesianCorridorOptimizer
from terasim_city.optimize.bayes_splits import (
    cycle_grid_bounds,
    factors_from_greens,
    greens_from_factors,
)
from terasim_city.plans.model import CorridorPlan, PhaseSpec, PlanProvenance, SignalPlan

_STATES4 = ("GGrr", "rrGG", "GrGr", "rGrG")


def _tls(
    tls_id: str,
    greens: tuple[float, ...],
    offset: float = 0.0,
    states=("GGrr", "rrGG"),
    *,
    yellow_s: float = 3.0,
    all_red_s: float = 1.0,
) -> SignalPlan:
    phases = []
    for i, (green, state) in enumerate(zip(greens, states)):
        phases.append(PhaseSpec(state=state, duration_s=green, kind="green", name=f"g{i}"))
        phases.append(
            PhaseSpec(
                state=state.replace("G", "y"), duration_s=yellow_s, kind="yellow", name=f"y{i}"
            )
        )
        phases.append(
            PhaseSpec(state="r" * len(state), duration_s=all_red_s, kind="all_red", name=f"r{i}")
        )
    return SignalPlan(tls_id=tls_id, program_id="seed", offset_s=offset, phases=tuple(phases))


def _corridor(cycles=(90.0, 90.0, 90.0)) -> CorridorPlan:
    plans = []
    for tls_id, cycle in zip(("J1", "J2", "J3"), cycles):
        green_total = cycle - 8.0
        plans.append(_tls(tls_id, (green_total * 0.6, green_total * 0.4)))
    return CorridorPlan(
        plan_id="seed", plans=tuple(plans), provenance=PlanProvenance(source="explicit")
    )


def _four_stage_corridor(green_s: float) -> CorridorPlan:
    """Three TLS with four green stages each and float-awkward yellow / all-red times."""
    plans = tuple(
        _tls(tls_id, (green_s,) * 4, 0.0, _STATES4, yellow_s=3.3, all_red_s=1.8)
        for tls_id in ("J1", "J2", "J3")
    )
    return CorridorPlan(plan_id="seed", plans=plans, provenance=PlanProvenance(source="explicit"))


def _with_offsets(plan: CorridorPlan, offsets: dict[str, float]) -> CorridorPlan:
    shifted = tuple(
        replace(sp, offset_s=offsets.get(sp.tls_id, sp.offset_s)) for sp in plan.plans
    )
    return replace(plan, plans=shifted)


def _evaluation(plan: CorridorPlan, score: float) -> PlanEvaluation:
    return PlanEvaluation(plan=plan, moe={}, score=score, valid=True, trial_dir="x")


def _decode(opt: BayesianCorridorOptimizer, seed: CorridorPlan, **params: float) -> CorridorPlan:
    """Decode the seed's own warm-start parameters with some of them overridden."""
    merged = {**opt._warm_start_params(seed), **params}
    trial = optuna.trial.create_trial(
        params=merged, distributions=dict(opt._distributions), value=0.0
    )
    return opt._plan_from_trial(trial)


def test_requires_two_tls():
    single = CorridorPlan(
        plan_id="s", plans=(_tls("J1", (40.0, 30.0)),), provenance=PlanProvenance(source="explicit")
    )
    with pytest.raises(ValueError, match=">= 2 TLS"):
        BayesianCorridorOptimizer(single, budget=Budget(max_trials=1))


def test_fixed_cycle_mode_keeps_common_cycle_and_varies_splits_and_offsets():
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=10, patience=0), rng_seed=3)
    plans = opt.ask(10)
    assert len(plans) == 10
    for plan in plans:
        assert plan.common_cycle_s() == pytest.approx(90.0)
        assert plan.plans[0].offset_s == 0.0
        for sp in plan.plans[1:]:
            assert 0.0 <= sp.offset_s < 90.0 and sp.offset_s == math.floor(sp.offset_s)
    assert len({tuple(p.duration_s for p in plan.plans[0].phases) for plan in plans}) > 1


def test_cycle_bounds_sample_a_common_cycle():
    seed = _corridor()
    opt = BayesianCorridorOptimizer(
        seed, budget=Budget(max_trials=12, patience=0), rng_seed=5, cycle_bounds=(60.0, 150.0)
    )
    plans = opt.ask(12)
    cycles = {round(plan.common_cycle_s()) for plan in plans}
    assert len(cycles) > 1 and all(60 <= c <= 150 for c in cycles)
    for plan in plans:
        cycle = plan.common_cycle_s()
        for sp in plan.plans:
            assert sp.cycle_s == pytest.approx(cycle)
            assert sp.offset_s < cycle
        assert dict(plan.provenance.params)["cycle_s"] == pytest.approx(cycle)


def test_mixed_cycle_seed_warm_starts_with_cycle_bounds():
    """A scenario's own programs may run different cycles per TLS."""
    seed = _corridor(cycles=(140.0, 60.0, 90.0))
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2), cycle_bounds=(45.0, 240.0))
    opt.warm_start([PlanEvaluation(plan=seed, moe={}, score=1.0, valid=True, trial_dir="x")])
    assert opt.best() is not None


def test_mixed_cycle_seed_without_bounds_raises():
    with pytest.raises(ValueError):
        BayesianCorridorOptimizer(
            _corridor(cycles=(140.0, 60.0, 90.0)), budget=Budget(max_trials=1)
        )


def test_tell_updates_best():
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=4, patience=0), rng_seed=1)
    plans = opt.ask(2)
    evaluations = [
        PlanEvaluation(plan=p, moe={}, score=0.5 + i, valid=True, trial_dir="t")
        for i, p in enumerate(plans)
    ]
    opt.tell(evaluations)
    assert opt.best().plan is plans[0]


# ---------------------------------------------------------------------------
# Search space
# ---------------------------------------------------------------------------

def test_fixed_cycle_mode_has_no_cycle_dimension():
    opt = BayesianCorridorOptimizer(_corridor(), budget=Budget(max_trials=2))
    assert "cycle_s" not in opt._distributions
    assert sorted(opt._distributions) == sorted(
        [f"f_{t}_{k}" for t in ("J1", "J2", "J3") for k in (0, 1)] + ["offset_J2", "offset_J3"]
    )
    # The first TLS has no offset dimension: it is the pinned reference.
    assert "offset_J1" not in opt._distributions
    assert dict(opt.ask(1)[0].provenance.params)["cycle_s"] == pytest.approx(90.0)


def test_cycle_bounds_add_exactly_one_cycle_dimension():
    opt = BayesianCorridorOptimizer(
        _corridor(), budget=Budget(max_trials=2), cycle_bounds=(60.0, 150.0)
    )
    assert len(opt._distributions) == 3 * 2 + 2 + 1
    domain = opt._distributions["cycle_s"]
    assert (domain.low, domain.high, domain.step) == (60.0, 150.0, 1.0)
    # Offsets span every grid point that fits the longest sampled cycle.
    assert opt._distributions["offset_J2"].high == 149.0


def test_first_tls_offset_is_pinned_at_zero_even_for_a_shifted_seed():
    seed = _with_offsets(_corridor(), {"J1": 25.0, "J2": 45.0, "J3": 70.0})
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=6, patience=0), rng_seed=4)
    for plan in opt.ask(6):
        assert plan.plans[0].offset_s == 0.0
        assert dict(plan.provenance.params)["offset_J1"] == 0.0


def test_decoded_plans_keep_phase_structure_and_fixed_time():
    seed = _corridor()
    opt = BayesianCorridorOptimizer(
        seed, budget=Budget(max_trials=6, patience=0), rng_seed=2, cycle_bounds=(60.0, 150.0)
    )
    for plan in opt.ask(6):
        assert plan.provenance.source == "bo_iteration"
        assert plan.provenance.parent_plan_id == "seed"
        params = dict(plan.provenance.params)
        for sp, seed_sp in zip(plan.plans, seed.plans):
            assert (sp.tls_id, sp.program_id) == (seed_sp.tls_id, seed_sp.program_id)
            assert [(p.state, p.kind, p.name) for p in sp.phases] == [
                (p.state, p.kind, p.name) for p in seed_sp.phases
            ]
            fixed = [p.duration_s for p in sp.phases if p.kind != "green"]
            assert fixed == [p.duration_s for p in seed_sp.phases if p.kind != "green"]
            greens = [p.duration_s for p in sp.phases if p.kind == "green"]
            assert all(g >= 5.0 for g in greens)
            assert sum(greens) == pytest.approx(params["cycle_s"] - sum(fixed))
            assert [params[f"green_{sp.tls_id}_{k}"] for k in range(2)] == greens
            assert params[f"offset_{sp.tls_id}"] == sp.offset_s


def test_min_green_s_bounds_every_decoded_green_even_for_extreme_factors():
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2), min_green_s=8.0)
    plan = _decode(opt, seed, f_J1_0=0.05, f_J1_1=1.0)  # the most lopsided split the factors allow
    greens = [p.duration_s for p in plan.plans[0].phases if p.kind == "green"]
    assert greens == greens_from_factors([0.05, 1.0], 82.0, 8.0)
    assert min(greens) > 11.0  # 8 s minimum + 66 s of headroom x 0.05 / 1.05


def test_same_rng_seed_gives_the_same_plans():
    def run() -> list[dict]:
        opt = BayesianCorridorOptimizer(
            _corridor(),
            budget=Budget(max_trials=8, patience=0),
            rng_seed=9,
            cycle_bounds=(60.0, 150.0),
        )
        return [plan.to_json_dict() for plan in opt.ask(8)]

    assert run() == run()


# ---------------------------------------------------------------------------
# Offsets wrap into the sampled cycle (every offset stays reachable)
# ---------------------------------------------------------------------------

def test_offsets_wrap_into_the_sampled_cycle_instead_of_clamping():
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2), cycle_bounds=(60.0, 150.0))
    # A 100 s offset in a 60 s cycle is 40 s into it (clamping would give 59), and an
    # offset equal to the cycle is the start of it (clamping would give 59 as well).
    plan = _decode(opt, seed, cycle_s=60.0, offset_J2=100.0, offset_J3=60.0)
    assert [sp.offset_s for sp in plan.plans] == [0.0, 40.0, 0.0]
    assert dict(plan.provenance.params)["offset_J2"] == 40.0
    # Offsets that already fit the cycle are untouched.
    plan = _decode(opt, seed, cycle_s=60.0, offset_J2=59.0, offset_J3=7.0)
    assert [sp.offset_s for sp in plan.plans] == [0.0, 59.0, 7.0]


@pytest.mark.parametrize("cycle", [60.0, 97.0, 150.0])
def test_every_offset_is_reachable_at_every_sampled_cycle(cycle):
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2), cycle_bounds=(60.0, 150.0))
    top = int(opt._distributions["offset_J2"].high)
    reached = set()
    for raw in range(top + 1):
        plan = _decode(opt, seed, cycle_s=cycle, offset_J2=float(raw))
        offset = plan.plans[1].offset_s
        assert offset == raw % cycle and 0.0 <= offset < plan.plans[1].cycle_s
        reached.add(offset)
    assert reached == {float(k) for k in range(int(cycle))}


# ---------------------------------------------------------------------------
# Domains: grid-snapped bounds, read back from optuna's own distributions
# ---------------------------------------------------------------------------

def test_messy_float_floor_is_snapped_and_the_stored_domains_match_optunas():
    # Four stages of 3.3 s yellow + 1.8 s all-red with min_green_s=7 put the cycle floor at
    # 48.400000000000006 s. Unsnapped, optuna warns and silently lowers the cycle domain's top
    # while the optimizer would still snap warm starts to the stale top.
    seed = _four_stage_corridor(30.0)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # optuna's "not divisible by step" is a UserWarning
        opt = BayesianCorridorOptimizer(
            seed, budget=Budget(max_trials=4, patience=0), rng_seed=1,
            min_green_s=7.0, cycle_bounds=(45.0, 150.0),
        )
    assert (opt._cycle_low, opt._cycle_high) == (49.0, 150.0)
    cycle_domain = opt._distributions["cycle_s"]
    assert (opt._cycle_low, opt._cycle_high) == (cycle_domain.low, cycle_domain.high)
    offset_domains = [opt._distributions[f"offset_{t}"] for t in ("J2", "J3")]
    assert offset_domains[0] is offset_domains[1]  # one domain shared by every offset
    assert opt._offset_high == offset_domains[0].high == 149.0
    # Warm starts at the seed's own cycle, just above the range and far above it, with offsets
    # near the top of the longer cycle: every one lands inside the domains optuna holds.
    cases = ((30.0, 140.4, 140.0), (32.6, 150.8, 150.0), (40.0, 180.4, 150.0))
    for green_s, cycle, snapped_cycle in cases:
        plan = _with_offsets(_four_stage_corridor(green_s), {"J2": cycle - 10.4, "J3": cycle - 0.7})
        assert plan.plans[0].cycle_s == pytest.approx(cycle)
        params = opt._warm_start_params(plan)
        assert params["cycle_s"] == snapped_cycle
        assert params["offset_J2"] == min(round(cycle - 10.4), 149.0)
        assert params["offset_J3"] == min(round(cycle - 0.7), 149.0)
        opt.warm_start([_evaluation(plan, 1.0)])
    plans = opt.ask(3)
    assert len(plans) == 3
    for plan in plans:
        assert 49.0 - 1e-9 <= plan.common_cycle_s() <= 150.0 + 1e-9


def test_stored_cycle_bounds_follow_the_optuna_domain_when_the_grid_is_inexact(monkeypatch):
    # A 0.1 s cycle step is not exactly representable, so even snapped bounds can be normalised
    # by optuna. The optimizer must keep following optuna's domain, or a warm start snapped to a
    # stale top bound would be rejected as outside it.
    monkeypatch.setattr("terasim_city.optimize.bayes_splits.CYCLE_STEP_S", 0.1)
    monkeypatch.setattr("terasim_city.optimize.bayes_corridor.CYCLE_STEP_S", 0.1)
    seed = _four_stage_corridor(30.0)
    floor = sum(p.duration_s for p in seed.plans[0].phases if p.kind != "green") + 4 * 7.0
    computed = cycle_grid_bounds((45.0, 150.0), floor)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # optuna warns while it normalises the domain
        opt = BayesianCorridorOptimizer(
            seed, budget=Budget(max_trials=2), min_green_s=7.0, cycle_bounds=(45.0, 150.0)
        )
    domain = opt._distributions["cycle_s"]
    assert computed != (domain.low, domain.high)  # premise: optuna really changed the domain
    assert (opt._cycle_low, opt._cycle_high) == (domain.low, domain.high)
    plan = _four_stage_corridor(40.0)  # a 180.4 s cycle, above the range
    assert opt._warm_start_params(plan)["cycle_s"] == domain.high
    opt.warm_start([_evaluation(plan, 1.0)])


def test_offset_domain_follows_optuna_when_the_offset_step_is_inexact():
    # 0.1 s is not exactly representable: for a 40 s top cycle the computed top offset is
    # 39.800000000000004 s, which optuna normalises to 39.8. The optimizer must follow the domain
    # optuna holds, or a warm start snapped to the stale top is rejected as outside it.
    computed = math.floor((40.0 - 0.1) / 0.1) * 0.1
    seed = _corridor(cycles=(35.0, 35.0, 35.0))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # optuna warns while it normalises the domain
        opt = BayesianCorridorOptimizer(
            seed, budget=Budget(max_trials=2), cycle_bounds=(30.0, 40.0), offset_step_s=0.1
        )
    offset_domains = [opt._distributions[f"offset_{t}"] for t in ("J2", "J3")]
    assert computed != offset_domains[0].high  # premise: optuna really changed the domain
    assert offset_domains[0] is offset_domains[1]  # one domain shared by every offset
    assert opt._offset_high == offset_domains[0].high
    # 39.9 s is inside the 40 s cycle but above the domain's top (a 39.95 s offset would round
    # to the whole cycle, i.e. 0): it is clamped to the top optuna holds.
    plan = _with_offsets(_corridor(cycles=(40.0, 40.0, 40.0)), {"J2": 39.9, "J3": 12.34})
    params = opt._warm_start_params(plan)
    assert params["offset_J2"] == opt._offset_high
    assert params["offset_J3"] == pytest.approx(12.3)
    opt.warm_start([_evaluation(plan, 1.0)])  # optuna accepts both snapped offsets


def test_warm_start_snaps_cycle_and_offsets_into_the_domain():
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2), cycle_bounds=(60.0, 120.0))
    # 200 s cycle: above the bounds, with an offset beyond the longest sampled cycle.
    long_plan = _with_offsets(_corridor(cycles=(200.0, 200.0, 200.0)), {"J2": 150.5, "J3": 3.4})
    params = opt._warm_start_params(long_plan)
    assert (params["cycle_s"], params["offset_J2"], params["offset_J3"]) == (120.0, 119.0, 3.0)
    # 40 s cycle: below the bounds.
    short_plan = _corridor(cycles=(40.0, 40.0, 40.0))
    assert opt._warm_start_params(short_plan)["cycle_s"] == 60.0
    opt.warm_start([_evaluation(long_plan, 2.0), _evaluation(short_plan, 1.0)])
    assert opt.best().plan is short_plan


def test_warm_start_offsets_are_relative_to_the_first_tls():
    # Decoding pins the first TLS at 0 and coordination is invariant to a common shift, so a
    # plan's warm-start point holds every later offset relative to the first TLS's.
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2))
    plan = _with_offsets(seed, {"J1": 30.0, "J2": 50.0, "J3": 10.0})
    params = opt._warm_start_params(plan)
    assert params["offset_J2"] == 20.0  # 50 - 30
    assert params["offset_J3"] == 70.0  # 10 - 30, wrapped into the 90 s cycle
    assert "offset_J1" not in params
    opt.warm_start([_evaluation(plan, 1.0)])
    # Decoded, that point is the plan with its first TLS shifted to 0.
    assert [sp.offset_s for sp in _decode(opt, plan).plans] == [0.0, 20.0, 70.0]
    # The first TLS is the SEED's first TLS, wherever the plan happens to list it.
    reordered = replace(plan, plans=tuple(reversed(plan.plans)))
    assert opt._warm_start_params(reordered) == params


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (0.1 + 0.2, 0.3, 0.0),  # the same offset up to float noise, the later one a hair smaller
        (0.0, 89.6, 0.0),  # rounds to the whole 90 s cycle, which is its start
        (30.0, 29.6, 0.0),  # 0.4 s before the first TLS: wraps to 89.6, rounds to the cycle
        (30.0, 29.4, 89.0),  # wraps to 89.4 and rounds to 89, still inside the cycle
        (0.0, 20.4, 20.0),
    ],
)
def test_warm_start_offsets_snap_before_wrapping(first, second, expected):
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2))
    plan = _with_offsets(seed, {"J1": first, "J2": second})
    assert opt._warm_start_params(plan)["offset_J2"] == expected
    opt.warm_start([_evaluation(plan, 1.0)])


def test_warm_start_offset_near_the_cycle_is_zero_when_the_cycle_sum_has_float_noise():
    # J2's greens sum to 90.000000000001 s, so its offset 89.6 rounds to 90 s, a hair UNDER
    # the cycle: still the cycle's start, not the top of the offset domain.
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2))
    j2 = _tls("J2", (49.2, 32.8 + 1e-12), offset=89.6)
    assert j2.cycle_s > 90.0  # premise
    plan = CorridorPlan(
        plan_id="noisy", plans=(seed.plans[0], j2, seed.plans[2]), provenance=seed.provenance
    )
    assert opt._warm_start_params(plan)["offset_J2"] == 0.0


def test_warm_start_offsets_wrap_in_each_tls_own_cycle():
    seed = _corridor(cycles=(140.0, 60.0, 90.0))
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2), cycle_bounds=(45.0, 240.0))
    plan = _with_offsets(seed, {"J1": 30.0, "J2": 10.0, "J3": 50.0})
    params = opt._warm_start_params(plan)
    assert params["offset_J2"] == 40.0  # 10 - 30, wrapped into J2's own 60 s cycle
    assert params["offset_J3"] == 20.0  # 50 - 30, in J3's 90 s cycle
    opt.warm_start([_evaluation(plan, 1.0)])
    # An offset that rounds to the whole cycle of THAT TLS is the start of it, i.e. 0.
    near_end = opt._warm_start_params(_with_offsets(seed, {"J2": 59.6, "J3": 89.6}))
    assert (near_end["offset_J2"], near_end["offset_J3"]) == (0.0, 0.0)


def test_mixed_cycle_seed_warm_start_uses_each_tls_own_greens_and_the_max_cycle():
    seed = _corridor(cycles=(60.0, 140.0, 90.0))  # the longest cycle is neither first nor last
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2), cycle_bounds=(45.0, 240.0))
    params = opt._warm_start_params(seed)
    assert params["cycle_s"] == 140.0
    for sp in seed.plans:
        greens = [p.duration_s for p in sp.phases if p.kind == "green"]
        assert [params[f"f_{sp.tls_id}_{k}"] for k in range(2)] == factors_from_greens(greens, 5.0)
    # Different cycles give different green headroom ratios, so the factors really are per TLS.
    assert len({params[f"f_{t}_1"] for t in ("J1", "J2", "J3")}) == 3
    # The seed's maximum cycle is clamped into tighter bounds.
    tight = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2), cycle_bounds=(45.0, 120.0))
    assert tight._warm_start_params(seed)["cycle_s"] == 120.0


def test_warm_start_rejects_incompatible_plans_and_leaves_the_study_untouched():
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2))
    lacking = CorridorPlan(plan_id="lacking", plans=seed.plans[:2], provenance=seed.provenance)
    with pytest.raises(ValueError, match="lacks TLS 'J3'"):
        opt.warm_start([_evaluation(lacking, 1.0)])
    # The first TLS anchors the relative offsets, so its absence must still be this ValueError
    # (the orchestration only skips seeds on ValueError), never a KeyError.
    lacking_first = CorridorPlan(
        plan_id="lacking_first", plans=seed.plans[1:], provenance=seed.provenance
    )
    with pytest.raises(ValueError, match="lacks TLS 'J1'"):
        opt.warm_start([_evaluation(lacking_first, 1.0)])
    three_stage = CorridorPlan(
        plan_id="three",
        plans=(seed.plans[0], _tls("J2", (30.0, 20.0, 10.0), states=_STATES4[:3]), seed.plans[2]),
        provenance=seed.provenance,
    )
    with pytest.raises(ValueError, match="3 green stage.*'J2'.*seed plan has 2"):
        opt.warm_start([_evaluation(three_stage, 1.0)])
    assert len(opt._study.trials) == 0 and opt.best() is None
    opt.warm_start([_evaluation(seed, 1.0)])
    assert len(opt._study.trials) == 1 and opt.best().plan is seed


@pytest.mark.parametrize("cycle_bounds", [None, (60.0, 150.0)], ids=["fixed-cycle", "cycle-search"])
def test_warm_start_rejects_a_plan_with_different_non_green_time(cycle_bounds):
    # The seed has 3 s yellow + 1 s all-red per stage (8 s of non-green time) and decoded plans
    # keep it, so a plan with 4 s yellows is not a point of the search space. Both plans below
    # have a 90 s cycle: only the non-green time differs.
    seed = _corridor()
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2), cycle_bounds=cycle_bounds)
    longer_yellow = CorridorPlan(
        plan_id="yellow4",
        plans=tuple(_tls(sp.tls_id, (40.0, 40.0), yellow_s=4.0) for sp in seed.plans),
        provenance=seed.provenance,
    )
    assert longer_yellow.common_cycle_s() == pytest.approx(90.0)
    with pytest.raises(ValueError, match="10s of non-green time at 'J1' but the seed plan has 8s"):
        opt.warm_start([_evaluation(longer_yellow, 1.0)])
    assert len(opt._study.trials) == 0 and opt.best() is None


@pytest.mark.parametrize(
    ("cycle_bounds", "plan_cycle", "accepted"),
    [
        (None, 89.6, True),  # within CYCLE_TOLERANCE_S of the fixed 90 s cycle
        (None, 90.3, True),
        (None, 90.6, False),
        (None, 100.0, False),
        ((60.0, 150.0), 100.0, True),  # cycle search: any cycle is clamped into the bounds
        ((60.0, 150.0), 140.0, True),
    ],
)
def test_fixed_cycle_warm_start_needs_the_seed_cycle_within_tolerance(
    cycle_bounds, plan_cycle, accepted
):
    seed = _corridor()  # 90 s
    opt = BayesianCorridorOptimizer(seed, budget=Budget(max_trials=2), cycle_bounds=cycle_bounds)
    plan = _corridor(cycles=(plan_cycle,) * 3)
    if accepted:
        opt.warm_start([_evaluation(plan, 1.0)])
        assert len(opt._study.trials) == 1
        return
    message = rf"runs a {plan_cycle:g}s cycle at 'J1' but the search keeps the seed's 90s cycle"
    with pytest.raises(ValueError, match=message):
        opt.warm_start([_evaluation(plan, 1.0)])
    assert len(opt._study.trials) == 0 and opt.best() is None


# ---------------------------------------------------------------------------
# Configuration errors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"min_green_s": 0.0}, "min_green_s"),
        ({"offset_step_s": 0.0}, "offset_step_s"),
        ({"offset_step_s": -1.0}, "offset_step_s"),
        ({"offset_step_s": 100.0}, "offset_step_s"),  # no offset fits in a 90 s cycle
        ({"min_green_s": 45.0}, "below the floor"),  # 8 s fixed + 2 x 45 s > the 90 s seed cycle
        ({"min_green_s": 45.0, "cycle_bounds": (45.0, 80.0)}, "cycle_bounds"),  # floor 98 s > 80 s
        ({"cycle_bounds": (45.0, float("inf"))}, "cycle_bounds"),
        ({"cycle_bounds": (float("nan"), 150.0)}, "cycle_bounds"),
    ],
)
def test_invalid_configuration_is_rejected(kwargs, match):
    with pytest.raises(ValueError, match=match):
        BayesianCorridorOptimizer(_corridor(), budget=Budget(max_trials=1), **kwargs)


def test_tls_without_a_green_phase_is_rejected():
    no_green = SignalPlan(
        tls_id="J2",
        program_id="seed",
        offset_s=0.0,
        phases=(
            PhaseSpec(state="yyrr", duration_s=3.0, kind="yellow"),
            PhaseSpec(state="rrrr", duration_s=1.0, kind="all_red"),
        ),
    )
    seed = _corridor()
    broken = CorridorPlan(
        plan_id="broken", plans=(seed.plans[0], no_green), provenance=seed.provenance
    )
    with pytest.raises(ValueError, match="'J2' has no green phase"):
        BayesianCorridorOptimizer(broken, budget=Budget(max_trials=1))


# ---------------------------------------------------------------------------
# Package surface
# ---------------------------------------------------------------------------

def test_is_exported_with_the_other_static_optimizers():
    import terasim_city.optimize as optimize

    assert optimize.BayesianCorridorOptimizer is BayesianCorridorOptimizer
    assert "BayesianCorridorOptimizer" in optimize.__all__
