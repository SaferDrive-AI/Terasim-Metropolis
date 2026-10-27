"""The SDK's KPI projection equals the service's ``kpis`` block (no simulation)."""

from __future__ import annotations

import copy
import math

import pytest
from terasim_plugin_sdk import KpiWeights, PluginRunError
from terasim_plugin_sdk.toolkit import PlanScore, best_plan, build_kpis, kpis_from_corridor_result


def _moe(delay: float, stops: float) -> dict:
    return {"control_delay_s_per_veh": delay, "stops_per_veh": stops, "vehicles_total": 100}


def _coarse_result() -> dict:
    timing = {"cycle_s": {"J1": 90.0}, "offsets_s": {"J1": 0.0}, "greens_s": {"J1": [48.0, 34.0]}}
    return {
        "schema": "terasim.corridor_optimization.v1",
        "baseline": {"plan_id": "baseline", "score": 1.0, "moe": _moe(30.0, 0.9), "timing": timing},
        "best": {
            "plan_id": "bo_0003",
            "score": 0.91,
            "moe": _moe(27.0, 0.85),
            "timing": {**timing, "offsets_s": {"J1": 12.0}},
        },
    }


def _refine_result(winner: str) -> dict:
    result = _coarse_result()
    result["export_source"] = "refine"
    result["refine"] = {
        "baseline": {"plan_id": "baseline", "score": 1.0, "moe": _moe(33.0, 1.0)},
        "evaluations": [
            {"plan_id": "bo_0003", "score": 0.95, "moe": _moe(31.0, 0.97)},
            {"plan_id": "bo_0001", "score": 0.93, "moe": _moe(30.5, 0.95)},
        ],
        "winner_plan_id": winner,
    }
    return result


def _service_kpis(result: dict, weights: dict) -> dict:
    """The service's projection, as serialized: a missing ``timing`` is left out."""
    optimize_api = pytest.importorskip("terasim_service.city.optimize_api")
    kpis = optimize_api._direct_kpis(copy.deepcopy(result), weights)
    for side in ("baseline", "optimized"):
        if kpis[side].get("timing") is None:
            kpis[side].pop("timing", None)
    return kpis


@pytest.mark.parametrize(
    "result",
    [_coarse_result(), _refine_result("bo_0001"), _refine_result("baseline")],
    ids=["coarse", "refine-winner", "refine-baseline-wins"],
)
@pytest.mark.parametrize("weights", [{"control_delay": 0.7, "stops": 0.3}, {"stops": 1.0}])
def test_projection_matches_the_service(result, weights):
    sdk = kpis_from_corridor_result(result, KpiWeights(weights))
    assert sdk == _service_kpis(result, weights)


def test_refine_projection_reads_the_refine_block():
    kpis = kpis_from_corridor_result(_refine_result("bo_0001"), KpiWeights())
    assert kpis["baseline"] == {"cost": 1.0, "metrics": _moe(33.0, 1.0)}
    assert kpis["optimized"]["cost"] == 0.93


def test_unknown_refine_winner_is_a_run_error():
    with pytest.raises(PluginRunError, match="winner_plan_id"):
        kpis_from_corridor_result(_refine_result("bo_9999"), KpiWeights())


class _Plan:
    def __init__(self, plan_id):
        self.plan_id = plan_id


def _score(plan_id, cost, valid=True):
    return PlanScore(plan=_Plan(plan_id), valid=valid, cost=cost, metrics=_moe(10.0, 0.5))


def test_best_plan_keeps_the_first_of_ties_and_skips_invalid():
    scores = [
        _score("a", 1.0),
        _score("b", 1.0),
        _score("c", None, valid=False),
        _score("d", math.inf),
    ]
    assert best_plan(scores).plan_id == "a"
    assert best_plan([_score("x", 1.0), _score("y", 0.5)]).plan_id == "y"
    with pytest.raises(PluginRunError, match="none of the 2"):
        best_plan([_score("c", None, valid=False), _score("d", math.inf)])


def test_build_kpis_needs_valid_scores():
    with pytest.raises(PluginRunError, match="no valid evaluation"):
        build_kpis(_score("a", None, valid=False), _score("b", 0.9), KpiWeights())
