"""NEMA split optimizer: green-ratio search under dual-ring constraints.

Every sampled candidate must satisfy, by construction:
  * cycle preserved exactly,
  * both rings tile each barrier side they occupy to the same duration,
  * every phase >= min_green + yellow + all_red.
Checked on four ring structures from the synthetic plan fixtures: the demo
corridor's full 8-phase (``i1``), 5-phase with a short ring 2 (``i2``) and
6-phase lead-lag (``i3``) intersections, plus a 4-phase intersection with
permissive lefts (``park_oak``).

Names avoid the ``sumo``/``simulation`` substrings (conftest auto-mark).
"""

from __future__ import annotations

import pytest

from terasim_city.optimize.base import Budget
from terasim_city.optimize.bayes_nema_splits import (
    BayesianNemaSplitOptimizer,
    _phase_floor,
)
from terasim_city.plans.nema_plan import NemaCorridorPlan, NemaSignalEntry
from tests.fixtures.nema_plan_files import (
    DEMO_ARTERIAL_PLAN,
    FOUR_PHASE_PLAN,
    load_plan,
    plan_intersection,
)

_SIDE_A = {1, 2, 5, 6}

#: intersection id -> the fixture document that defines it.
_SOURCES = {
    "i1": DEMO_ARTERIAL_PLAN,       # full 8-phase dual ring
    "i2": DEMO_ARTERIAL_PLAN,       # 5-phase, ring 2 rests on the side-street side
    "i3": DEMO_ARTERIAL_PLAN,       # 6-phase, lead-lag arterial lefts
    "park_oak": FOUR_PHASE_PLAN,    # 4-phase, permissive lefts
}


def _seed(intersection_id: str) -> NemaCorridorPlan:
    inter = plan_intersection(_SOURCES[intersection_id], intersection_id)
    return NemaCorridorPlan(
        plan_id="seed",
        entries=(NemaSignalEntry("J", inter, inter["patterns"][0]),),
    )


def _check_constraints(plan: NemaCorridorPlan, seed: NemaCorridorPlan):
    entry, seed_entry = plan.entries[0], seed.entries[0]
    cycle = seed_entry.cycle_s
    assert entry.cycle_s == cycle                            # cycle frozen
    assert entry.offset_s == seed_entry.offset_s             # offset frozen
    splits = {int(k): v for k, v in entry.pattern["splits"].items()}
    meta = {p["phase"]: p for p in entry.intersection["phases"]}
    rings = entry.intersection["rings"]

    # per-phase floor
    for p, s in splits.items():
        assert s >= _phase_floor(meta[p]) - 0.11, (p, s)

    # each ring tiles its sides; sides consistent across rings; sum == cycle
    side_totals = {"A": set(), "B": set()}
    for rn in ("ring1", "ring2"):
        members = [p for p in rings.get(rn, []) if p]
        for side, phases in (
            ("A", [p for p in members if p in _SIDE_A]),
            ("B", [p for p in members if p not in _SIDE_A]),
        ):
            if phases:
                side_totals[side].add(round(sum(splits[p] for p in phases), 1))
    for side, totals in side_totals.items():
        assert len(totals) <= 1, f"side {side} ring sums differ: {totals}"
    total = sum(next(iter(t)) for t in side_totals.values() if t)
    assert total == pytest.approx(cycle, abs=0.11)


@pytest.mark.parametrize("iid", ["i1", "i2", "i3", "park_oak"])
def test_candidates_satisfy_dual_ring_constraints(iid):
    seed = _seed(iid)
    opt = BayesianNemaSplitOptimizer(seed, budget=Budget(max_trials=12), rng_seed=3)
    for candidate in opt.ask(12):
        _check_constraints(candidate, seed)
        assert candidate.plan_id.startswith("bo_")
        assert candidate.provenance.source == "bo_iteration"


def test_candidates_actually_vary():
    seed = _seed("i1")
    opt = BayesianNemaSplitOptimizer(seed, budget=Budget(max_trials=8), rng_seed=4)
    tables = {tuple(sorted(c.entries[0].pattern["splits"].items())) for c in opt.ask(8)}
    assert len(tables) > 1


def test_warm_start_accepts_seed():
    seed = _seed("i1")
    opt = BayesianNemaSplitOptimizer(seed, budget=Budget(max_trials=4), rng_seed=5)
    opt.warm_start([])  # no evaluations: no-op path must not crash
    params = opt._warm_start_params(seed)
    assert set(params) == set(opt._distributions)  # every dimension covered


def test_rejects_corridor_plan():
    doc = load_plan(DEMO_ARTERIAL_PLAN)
    entries = tuple(
        NemaSignalEntry(i["intersection_id"], i, i["patterns"][0])
        for i in doc["intersections"][:2]
    )
    plan = NemaCorridorPlan(plan_id="p", entries=entries)
    with pytest.raises(ValueError, match="ONE intersection"):
        BayesianNemaSplitOptimizer(plan, budget=Budget())


def test_rejects_cycle_below_floors():
    inter = plan_intersection(DEMO_ARTERIAL_PLAN, "i1")
    pattern = dict(inter["patterns"][0])
    # Side A needs 9 + 15.5 s and side B 9 + 13 s of floors: 46.5 s >> 30 s.
    pattern["cycle_s"] = 30
    plan = NemaCorridorPlan(
        plan_id="p", entries=(NemaSignalEntry("J", inter, pattern),)
    )
    with pytest.raises(ValueError, match="cannot cover"):
        BayesianNemaSplitOptimizer(plan, budget=Budget())


# ---------------------------------------------------------------------------
# with_splits (model support)
# ---------------------------------------------------------------------------

def test_with_splits_changes_only_splits():
    seed = _seed("i1")
    new = {"1": 20.0, "2": 50.0, "3": 16.0, "4": 54.0,
           "5": 15.0, "6": 55.0, "7": 36.0, "8": 34.0}
    derived = seed.with_splits({"J": new})
    assert derived.entries[0].pattern["splits"] == new
    assert seed.entries[0].pattern["splits"] != new          # original untouched
    assert derived.entries[0].cycle_s == seed.entries[0].cycle_s
    with pytest.raises(ValueError, match="unknown"):
        seed.with_splits({"nope": new})
