"""Unit tests for the frozen plan value objects (no SUMO required)."""

from __future__ import annotations

import json


import pytest

from terasim_city.optimize import Budget, PlanEvaluation
from terasim_city.plans import (
    MOVEMENTS,
    CorridorPlan,
    PhaseSpec,
    PlanProvenance,
    SignalPlan,
)


def _phase(state="GGrr", duration=30.0, kind="green", **kwargs) -> PhaseSpec:
    return PhaseSpec(state=state, duration_s=duration, kind=kind, **kwargs)


def _signal_plan(tls_id="J1", offset=0.0, durations=(40.0, 3.0, 1.0)) -> SignalPlan:
    kinds = ("green", "yellow", "all_red")
    states = ("GGrr", "yyrr", "rrrr")
    phases = tuple(
        _phase(state=s, duration=d, kind=k) for s, d, k in zip(states, durations, kinds)
    )
    return SignalPlan(tls_id=tls_id, program_id="p1", offset_s=offset, phases=phases)


def _corridor_plan(**overrides) -> CorridorPlan:
    fields = {
        "plan_id": "seed_default",
        "plans": (_signal_plan("J1"), _signal_plan("J2", offset=12.0)),
        "provenance": PlanProvenance(source="sumo_tls"),
    }
    fields.update(overrides)
    return CorridorPlan(**fields)


# ---------------------------------------------------------------------------
# MOVEMENTS
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PhaseSpec
# ---------------------------------------------------------------------------

def test_phase_spec_valid_and_coerces_sequences():
    phase = PhaseSpec(
        state="GGrr",
        duration_s=30,
        kind="green",
        nema_phases=[2, 6],
        movements=["EB-through", "WB-through"],
    )
    assert phase.duration_s == 30.0
    assert phase.nema_phases == (2, 6)
    assert phase.movements == ("EB-through", "WB-through")


@pytest.mark.parametrize("duration", [0.0, -5.0])
def test_phase_spec_rejects_nonpositive_duration(duration):
    with pytest.raises(ValueError, match="duration_s"):
        _phase(duration=duration)


def test_phase_spec_rejects_bad_kind():
    with pytest.raises(ValueError, match="kind"):
        _phase(kind="flashing")


def test_phase_spec_rejects_bad_movement():
    with pytest.raises(ValueError, match="EB-uturn"):
        _phase(movements=("EB-uturn",))


def test_phase_spec_rejects_empty_state():
    with pytest.raises(ValueError, match="state"):
        _phase(state="")


def test_phase_spec_accepts_all_nema_dual_ring_numbers():
    phase = _phase(nema_phases=tuple(range(1, 9)))
    assert phase.nema_phases == (1, 2, 3, 4, 5, 6, 7, 8)


# ---------------------------------------------------------------------------
# SignalPlan
# ---------------------------------------------------------------------------

def test_signal_plan_cycle_is_sum_of_durations():
    plan = _signal_plan(durations=(40.0, 3.0, 1.0))
    assert plan.cycle_s == pytest.approx(44.0)


def test_signal_plan_rejects_empty_phases():
    with pytest.raises(ValueError, match="phases"):
        SignalPlan(tls_id="J1", program_id="p1", offset_s=0.0, phases=())


def test_signal_plan_sanitizes_program_id():
    plan = SignalPlan(
        tls_id="cluster_1_#1more",
        program_id="-416901209#1_1",
        offset_s=0.0,
        phases=(_phase(),),
    )
    assert plan.program_id == "-416901209_1_1"
    # tls_id is a SUMO net identifier and must be preserved verbatim.
    assert plan.tls_id == "cluster_1_#1more"


# ---------------------------------------------------------------------------
# PlanProvenance
# ---------------------------------------------------------------------------

def test_provenance_rejects_unknown_source():
    with pytest.raises(ValueError, match="source"):
        PlanProvenance(source="guesswork")


def test_provenance_coerces_params():
    prov = PlanProvenance(source="bo_iteration", params=[["offset_J2", 12]])
    assert prov.params == (("offset_J2", 12.0),)


