"""NemaCorridorPlan: the optimizer-facing NEMA plan representation.

Names avoid the ``sumo``/``simulation`` substrings (conftest auto-mark).
"""

from __future__ import annotations

import pytest

from terasim_city.optimize.base import Budget
from terasim_city.optimize.bayes_nema_offsets import BayesianNemaOffsetOptimizer
from terasim_city.plans.nema_plan import NemaCorridorPlan, NemaSignalEntry


def _inter(coord=(2, 6)):
    return {
        "intersection_id": "x", "name": "X",
        "phases": [{"phase": 2, "movements": ["EB-through"]},
                   {"phase": 6, "movements": ["WB-through"]}],
        "rings": {"ring1": [1, 2, 3, 4], "ring2": [5, 6, 7, 8],
                  "coordinated_phases": list(coord)},
    }


def _entry(tls: str, cycle: float, offset: float = 0.0) -> NemaSignalEntry:
    return NemaSignalEntry(
        tls_id=tls,
        intersection=_inter(),
        pattern={"pattern_id": "am", "cycle_s": cycle, "offset_s": offset,
                 "splits": {"2": cycle / 2, "6": cycle / 2}},
    )


def _plan(*entries) -> NemaCorridorPlan:
    return NemaCorridorPlan(plan_id="seed", entries=tuple(entries))


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_with_offsets_changes_only_offsets():
    plan = _plan(_entry("A", 100), _entry("B", 100))
    derived = plan.with_offsets({"B": 37.0})
    assert derived.offsets() == {"A": 0.0, "B": 37.0}
    assert plan.offsets()["B"] == 0.0                       # original untouched
    assert derived.entries[1].pattern["splits"] == plan.entries[1].pattern["splits"]


def test_with_offsets_rejects_unknown_tls():
    with pytest.raises(ValueError, match="unknown"):
        _plan(_entry("A", 100), _entry("B", 100)).with_offsets({"C": 1.0})


def test_json_roundtrip():
    plan = _plan(_entry("A", 100), _entry("B", 100, 19))
    clone = NemaCorridorPlan.from_json_dict(plan.to_json_dict())
    assert clone.offsets() == plan.offsets()
    assert clone.to_json_dict() == plan.to_json_dict()
    assert plan.to_json_dict()["kind"] == "nema"


# ---------------------------------------------------------------------------
# Cycle compatibility: equal + integer multiples
# ---------------------------------------------------------------------------


def test_double_cycle_is_coordinatable():
    # A 70 s intersection (double-cycled) inside a 140 s corridor.
    assert _plan(_entry("A", 140), _entry("B", 70)).base_cycle_s() == 70


def test_non_multiple_cycles_raise():
    with pytest.raises(ValueError, match="cannot coordinate"):
        _plan(_entry("A", 140), _entry("B", 126)).base_cycle_s()


# ---------------------------------------------------------------------------
# Offset optimizer
# ---------------------------------------------------------------------------

def test_optimizer_mutates_only_nonreference_offsets():
    plan = _plan(_entry("A", 100), _entry("B", 100), _entry("C", 100))
    opt = BayesianNemaOffsetOptimizer(plan, budget=Budget(max_trials=4), rng_seed=1)
    batch = opt.ask(2)
    assert len(batch) == 2
    for candidate in batch:
        offs = candidate.offsets()
        assert offs["A"] == 0.0                              # reference frozen
        assert 0.0 <= offs["B"] <= 99.0 and offs["B"] == round(offs["B"])
        assert 0.0 <= offs["C"] <= 99.0
        assert candidate.plan_id.startswith("bo_")
        assert candidate.provenance.source == "bo_iteration"


def test_optimizer_searches_own_cycle_for_double_cycled_tls():
    # B runs 70 s (double cycle): its offset domain is [0, 69], not [0, 139].
    plan = _plan(_entry("A", 140), _entry("B", 70))
    opt = BayesianNemaOffsetOptimizer(plan, budget=Budget(max_trials=8), rng_seed=2)
    seen = [c.offsets()["B"] for c in opt.ask(8)]
    assert all(0.0 <= o <= 69.0 for o in seen)


def test_optimizer_rejects_single_intersection():
    with pytest.raises(ValueError, match=">= 2"):
        BayesianNemaOffsetOptimizer(_plan(_entry("A", 100)), budget=Budget())


