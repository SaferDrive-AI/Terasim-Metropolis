"""The built-in ``signal_timing`` plugin: request mapping, error contract, end-to-end runs."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from terasim_plugin_sdk import (
    CorridorSelection,
    CurrentPlan,
    KpiWeights,
    MapInput,
    OperationRequest,
    PluginInputError,
    PluginRegistry,
    TrafficSnapshot,
    get_plugin,
)
from terasim_plugin_sdk.builtin import SIGNAL_TIMING_PARAMS, SignalTimingPlugin
from terasim_plugin_sdk.conformance import demo_request, run_conformance

from tests.test_plugin_sdk.conftest import (
    DEMO_DIR,
    apply,
    needs_demo,
    simulates,
    write_demo_nema_plan,
)

pytestmark = [
    needs_demo,
    pytest.mark.skipif(importlib.util.find_spec("sumolib") is None, reason="needs sumolib"),
]


def _request(tmp_path: Path, **changes) -> OperationRequest:
    fields = dict(
        map=MapInput.from_sumocfg(DEMO_DIR / "demo.sumocfg"),
        traffic=TrafficSnapshot.from_file(DEMO_DIR / "demo_tmc.json"),
        output_dir=tmp_path / "out",
        corridor=CorridorSelection.between("i1", "i3"),
        current_plan=CurrentPlan.timing_sheet(DEMO_DIR / "baseline_sheet.json"),
        sim_end=300.0,
    )
    fields.update(changes)
    return OperationRequest(**fields).resolved()


def test_signal_timing_is_the_registered_builtin(fresh_default_registry):
    assert isinstance(get_plugin("signal_timing"), SignalTimingPlugin)
    assert sorted(SIGNAL_TIMING_PARAMS) == [
        "counts_source",
        "cycle_max_s",
        "cycle_min_s",
        "flow_source",
        "max_trials",
        "optimizer",
        "patience",
        "search_cycle",
        "stage_mode",
    ]


def test_request_maps_onto_the_core_config(tmp_path):
    from terasim_city.evaluation.moe import KpiConfig

    request = _request(
        tmp_path,
        weights=KpiWeights({"control_delay": 0.9, "stops": 0.1}),
        sim_end=600.0,
        seed=11,
        n_workers=1,
        params={
            "optimizer": "bayes_corridor",
            "max_trials": 6,
            "patience": 2,
            "search_cycle": True,
            "cycle_min_s": 60,
            "cycle_max_s": 120,
            "stage_mode": "per_approach",
        },
    )
    config = SignalTimingPlugin().build_config(request)
    assert config.tmc_json == DEMO_DIR / "demo_tmc.json"
    assert config.base_sumocfg == DEMO_DIR / "demo.sumocfg"
    assert config.output_dir == tmp_path / "out"
    assert (config.from_ref, config.to_ref, config.tls_ids) == ("i1", "i3", ())
    assert config.baseline_sheet == DEMO_DIR / "baseline_sheet.json"
    assert config.kpi == KpiConfig.from_dict({"control_delay": 0.9, "stops": 0.1})
    assert (config.sim_end, config.seed, config.n_workers) == (600.0, 11, 1)
    assert (config.optimizer, config.budget.max_trials, config.budget.patience) == (
        "bayes_corridor",
        6,
        2,
    )
    assert (config.search_cycle, config.cycle_min_s, config.cycle_max_s) == (True, 60.0, 120.0)
    assert (config.stage_mode, config.flow_source, config.vehicle_behavior) == (
        "per_approach",
        "corridor_graph",
        "sumo_passive",
    )
    assert config.plan_data_root is None and config.seed_from_scenario_programs is False


def test_current_plan_kinds_map_onto_the_core_config(tmp_path):
    plugin = SignalTimingPlugin()

    assert plugin.build_config(_request(tmp_path, current_plan=None)).baseline_sheet is None
    programs = plugin.build_config(_request(tmp_path, current_plan=CurrentPlan.scenario_programs()))
    assert programs.seed_from_scenario_programs is True and programs.baseline_sheet is None

    document = write_demo_nema_plan(tmp_path / "plans")
    nema = plugin.build_config(
        _request(
            tmp_path, current_plan=CurrentPlan.nema_plan(document, plan_time="2026-07-01T08:00:00")
        )
    )
    assert nema.plan_data_root == tmp_path / "out" / "inputs" / "plan_store"
    assert nema.plan_time == "2026-07-01T08:00:00"
    assert (nema.plan_data_root / "plans" / "demo_arterial" / "i3" / "plan.json").is_file()

    store = plugin.build_config(
        _request(tmp_path, current_plan=CurrentPlan.plan_store(nema.plan_data_root))
    )
    assert store.plan_data_root == nema.plan_data_root and store.plan_time is None


@pytest.mark.parametrize(
    "params, message",
    [
        ({"colour": "blue"}, "does not accept params \\['colour'\\]"),
        ({"max_trials": "5"}, "must be an integer"),
        ({"max_trials": True}, "must be an integer"),
        ({"cycle_min_s": "45"}, "must be a number"),
        ({"search_cycle": "yes"}, "true or false"),
        ({"optimizer": 3}, "must be a string"),
        ({"optimizer": "simulated_annealing"}, "optimizer must be one of"),
        ({"max_trials": 0}, "max_trials"),
        ({"cycle_min_s": 120, "cycle_max_s": 60}, "cycle_min_s < cycle_max_s"),
        ({"flow_source": "loop_detectors"}, "unknown flow_source"),
        ({"stage_mode": "four_stage"}, "stage_mode"),
    ],
)
def test_invalid_params_are_input_errors(tmp_path, params, message):
    with pytest.raises(PluginInputError, match=message):
        SignalTimingPlugin().build_config(_request(tmp_path, params=params))


def test_other_invalid_requests_are_input_errors(tmp_path):
    plugin = SignalTimingPlugin()
    with pytest.raises(PluginInputError, match="unknown vehicle_behavior"):
        plugin.build_config(_request(tmp_path, vehicle_behavior="teleporting_cars"))
    with pytest.raises(PluginInputError, match="map file not found"):
        plugin.build_config(_request(tmp_path, map=MapInput.from_sumocfg(tmp_path / "x.sumocfg")))
    # Raised inside the core run, before any trial: still the input-error contract.
    with pytest.raises(PluginInputError, match="J9"):
        plugin.run(_request(tmp_path, corridor=CorridorSelection.signals("J9")))
    with pytest.raises(PluginInputError, match="bayes_splits"):
        plugin.run(_request(tmp_path, params={"optimizer": "bayes_splits"}))


def test_cancelling_from_progress_stops_before_any_trial(tmp_path):
    class Cancelled(Exception):
        pass

    calls = []

    def progress(done, total, label):
        calls.append((done, total, label))
        raise Cancelled()

    with pytest.raises(Cancelled):
        SignalTimingPlugin().run(_request(tmp_path), progress=progress)
    assert calls == [(0, 1 + 20, None)]  # the sheet seed plus the default search budget
    assert not list((tmp_path / "out").glob("trials/trial_*"))


# ---------------------------------------------------------------------------
# End to end (SUMO)
# ---------------------------------------------------------------------------


def _service_kpis(core_result: dict, weights: dict) -> dict:
    optimize_api = pytest.importorskip("terasim_service.city.optimize_api")
    kpis = optimize_api._direct_kpis(core_result, weights)
    for side in ("baseline", "optimized"):
        if kpis[side].get("timing") is None:
            kpis[side].pop("timing", None)
    return kpis


@apply(simulates)
def test_signal_timing_end_to_end_with_a_timing_sheet(tmp_path):
    registry = PluginRegistry(entry_points=False)
    request = demo_request(
        DEMO_DIR, tmp_path / "out", params={"max_trials": 2, "patience": 0}, n_workers=2
    )

    result = run_conformance(registry.get("signal_timing"), request)

    out = tmp_path / "out"
    core_result = json.loads(
        (out / "corridor_optimization_result.json").read_text(encoding="utf-8")
    )
    assert result.kpis == _service_kpis(core_result, {"control_delay": 0.7, "stops": 0.3})
    assert result.tls_ids == ("J1", "J2", "J3")
    assert result.corridor_id == "demo_arterial"
    assert result.nema_plan == json.loads((out / "nema_plan.json").read_text(encoding="utf-8"))
    assert result.nema_plan["handoff_ready"] is True
    assert result.kpis["baseline"]["timing"]["offsets_s"] == {"J1": 0.0, "J2": 20.0, "J3": 40.0}
    assert result.details["seed_source"] == "baseline_sheet"
    assert result.details["baseline_plan_id"] == "baseline"
    assert result.details["n_trials"] == 3
    assert {"corridor_optimization_result", "trials_ledger", "scenario", "trials"} <= set(
        result.artifacts
    )
    assert len((out / "trials.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    assert result.response() == {"nema_plan": result.nema_plan, "kpis": result.kpis}


@apply(simulates)
def test_signal_timing_end_to_end_with_a_nema_plan_document(tmp_path):
    document = write_demo_nema_plan(tmp_path / "plans")
    request = OperationRequest(
        map=MapInput.from_sumocfg(DEMO_DIR / "demo.sumocfg"),
        traffic=TrafficSnapshot.from_file(DEMO_DIR / "demo_tmc.json"),
        output_dir=tmp_path / "out",
        corridor=CorridorSelection.between("i1", "i3"),
        current_plan=CurrentPlan.nema_plan(document),
        sim_end=300.0,
        n_workers=1,
        params={"optimizer": "exhaustive"},
    )

    result = run_conformance(SignalTimingPlugin(), request)

    assert result.details["seed_source"] == "stored_nema"
    assert result.details["stored_plans_used"] is True
    assert [item["pattern_id"] for item in result.nema_plan["intersections"]] == ["am_peak"] * 3
    assert result.kpis["optimized"]["cost"] == result.kpis["baseline"]["cost"]
    assert result.notes == (
        "no search after the seed plans: optimizer 'exhaustive' evaluates the seed plans only",
    )
