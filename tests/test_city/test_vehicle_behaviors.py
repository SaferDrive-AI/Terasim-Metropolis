"""Tests for the vehicle-behavior registry (the CNDE seam) and its plumbing.

Registry unit tests are pure. The orchestration-level tests reuse the
inline-eval fast pattern of ``test_corridor_optimization``: a monkeypatched
``evaluate_plan_task`` captures the task dicts so the behavior name and the
lane-assist gating can be asserted without ever starting SUMO.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from terasim_city.factories import (
    VEHICLE_BEHAVIORS,
    PassiveVehicleFactory,
    register_vehicle_behavior,
    resolve_vehicle_factory,
)
from terasim_city.optimize.batch import INLINE_EVAL_ENV_VAR, BatchEvaluator
from terasim_city.orchestration.corridor_optimization import (
    CorridorOptimizationConfig,
    run_corridor_optimization,
)
from terasim_city.plans.model import (
    CorridorPlan,
    PhaseSpec,
    PlanProvenance,
    SignalPlan,
)
from tests.fixtures.tmc_templates import demo_tmc, write_tmc
from tests.test_city.conftest import REPO_ROOT, requires_sumolib

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_CFG = DEMO_DIR / "demo.sumocfg"

requires_demo_assets = pytest.mark.skipif(
    not DEMO_CFG.exists(),
    reason=f"corridor demo assets not present at {DEMO_DIR} "
    "(run examples/corridor_demo/build_demo_map.py)",
)


@pytest.fixture(autouse=True)
def _restore_registry():
    """The registry is module-global mutable state: snapshot and restore."""
    snapshot = dict(VEHICLE_BEHAVIORS)
    yield
    VEHICLE_BEHAVIORS.clear()
    VEHICLE_BEHAVIORS.update(snapshot)


class _FakeFactory:
    pass


def _dummy_plan() -> CorridorPlan:
    return CorridorPlan(
        plan_id="dummy",
        plans=(
            SignalPlan(
                tls_id="J1",
                program_id="dummy",
                offset_s=0.0,
                phases=(
                    PhaseSpec(state="GGrr", duration_s=30.0, kind="green"),
                    PhaseSpec(state="yyrr", duration_s=3.0, kind="yellow"),
                    PhaseSpec(state="rrGG", duration_s=30.0, kind="green"),
                    PhaseSpec(state="rryy", duration_s=3.0, kind="yellow"),
                ),
            ),
        ),
        provenance=PlanProvenance(source="explicit"),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_resolve_sumo_passive_returns_fresh_factory_instances():
    first = resolve_vehicle_factory("sumo_passive")
    second = resolve_vehicle_factory("sumo_passive")
    assert isinstance(first, PassiveVehicleFactory)
    assert isinstance(second, PassiveVehicleFactory)
    assert first is not second


def test_resolve_unknown_behavior_lists_registered_names():
    with pytest.raises(ValueError, match="unknown vehicle behavior 'warp_drive'") as exc:
        resolve_vehicle_factory("warp_drive")
    message = str(exc.value)
    assert "sumo_passive" in message
    assert "cnde" in message


def test_cnde_resolves_to_a_real_factory():
    """'cnde' is wired to the real CndeVehicleFactory, not a
    fail-loud placeholder. Resolving it must NOT instantiate the GPU model —
    CndeVehicleFactory itself is a cheap object (a minimal cfg namespace);
    the 972MB checkpoint only loads on the first create_vehicle() call.
    """
    assert "cnde" in VEHICLE_BEHAVIORS  # discoverable by name
    from terasim_city.factories.cnde_behavior import CndeVehicleFactory

    factory = resolve_vehicle_factory("cnde")
    assert isinstance(factory, CndeVehicleFactory)


def test_register_duplicate_rejected_without_replace():
    with pytest.raises(ValueError, match="already registered"):
        register_vehicle_behavior("sumo_passive", _FakeFactory)
    # The failed call must not have clobbered the existing entry.
    assert isinstance(resolve_vehicle_factory("sumo_passive"), PassiveVehicleFactory)


# ---------------------------------------------------------------------------
# BatchEvaluator plumbing
# ---------------------------------------------------------------------------

def _make_evaluator(tmp_path: Path, **kwargs) -> BatchEvaluator:
    return BatchEvaluator(
        scenario_sumocfg=tmp_path / "scenario.sumocfg",
        net_file=tmp_path / "net.net.xml",
        trials_root=tmp_path / "trials",
        sim_end=60.0,
        stalls=[],
        quality_thresholds={"emergency_stops": 0, "teleports": 0, "collisions": 0},
        n_workers=1,
        **kwargs,
    )


def test_batch_evaluator_task_carries_custom_behavior(tmp_path):
    evaluator = _make_evaluator(tmp_path, vehicle_behavior="fake")
    task = evaluator._build_task(_dummy_plan())
    assert task["vehicle_behavior"] == "fake"


# ---------------------------------------------------------------------------
# Orchestration plumbing (inline eval + captured task dicts, no SUMO)
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


def _install_capturing_evaluator(monkeypatch) -> list:
    """Inline eval + fake evaluate_plan_task; returns the captured task list."""
    monkeypatch.setenv(INLINE_EVAL_ENV_VAR, "1")
    captured: list[dict] = []

    def fake_evaluate_plan_task(task: dict) -> dict:
        captured.append(task)
        return {
            "plan_id": task["plan"]["plan_id"],
            "moe": _synthetic_moe(10.0),
            "valid": True,
            "quality_errors": [],
            "trial_dir": task["trial_dir"],
            "kpi": {},
        }

    monkeypatch.setattr(
        "terasim_city.optimize.batch.evaluate_plan_task", fake_evaluate_plan_task
    )
    return captured


@requires_demo_assets
@requires_sumolib
def test_run_echoes_behavior_for_sumo_passive(
    tmp_path, monkeypatch
):
    captured = _install_capturing_evaluator(monkeypatch)
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        tls_ids=("J2",),
        optimizer="exhaustive",
        sim_end=60.0,
        n_workers=1,
    )
    result = run_corridor_optimization(cfg)

    assert result["config"]["vehicle_behavior"] == "sumo_passive"
    assert captured, "no evaluation task was captured"
    assert all(t["vehicle_behavior"] == "sumo_passive" for t in captured)
    written = json.loads(
        (cfg.output_dir / "corridor_optimization_result.json").read_text()
    )
    assert written["config"]["vehicle_behavior"] == "sumo_passive"