def test_provenance_keeps_string_params_verbatim():
    # Labels such as the Webster stage mode are provenance too: numbers are
    # still coerced to float, strings are not.
    prov = PlanProvenance(
        source="webster_init", params=[["cycle_s", 60], ["stage_mode", "per_approach"]]
    )
    assert prov.params == (("cycle_s", 60.0), ("stage_mode", "per_approach"))


def test_provenance_string_params_survive_the_json_round_trip():
    # Trials cross the process boundary as to_json_dict()/from_json_dict().
    original = _corridor_plan(
        provenance=PlanProvenance(
            source="webster_init",
            params=(("cycle_s", 60.0), ("stage_mode", "per_approach")),
        )
    )
    restored = CorridorPlan.from_json_dict(json.loads(json.dumps(original.to_json_dict())))
    assert restored == original
    assert ("stage_mode", "per_approach") in restored.provenance.params


# ---------------------------------------------------------------------------
# CorridorPlan
# ---------------------------------------------------------------------------

def test_corridor_plan_rejects_duplicate_tls():
    with pytest.raises(ValueError, match="duplicate tls_id"):
        _corridor_plan(plans=(_signal_plan("J1"), _signal_plan("J1")))


def test_corridor_plan_rejects_empty_plans():
    with pytest.raises(ValueError, match="no per-TLS plans"):
        _corridor_plan(plans=())


def test_corridor_plan_by_tls_preserves_order():
    plan = _corridor_plan()
    by_tls = plan.by_tls()
    assert list(by_tls) == ["J1", "J2"]
    assert by_tls["J2"].offset_s == 12.0


def test_common_cycle_within_tolerance():
    plans = (_signal_plan("J1"), _signal_plan("J2", durations=(40.4, 3.0, 1.0)))
    assert _corridor_plan(plans=plans).common_cycle_s() == pytest.approx(44.0)


def test_common_cycle_mismatch_lists_per_tls_cycles():
    plans = (_signal_plan("J1"), _signal_plan("J2", durations=(50.0, 3.0, 1.0)))
    plan = _corridor_plan(plans=plans)
    with pytest.raises(ValueError) as exc:
        plan.common_cycle_s()
    message = str(exc.value)
    assert "J1=44" in message
    assert "J2=54" in message


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------

def test_json_round_trip_through_serialized_string():
    original = _corridor_plan(
        plan_id="bo_0004",
        provenance=PlanProvenance(
            source="bo_iteration",
            parent_plan_id="seed_default",
            iteration=4,
            params=(("offset_J2", 12.0),),
        ),
        plans=(
            _signal_plan("J1"),
            SignalPlan(
                tls_id="J2",
                program_id="p1",
                offset_s=12.0,
                phases=(
                    _phase(movements=("EB-through", "WB-through"), nema_phases=(2, 6)),
                    _phase(state="yyrr", duration=3.0, kind="yellow"),
                    _phase(state="rrrr", duration=1.0, kind="all_red", name="clearance"),
                    _phase(state="rrGG", duration=40.0, kind="green"),
                ),
            ),
        ),
    )
    payload = json.loads(json.dumps(original.to_json_dict()))
    restored = CorridorPlan.from_json_dict(payload)
    assert restored == original


def test_from_json_dict_rejects_wrong_schema():
    payload = _corridor_plan().to_json_dict()
    payload["schema"] = "terasim.something_else.v9"
    with pytest.raises(ValueError, match="schema"):
        CorridorPlan.from_json_dict(payload)


def test_from_json_dict_rejects_missing_keys():
    payload = _corridor_plan().to_json_dict()
    del payload["provenance"]
    with pytest.raises(ValueError, match="provenance"):
        CorridorPlan.from_json_dict(payload)


# ---------------------------------------------------------------------------
# optimize.base contracts (Budget / PlanEvaluation / Optimizer protocol)
# ---------------------------------------------------------------------------

def test_budget_defaults_and_validation():
    budget = Budget()
    assert budget.max_trials == 20
    assert budget.patience == 8
    with pytest.raises(ValueError, match="max_trials"):
        Budget(max_trials=0)
    # patience <= 0 disables early stopping; it is not an error.
    assert Budget(patience=0).patience == 0


